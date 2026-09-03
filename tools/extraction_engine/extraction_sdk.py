import aiohttp
from dataclasses import dataclass
from typing import Optional
import asyncio


@dataclass
class EditConfig:
    tone: str = "neutral"
    length: str = "medium"
    audience: str = "general"


class ExtractionClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base = base_url

    async def extract(self, text: str, schema: dict) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base}/extract", json={"content": text, "schema_definition": schema}) as r:
                return await r.json()

    async def rewrite(self, text: str, config: EditConfig) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base}/edit/rewrite", json={"content": text, "style": config.__dict__}) as r:
                return await r.json()


async def main():
    client = ExtractionClient()
    result = await client.rewrite(
        "Draft blog post here...",
        EditConfig(tone="professional", length="concise")
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
