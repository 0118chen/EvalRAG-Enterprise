import pytest

from app.core.llm import MockLLM


@pytest.mark.asyncio
async def test_mock_llm_refuses_empty_context() -> None:
    assert "未找到" in await MockLLM().answer("问题", "")


@pytest.mark.asyncio
async def test_mock_llm_returns_answer_with_context() -> None:
    assert "问题" in await MockLLM().answer("问题", "证据")

