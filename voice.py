import asyncio
import io
import os
import tempfile
import edge_tts

ALYA_VOICE = "ja-JP-NanamiNeural"


async def synthesize(text: str) -> bytes:
    communicate = edge_tts.Communicate(text, ALYA_VOICE, rate="-25%", pitch="+3Hz")
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    buf.seek(0)
    return buf.read()
