"""Run inside backend after Compose is started with --scale backend=2 --scale bot=2."""

import argparse
import asyncio
import base64
import io
import json
import os
import socket
import time
from pathlib import Path

import httpx
from PIL import Image


def addresses(service, port):
    return {
        address[4][0]
        for address in socket.getaddrinfo(
            service, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    }


async def confirmations(client, bots):
    responses = await asyncio.gather(
        *[client.get(f"http://{ip}:8001/telegram-api/stats") for ip in bots]
    )
    for response in responses:
        response.raise_for_status()
    return sum(r.json()["confirmations"] for r in responses)


async def main(telegram_mock=False, verify_saved=False):
    backends, bots = sorted(addresses("backend", 8000)), addresses("bot", 8001)
    assert len(backends) == len(bots) == 2, "Start exactly 2 backend and 2 bot replicas"
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, "PNG")
    update_id = time.time_ns() // 1_000_000
    payload = {
        "telegram_update_id": update_id,
        "title": f"Проверка реплик {update_id}",
        "image_base64": base64.b64encode(image.getvalue()).decode(),
    }
    state_file = Path("/tmp/replica-check.json")
    if verify_saved:
        saved = json.loads(await asyncio.to_thread(state_file.read_text))
        payload = saved["payload"]
    proxy = "http://proxy:8080"
    async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
        warmed_page = await client.get(proxy)
        assert warmed_page.status_code == 200
        assert "no-store" in warmed_page.headers.get("Cache-Control", "")
        if not verify_saved:
            assert payload["title"] not in warmed_page.text
        responses = await asyncio.gather(
            *[
                client.post(
                    f"http://{backends[i % len(backends)]}:8000/products",
                    json=payload,
                    headers={"X-Bot-Secret": os.environ["BOT_API_SECRET"]},
                )
                for i in range(12)
            ]
        )
        expected = [200] * 12 if verify_saved else [200] * 11 + [201]
        assert sorted(r.status_code for r in responses) == expected, [
            r.status_code for r in responses
        ]
        assert len({r.json()["product"]["id"] for r in responses}) == 1
        assert sum(r.json()["created"] for r in responses) == (0 if verify_saved else 1)
        if verify_saved:
            assert responses[0].json()["product"]["id"] == saved["product"]["id"]
        webhook_upstreams, api_upstreams = set(), set()
        health = await client.get(f"{proxy}/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        for i in range(12):
            # Unhandled update exercises real routing without sending Telegram messages.
            response = await client.post(
                f"{proxy}/telegram/webhook",
                json={"update_id": i},
                headers={"X-Telegram-Bot-Api-Secret-Token": os.environ["TELEGRAM_WEBHOOK_SECRET"]},
            )
            assert response.status_code == 200, response.text
            webhook_upstreams.add(response.headers["X-Upstream"])
            response = await client.get(f"{proxy}/api/products")
            assert response.status_code == 200
            api_upstreams.add(response.headers["X-Upstream"])
        assert webhook_upstreams == {f"{ip}:8001" for ip in bots}, webhook_upstreams
        assert api_upstreams == {f"{ip}:8000" for ip in backends}, api_upstreams
        product = responses[0].json()["product"]
        assert (await client.get(proxy + product["image_url"])).content == image.getvalue()
        assert payload["title"] in (await client.get(proxy)).text
        assert (await client.post(f"{proxy}/api/products", json=payload)).status_code == 403
        assert (
            await client.post(f"{proxy}/telegram/webhook", json={"update_id": 1})
        ).status_code == 401
        if telegram_mock or verify_saved:
            before = await confirmations(client, bots)
            update = (
                saved["update"]
                if verify_saved
                else {
                    "update_id": update_id + 1,
                    "message": {
                        "message_id": 1,
                        "date": int(time.time()),
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False, "first_name": "CI Admin"},
                        "caption": f"Фото webhook {update_id}",
                        "photo": [
                            {
                                "file_id": "photo",
                                "file_unique_id": "unique-photo",
                                "width": 8,
                                "height": 8,
                            }
                        ],
                    },
                }
            )
            webhooks = await asyncio.gather(
                *[
                    client.post(
                        f"{proxy}/telegram/webhook",
                        json=update,
                        headers={
                            "X-Telegram-Bot-Api-Secret-Token": os.environ["TELEGRAM_WEBHOOK_SECRET"]
                        },
                    )
                    for _ in range(12)
                ]
            )
            assert all(r.status_code == 200 for r in webhooks), [r.text for r in webhooks]
            assert {r.headers["X-Upstream"] for r in webhooks} == {f"{ip}:8001" for ip in bots}
            assert await confirmations(client, bots) == before + (0 if verify_saved else 1)
            products = (await client.get(f"{proxy}/api/products")).json()
            matches = [p for p in products if p["title"] == update["message"]["caption"]]
            assert len(matches) == 1, matches
            if verify_saved:
                assert matches[0]["id"] == saved["webhook_product"]["id"]
            assert (await client.get(proxy + matches[0]["image_url"])).content == image.getvalue()
            refreshed = await client.get(proxy)
            assert refreshed.status_code == 200
            assert update["message"]["caption"] in refreshed.text
            assert "no-store" in refreshed.headers.get("Cache-Control", "")
            if not verify_saved:
                assert update["message"]["caption"] not in warmed_page.text
                await asyncio.to_thread(
                    state_file.write_text,
                    json.dumps(
                        {
                            "payload": payload,
                            "product": product,
                            "update": update,
                            "webhook_product": matches[0],
                        }
                    ),
                )
        print(
            f"PASS: {len(backends)} backend replicas, {len(bots)} bot replicas; "
            f"12 concurrent requests -> one product #{product['id']}; "
            f"upstreams API={sorted(api_upstreams)}, bot={sorted(webhook_upstreams)}; "
            f"image and warmed Next.js verified; mock={telegram_mock or verify_saved}; "
            f"replay after recreation={verify_saved}."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram-mock", action="store_true", help="CI test transport only")
    parser.add_argument("--verify-saved", action="store_true", help="Replay the CI fixture")
    args = parser.parse_args()
    asyncio.run(main(args.telegram_mock, args.verify_saved))
