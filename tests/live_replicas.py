"""Windows fallback: actual OS processes + PostgreSQL + MinIO + Nginx; fake Telegram only.

Requires prepared test DB/storage, built frontend, .tools/nginx-1.28.0/nginx.exe.
Runs with `python -m tests.live_replicas`. Leaves two clearly named test products.
"""

import asyncio
import base64
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    process.wait(timeout=15)


async def main():
    assert make_url(os.environ["DATABASE_URL"]).database == "catalog_test", "Use catalog_test"
    # Windows Nginx cannot read configuration paths containing Cyrillic characters.
    runtime = Path(tempfile.gettempdir()) / "mini-catalog-invix-nginx"
    runtime.mkdir(exist_ok=True)
    config = (ROOT / "infra/nginx.conf").read_text()
    config = config.replace("worker_processes auto;", "worker_processes 1;")
    config = config.replace("    resolver 127.0.0.11 valid=5s ipv6=off;", "")
    for old, new in {
        "server backend:8000 resolve;": "server 127.0.0.1:58000; server 127.0.0.1:58001;",
        "server bot:8001 resolve;": "server 127.0.0.1:58010; server 127.0.0.1:58011;",
        "server frontend:3000 resolve;": "server 127.0.0.1:53000;",
        "listen 8080;": "listen 127.0.0.1:58080;",
        "/tmp/": runtime.as_posix() + "/",
    }.items():
        config = config.replace(old, new)
    # Separate internal listener allows bot POSTs while the public /api stays read-only.
    config = config.replace(
        "    server {",
        "    server {\n"
        "        listen 127.0.0.1:58081;\n"
        "        client_max_body_size 8m;\n"
        "        location / { proxy_request_buffering off; proxy_pass http://api; }\n"
        "    }\n    server {",
        1,
    )
    config_path = runtime / "nginx.conf"
    config_path.write_text(config)
    (runtime / "logs").mkdir(exist_ok=True)
    env = os.environ | {
        "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        "TELEGRAM_ADMIN_ID": "42",
        "TELEGRAM_WEBHOOK_SECRET": "s" * 40,
        "MINI_APP_URL": "https://catalog.example.com",
        "BACKEND_URL": "http://127.0.0.1:58081",
        "PORT": "53000",
        "HOSTNAME": "127.0.0.1",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    commands = (
        [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
            for port in [58000, 58001]
        ]
        + [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.replica_bot:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
            for port in [58010, 58011]
        ]
        + [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.telegram_stub:app",
                "--host",
                "127.0.0.1",
                "--port",
                "58090",
            ],
            ["node", "frontend/.next/standalone/server.js"],
            [
                str(ROOT / ".tools/nginx-1.28.0/nginx.exe"),
                "-p",
                str(runtime) + "/",
                "-c",
                str(config_path),
            ],
        ]
    )
    processes, logs = [], []
    try:
        for i, command in enumerate(commands):
            log = (runtime / f"replica-{i}.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(  # noqa: ASYNC220 -- test setup, before any network requests
                    command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
                )
            )
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            proxy = "http://127.0.0.1:58080"
            last_error = "No response"
            for _ in range(100):
                assert all(p.poll() is None for p in processes), f"Service exited; logs: {runtime}"
                try:
                    readiness = await client.get(f"{proxy}/health", timeout=2)
                    bot_readiness = await asyncio.gather(
                        *[
                            client.get(f"http://127.0.0.1:{port}/health", timeout=2)
                            for port in [58010, 58011]
                        ]
                    )
                    if readiness.status_code == 200 and all(
                        r.status_code == 200 for r in bot_readiness
                    ):
                        break
                    last_error = f"HTTP {readiness.status_code}: {readiness.text[:200]}"
                except httpx.HTTPError as exc:
                    last_error = repr(exc)
                await asyncio.sleep(0.2)
            else:
                raise AssertionError(
                    f"Live services did not become ready: {last_error}; logs: {runtime}"
                )
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), "white").save(buffer, "PNG")
            update_id = time.time_ns() // 1_000_000
            payload = {
                "telegram_update_id": update_id,
                "title": f"Реплики {update_id}",
                "image_base64": base64.b64encode(buffer.getvalue()).decode(),
            }
            warmed_page = await client.get(proxy)
            assert warmed_page.status_code == 200
            assert payload["title"] not in warmed_page.text
            assert "no-store" in warmed_page.headers.get("Cache-Control", "")
            responses = await asyncio.gather(
                *[
                    client.post(
                        f"http://127.0.0.1:{58000 + i % 2}/products",
                        json=payload,
                        headers={"X-Bot-Secret": env["BOT_API_SECRET"]},
                    )
                    for i in range(12)
                ]
            )
            assert sorted(r.status_code for r in responses) == [200] * 11 + [201], [
                r.text for r in responses
            ]
            assert len({r.json()["product"]["id"] for r in responses}) == 1
            product = responses[0].json()["product"]
            assert (await client.get(proxy + product["image_url"])).content == buffer.getvalue()
            html = (await client.get(proxy)).text
            assert payload["title"] in html
            api_upstreams = {
                (await client.get(f"{proxy}/api/products")).headers["X-Upstream"] for _ in range(12)
            }
            update = {
                "update_id": update_id + 1,
                "message": {
                    "message_id": 1,
                    "date": int(time.time()),
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                    "caption": f"Через бота {update_id}",
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
            webhooks = await asyncio.gather(
                *[
                    client.post(
                        f"{proxy}/telegram/webhook",
                        json=update,
                        headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 40},
                    )
                    for _ in range(12)
                ]
            )
            assert all(r.status_code == 200 for r in webhooks), [r.text for r in webhooks]
            webhook_upstreams = {r.headers["X-Upstream"] for r in webhooks}
            assert len(api_upstreams) == len(webhook_upstreams) == 2
            assert (await client.get("http://127.0.0.1:58090/stats")).json()["confirmations"] == 1
            products = (await client.get(f"{proxy}/api/products")).json()
            assert sum(p["title"] == update["message"]["caption"] for p in products) == 1
            refreshed_page = await client.get(proxy)
            assert update["message"]["caption"] not in html
            assert update["message"]["caption"] in refreshed_page.text
            assert "no-store" in refreshed_page.headers.get("Cache-Control", "")
            # Process death must leave persisted catalog data usable through the other replica.
            await asyncio.to_thread(stop_process, processes[0])
            remaining = await client.get("http://127.0.0.1:58001/products")
            assert any(p["id"] == product["id"] for p in remaining.json())
            print(
                f"PASS: two backend processes {sorted(api_upstreams)}; "
                f"two bot processes {sorted(webhook_upstreams)}; 12 concurrent API requests "
                "and 12 webhooks each create one product; one Telegram confirmation; "
                "real MinIO image; warmed Next.js page refreshed after creation; "
                "backend process death survived on the directly queried surviving replica."
            )
    finally:
        for process in reversed(processes):
            await asyncio.to_thread(stop_process, process)
        for log in logs:
            log.close()


if __name__ == "__main__":
    asyncio.run(main())
