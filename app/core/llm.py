"""Provider-neutral LLM adapter for cloud and local OpenAI-compatible endpoints."""

from dataclasses import dataclass
from typing import Protocol

import httpx


class LLM(Protocol):
    async def answer(self, question: str, context: str) -> str: ...


@dataclass(frozen=True)
class MockLLM:
    """Deterministic provider used by tests and local development without API keys."""

    async def answer(self, question: str, context: str) -> str:
        if not context.strip():
            return "未找到足够依据，无法可靠回答该问题。"
        return f"基于检索证据回答：{question}。"


@dataclass(frozen=True)
class OpenAICompatibleLLM:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 30.0

    async def answer(self, question: str, context: str) -> str:
        payload = {"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": "仅依据提供的证据回答；证据不足时明确说明。"},
            {"role": "user", "content": f"问题：{question}\n证据：{context}"},
        ]}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"])

