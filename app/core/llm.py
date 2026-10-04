"""Provider-neutral LLM adapter for cloud and local OpenAI-compatible endpoints."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

LLM_MAX_ATTEMPTS = 3
LLM_BACKOFF_SECONDS = 0.5
# 这些状态码的意思是"同一个请求再发一次可能就成功"：限流、网关抖动、上游瞬时故障。
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMStreamError(RuntimeError):
    """A provider stream failed without exposing provider response details."""


class LLMRequestError(RuntimeError):
    """Every attempt at a non-streaming answer failed; the last cause is attached."""


class LLM(Protocol):
    async def answer(self, question: str, context: str) -> str: ...

    def stream(self, question: str, context: str) -> AsyncIterator[str]: ...


@dataclass(frozen=True)
class MockLLM:
    """Deterministic provider used by tests and local development without API keys."""

    async def answer(self, question: str, context: str) -> str:
        if not context.strip():
            return "未找到足够依据，无法可靠回答该问题。"
        return f"基于检索证据回答：{question}。"

    async def stream(self, question: str, context: str) -> AsyncIterator[str]:
        answer = await self.answer(question, context)
        for token in answer:
            yield token


@dataclass(frozen=True)
class OpenAICompatibleLLM:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 30.0

    def _payload(self, question: str, context: str, *, stream: bool = False) -> dict:
        payload = {"model": self.model, "temperature": 0, "messages": [
            {
                "role": "system",
                "content": (
                    "仅依据提供的证据回答。证据包含直接条款时，必须直接、完整地回答，"
                    "覆盖与问题相关的所有比例、期限、条件、例外和补充规定；"
                    "不得依据外部知识推断文件已经废止或失效；只有证据确实不足时才拒答。"
                ),
            },
            {"role": "user", "content": f"问题：{question}\n证据：{context}"},
        ]}
        if stream:
            payload["stream"] = True
        return payload

    async def answer(self, question: str, context: str) -> str:
        payload = self._payload(question, context)
        last_error: Exception | None = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            try:
                return await self._post_chat(payload)
            except httpx.TransportError as error:
                # 连接被拒/重置、读超时、DNS 失败。实测跑 32 题答案评测时撞过一次
                # ConnectError：整轮 evaluation 直接 failed，而重发一次就成功——评测跑批
                # 几百次调用，把"一次抖动=整轮作废"留给调用方去处理是不合理的。
                last_error = error
                retry_after = None
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise
                last_error = error
                retry_after = error.response.headers.get("Retry-After")
            if attempt == LLM_MAX_ATTEMPTS:
                break
            delay = self._retry_delay(attempt, retry_after)
            logger.warning(
                "llm attempt %d/%d failed (%s), retrying in %.1fs",
                attempt,
                LLM_MAX_ATTEMPTS,
                type(last_error).__name__,
                delay,
            )
            await asyncio.sleep(delay)
        raise LLMRequestError(
            f"LLM request failed after {LLM_MAX_ATTEMPTS} attempts"
        ) from last_error

    async def _post_chat(self, payload: dict) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"])

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        """Honor Retry-After when the provider sends it, else exponential backoff."""
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 30.0))
            except ValueError:
                pass
        return LLM_BACKOFF_SECONDS * 2 ** (attempt - 1)

    async def stream(self, question: str, context: str) -> AsyncIterator[str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client, client.stream(
            "POST",
            f"{self.base_url.rstrip('/')}/chat/completions",
            json=self._payload(question, context, stream=True),
            headers=headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise LLMStreamError("malformed JSON in provider stream") from exc
                if event.get("error"):
                    raise LLMStreamError("provider error in stream")
                choices = event.get("choices") or []
                if not choices:
                    continue
                content = choices[0].get("delta", {}).get("content")
                if content:
                    yield str(content)


def create_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "mock":
        return MockLLM()
    if not settings.llm_api_key:
        raise RuntimeError("LLM provider is not configured")
    return OpenAICompatibleLLM(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
    )
