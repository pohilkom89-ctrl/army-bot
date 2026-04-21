# TODO: подключим когда будут клиенты с запросом на голос.
# Варианты: Groq (бесплатно, US) или Yandex SpeechKit (РФ, платно)

import os

import aiohttp
from loguru import logger


async def transcribe_voice(
    file_bytes: bytes, mime: str = "audio/ogg"
) -> str | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                file_bytes,
                filename="voice.ogg",
                content_type=mime,
            )
            data.add_field("model", "openai/whisper-1")
            async with s.post(
                "https://openrouter.ai/api/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data=data,
            ) as r:
                if r.status == 200:
                    result = await r.json()
                    return result.get("text")
                logger.warning(f"Whisper error: {r.status}")
                return None
    except Exception as e:
        logger.exception(f"Voice transcription error: {e}")
        return None
