import asyncio

from app.core.llm import MockLLM


def test_mock_llm_refuses_empty_context() -> None:
    assert "未找到" in asyncio.run(MockLLM().answer("问题", ""))


def test_mock_llm_returns_answer_with_context() -> None:
    assert "问题" in asyncio.run(MockLLM().answer("问题", "证据"))
