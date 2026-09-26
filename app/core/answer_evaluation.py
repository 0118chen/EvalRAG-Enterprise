"""LLM-based answer quality scoring for golden evaluation runs."""

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.ingestion import Chunk
from app.core.llm import LLM


@dataclass(frozen=True)
class AnswerScore:
    correctness: float
    faithfulness: float
    completeness: float
    reason: str


def parse_judge_response(response: str) -> AnswerScore:
    stripped = response.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return AnswerScore(0.0, 0.0, 0.0, "judge response was not valid JSON")
    try:
        payload = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return AnswerScore(0.0, 0.0, 0.0, "judge response was not valid JSON")

    def score(name: str) -> float:
        try:
            return min(1.0, max(0.0, float(payload.get(name, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    return AnswerScore(
        correctness=score("correctness"),
        faithfulness=score("faithfulness"),
        completeness=score("completeness"),
        reason=str(payload.get("reason", ""))[:1000],
    )


async def judge_answer(
    llm: LLM,
    *,
    question: str,
    expected_answer: str,
    generated_answer: str,
    evidence: list[tuple[Chunk, float]],
) -> AnswerScore:
    context: dict[str, Any] = {
        "instruction": (
            "比较 system_answer 与 standard_answer，并仅依据 evidence 判断忠实度。"
            "correctness、faithfulness、completeness 取 0 到 1。"
            "只输出 JSON："
            '{"correctness":0,"faithfulness":0,"completeness":0,"reason":""}'
        ),
        "question": question,
        "standard_answer": expected_answer,
        "system_answer": generated_answer,
        "evidence": [
            {
                "document_id": chunk.document_id,
                "page": chunk.page,
                "text": chunk.text,
            }
            for chunk, _ in evidence
        ],
    }
    response = await llm.answer(
        "请作为金融合规评测裁判，严格按照指定 JSON 输出评分。",
        json.dumps(context, ensure_ascii=False),
    )
    return parse_judge_response(response)
