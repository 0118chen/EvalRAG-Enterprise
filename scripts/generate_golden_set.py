"""Generate a page-grounded golden set from the downloaded rural finance corpus."""

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.ingestion import extract_text
from app.core.llm import create_llm


def parse_json_array(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        raise ValueError("LLM response does not contain a JSON array")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, list):
        raise TypeError("LLM response is not a JSON array")
    return payload


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


async def generate_document(
    file: Path,
    llm,
    questions_per_document: int,
) -> list[dict[str, Any]]:
    pages = extract_text(file.name, file.read_bytes())
    page_map = {page: text for page, text in pages}
    context = "\n\n".join(f"[PAGE {page}]\n{text}" for page, text in pages)
    prompt = f"""
你是金融合规评测集设计员。请依据文档《{file.name}》生成
{questions_per_document} 道独立、明确、可以用原文直接回答的问题。

要求：
1. 仅输出 JSON 数组，不要输出 Markdown。
2. 每题包含 question、expected_answer、page、evidence_quote、category。
3. page 必须来自文本中的 [PAGE n] 标记。
4. evidence_quote 必须是对应页面中的连续原文，不超过 100 个汉字。
5. 问题不得依赖“本办法”“该文件”等模糊指代，必须写出制度名称。
6. 覆盖适用对象、定义、期限、流程、监督管理、法律责任等不同方面。
7. expected_answer 用 1 至 3 句话回答，不得补充原文之外的信息。

JSON 示例：
[
  {{
    "question": "《某办法》适用于哪些机构？",
    "expected_answer": "适用于依法开展某类业务的金融机构。",
    "page": 1,
    "evidence_quote": "本办法适用于……",
    "category": "适用范围"
  }}
]

文档正文：
{context}
"""
    response = await llm.answer(
        "请按指定 JSON 结构生成评测题目。",
        prompt,
    )
    items = parse_json_array(response)
    valid: list[dict[str, Any]] = []
    for item in items:
        page = int(item.get("page", 0))
        quote = str(item.get("evidence_quote", ""))
        page_text = page_map.get(page, "")
        if page not in page_map or not quote or normalize(quote) not in normalize(page_text):
            continue
        valid.append(
            {
                "question": str(item["question"]).strip(),
                "expected_answer": str(item["expected_answer"]).strip(),
                "page": page,
                "category": str(item.get("category", "general")).strip(),
                "evidence_quote": quote,
                "source_filename": file.name,
            }
        )
    return valid


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--law-dir", default="law")
    parser.add_argument("--output", default="law/golden_eval_v1.json")
    parser.add_argument("--questions-per-document", type=int, default=4)
    args = parser.parse_args()

    settings = get_settings()
    llm = create_llm(settings)
    law_dir = Path(args.law_dir)
    examples: list[dict[str, Any]] = []
    for file in sorted(law_dir.iterdir()):
        if not file.is_file() or file.suffix.lower() not in {".pdf", ".docx"}:
            continue
        generated = await generate_document(
            file,
            llm,
            args.questions_per_document,
        )
        examples.extend(generated)
        print(f"{file.name}: {len(generated)}")

    output = Path(args.output)
    output.write_text(
        json.dumps(examples, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"golden examples: {len(examples)}")
    print(f"output: {output}")


if __name__ == "__main__":
    asyncio.run(main())
