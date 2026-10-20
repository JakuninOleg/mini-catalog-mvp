"""Test-only fake Telegram transport; never imported by production services."""

import io

from fastapi import FastAPI, Response
from PIL import Image

app = FastAPI()
confirmations = []
image = io.BytesIO()
Image.new("RGB", (8, 8), "white").save(image, "PNG")


@app.get("/stats")
def stats():
    return {"confirmations": len(confirmations)}


@app.post("/bot{token}/{method}")
async def telegram_method(token: str, method: str):
    if method == "getFile":
        result = {
            "file_id": "photo",
            "file_unique_id": "unique-photo",
            "file_size": len(image.getvalue()),
            "file_path": "photos/photo.png",
        }
    else:
        if method == "sendPhoto":
            confirmations.append(1)
        result = {"message_id": 1, "date": 1700000000, "chat": {"id": 42, "type": "private"}}
    return {"ok": True, "result": result}


@app.get("/file/bot{token}/photos/photo.png")
async def telegram_file(token: str):
    return Response(image.getvalue(), media_type="image/png")
