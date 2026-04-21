import asyncio
import base64
import json
import os

import aiohttp
from loguru import logger


class ImageGenerator:
    BASE_URL = "https://api-key.fusionbrain.ai/"

    def __init__(self):
        self.api_key = os.getenv("FUSIONBRAIN_API_KEY")
        self.secret_key = os.getenv("FUSIONBRAIN_SECRET_KEY")
        self.enabled = bool(self.api_key and self.secret_key)
        if not self.enabled:
            logger.warning(
                "Fusionbrain ключи не заданы — генерация картинок отключена"
            )

    def _headers(self):
        return {
            "X-Key": f"Key {self.api_key}",
            "X-Secret": f"Secret {self.secret_key}",
        }

    async def get_model_id(self) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{self.BASE_URL}key/api/v1/models",
                headers=self._headers(),
            ) as r:
                data = await r.json()
                return str(data[0]["id"])

    async def generate(
        self,
        prompt: str,
        style: str = "DEFAULT",
        width: int = 1024,
        height: int = 1024,
    ) -> bytes | None:
        if not self.enabled:
            return None
        try:
            model_id = await self.get_model_id()
            async with aiohttp.ClientSession() as s:
                data = aiohttp.FormData()
                data.add_field("model_id", model_id)
                data.add_field(
                    "params",
                    json.dumps(
                        {
                            "type": "GENERATE",
                            "style": style,
                            "width": width,
                            "height": height,
                            "generateParams": {"query": prompt},
                        }
                    ),
                    content_type="application/json",
                )
                async with s.post(
                    f"{self.BASE_URL}key/api/v1/text2image/run",
                    headers=self._headers(),
                    data=data,
                ) as r:
                    result = await r.json()
                    uuid = result["uuid"]

            for _ in range(30):
                await asyncio.sleep(2)
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"{self.BASE_URL}key/api/v1/text2image/status/{uuid}",
                        headers=self._headers(),
                    ) as r:
                        status = await r.json()
                        if status["status"] == "DONE":
                            img_b64 = status["images"][0]
                            return base64.b64decode(img_b64)
            return None
        except Exception as e:
            logger.exception(f"Ошибка генерации картинки: {e}")
            return None


image_generator = ImageGenerator()
