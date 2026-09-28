"""Generate the v2 golden set: paraphrased, multi-hop, equivalent and unanswerable.

v1's prompt *required* the question to name the regulation, and the name appears verbatim
in the first page of every document - that single wording rule handed the retriever a
near-unique lexical clue and inflated Recall@1 by roughly 11.5 percentage points (see
docs/evaluation-report.md). v2 reverses the rule, then adds the three question types the
v1 schema could not even express:

  paraphrase   同义改写   everyday wording, no 《》 and no document title anywhere
  multi_hop    多跳       one question that needs two documents, one hop each
  equivalence  等价多标签 the same sentence written into several documents
  refusal      应拒答     plausible questions this corpus genuinely cannot answer

Every generated label is re-checked against the raw pages; whatever fails is dropped
rather than written out, and the dropped count is printed.
"""

import argparse
import asyncio
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.ingestion import SUPPORTED_SUFFIXES, extract_text
from app.core.llm import create_llm
from scripts.generate_golden_set import normalize, parse_json_array

TITLE_PATTERN = re.compile(r"《[^》]+》")

# Every question the generator threw away, with the reason. Written next to the golden set
# so a shortfall ("this document produced nothing") is explainable instead of mysterious.
REJECTED: list[dict[str, Any]] = []


def reject(category: str, question: str, reason: str, source: str = "") -> None:
    REJECTED.append(
        {"category": category, "source_filename": source, "question": question, "reason": reason}
    )

SENTENCE_SPLIT = re.compile(r"[。；！？]")
DEFINITION_MARKERS = ("所称", "是指", "本办法所称")
# Shared boilerplate makes a question with several correct answers, not a hard one.
BOILERPLATE_MARKERS = ("施行", "现予公布", "废止", "自20", "自发布之日起", "本通知", "特此")

# A de-leaked question about the effective date asks the same thing of every document in the
# corpus ("这份规定从哪天生效？"), so the expected document is arbitrary even though the
# quote verifies. Reject at question level, not quote level.
BOILERPLATE_QUESTION_MARKERS = ("生效", "施行", "发布日期", "公布日期", "发布之日")
# Article and list markers that PDF extraction leaves at the front of a sentence.
LEADING_MARKER = re.compile(r"^(?:第[一二三四五六七八九十百]+条(?:\s*第[一二三四五六七八九十百]+条)*|（[一二三四五六七八九十]+）|\d+[.、])")
# PDF extraction breaks lines mid-sentence, so a definition arrives as several fragments.
CONTINUED_LINE = re.compile(r"(?<![。；！？：])\n(?!\s*(?:第[一二三四五六七八九十百]+条|（[一二三四五六七八九十]+）|\d+\.))")

PAIRS: tuple[dict[str, Any], ...] = (
    {
        "left": "农村土地承包法",
        "right": "农村集体经济组织法",
        "keywords": ("承包", "成员", "集体"),
        "topic": "承包经营权与集体经济组织成员资格的关系",
    },
    {
        "left": "农村土地承包法",
        "right": "民法典",
        "keywords": ("承包期", "流转", "用益物权", "土地经营权"),
        "topic": "承包期、土地经营权流转与用益物权",
    },
    {
        "left": "农村集体经济组织法",
        "right": "民法典",
        "keywords": ("特别法人", "成员", "集体财产"),
        "topic": "集体经济组织的法人地位与成员权利",
    },
    {
        "left": "固定资产贷款管理办法",
        "right": "流动资金贷款管理办法",
        "keywords": ("期限", "受托支付", "贷款用途", "贷款人"),
        "topic": "两类贷款在期限、用途与支付管理上的差别",
    },
    {
        "left": "个人贷款管理办法",
        "right": "农户贷款管理办法",
        "keywords": ("个人贷款", "农户", "用途", "期限"),
        "topic": "个人贷款与农户贷款的界定",
    },
    {
        "left": "商业银行法",
        "right": "个人贷款管理办法",
        "keywords": ("借款人", "贷款", "商业银行"),
        "topic": "借款人与贷款人的权利义务",
    },
    {
        "left": "乡村振兴促进法",
        "right": "农村土地承包法",
        "keywords": ("土地经营权", "流转", "承包"),
        "topic": "土地经营权流转的政策与法律依据",
    },
    {
        "left": "小额贷款公司监督管理暂行办法",
        "right": "关于加强小额贷款公司监督管理的通知",
        "keywords": ("小额贷款公司", "监督管理", "融资"),
        "topic": "小额贷款公司监管要求的演进",
    },
    {
        "left": "政府性融资担保发展管理办法",
        "right": "固定资产贷款管理办法",
        "keywords": ("担保", "贷款", "风险"),
        "topic": "担保安排与固定资产贷款要求",
    },
    {
        "left": "民法典",
        "right": "商业银行法",
        "keywords": ("借款合同", "利息", "贷款"),
        "topic": "借款合同的订立与履行",
    },
)

REFUSAL_TOPICS = (
    "私募基金或券商资管产品的杠杆比例与结构化安排",
    "保险资金运用比例与偿付能力监管指标",
    "上市公司信息披露与内幕交易认定的具体标准",
    "网络借贷信息中介机构的备案与退出流程",
    "虚拟货币交易平台的牌照与反洗钱义务",
    "公募基金申购赎回费率与销售适当性细则",
    "银行理财产品的净值化估值与信息披露要求",
    "融资租赁公司的租赁物范围与集中度指标",
)


def document_titles(paths: list[Path]) -> list[str]:
    """Titles to keep out of a question: the regulation name inside each filename.

    Filenames look like ``05_现行核心_国家金融监督管理总局_固定资产贷款管理办法_2024.pdf``,
    so the regulation is the second-to-last segment - not the issuing body one segment earlier.
    """
    titles: list[str] = []
    for path in paths:
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        title = re.sub(r"^(中华人民共和国)+", "", parts[-2]).strip()
        if len(title) >= 3:
            titles.append(title)
    return sorted(set(titles), key=len, reverse=True)


def sentences_in_page(text: str) -> list[str]:
    """Split a page into sentences after re-joining the line breaks PDF extraction adds."""
    joined = CONTINUED_LINE.sub("", text)
    cleaned = []
    for sentence in SENTENCE_SPLIT.split(joined):
        stripped = LEADING_MARKER.sub("", sentence.strip()).strip()
        if stripped:
            cleaned.append(stripped)
    return cleaned


def mentions_a_title(question: str, titles: list[str]) -> str | None:
    """Return what leaked, so the caller can report the reason a question was dropped."""
    bracketed = TITLE_PATTERN.search(question)
    if bracketed:
        return bracketed.group(0)
    for title in titles:
        if title in question:
            return title
    return None


def asks_for_boilerplate(question: str) -> bool:
    """True for the de-leaked question every document in the corpus could answer."""
    return any(marker in question for marker in BOILERPLATE_QUESTION_MARKERS)


def shared_with_another_document(quote: str, source: str, corpus_texts: dict[str, str]) -> bool:
    """True when the same sentence also appears elsewhere: the document label then collides."""
    needle = normalize(quote)
    return any(needle in text for name, text in corpus_texts.items() if name != source)


def select_pages(
    pages: list[tuple[int, str]],
    keywords: tuple[str, ...],
    limit: int = 3,
) -> list[tuple[int, str]]:
    """Keep the prompts small: the first page plus the keyword-heaviest pages."""
    scored = [
        (sum(text.count(keyword) for keyword in keywords), page, text)
        for page, text in pages
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = [page for _score, page, _text in scored[:limit]]
    if pages and pages[0][0] not in chosen:
        chosen.append(pages[0][0])
    ordered = sorted(set(chosen))
    by_page = dict(pages)
    return [(page, by_page[page]) for page in ordered if page in by_page]


def page_context(pages: list[tuple[int, str]], label: str) -> str:
    body = "\n\n".join(f"[{label} PAGE {page}]\n{text}" for page, text in pages)
    return f"{label}:\n{body}"


def valid_quote(quote: str, page: int, page_map: dict[int, str]) -> bool:
    return bool(quote) and page in page_map and normalize(quote) in normalize(page_map[page])


async def generate_paraphrases(
    file: Path,
    llm,
    count: int,
    titles: list[str],
    attempts: int = 3,
    category: str = "同义改写",
    corpus_texts: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    pages = extract_text(file.name, file.read_bytes())
    page_map = dict(pages)
    context = "\n\n".join(f"[PAGE {page}]\n{text}" for page, text in pages)
    # Extraction collapses single-page formats to one page (docx always, and HTML or
    # spreadsheet exports too). Saying so stops the model from inventing page numbers
    # that then fail verification.
    page_rule = (
        "该文件解析后只有一页，所有 page 必须写 1。"
        if len(pages) == 1
        else "page 必须来自文本中的 [PAGE n] 标记。"
    )
    kept: list[dict[str, Any]] = []
    dropped = 0
    leaked: list[str] = []
    for attempt in range(attempts):
        shortfall = count - len(kept)
        if shortfall <= 0:
            break
        prompt = f"""
你是金融合规评测集设计员。请依据文档《{file.name}》（不要在任何问题里写出这个文件名或它的法规名）
生成 {shortfall} 道独立、明确、可以用原文直接回答的问题。

要求：
1. 仅输出 JSON 数组，不要输出 Markdown。
2. 每题包含 question、expected_answer、page、evidence_quote、category。
3. {page_rule}
4. evidence_quote 必须是对应页面中的连续原文，不超过 100 个汉字。
5. **禁止出现书名号《》**，禁止写出任何法规、办法、法律的名称（含简称、含发文机关名），
   也不要写“本办法”“该文件”“上述规定”。用日常指代（“这份规定”“一家放贷的银行”）。
6. 必须用日常说法提问，做同义改写：例如用“借多久”代替“贷款期限”、
   用“谁来还钱”代替“借款人的还款义务”、用“能不能转给别人”代替“转让条件”。
7. 问题仍需唯一指向一个答案，不要出现“可能”“大概”“哪些方面”这类含糊说法。
8. 不得与已生成的问题重复：{json.dumps([item['question'] for item in kept], ensure_ascii=False)}
9. 不要问生效日期、施行日期、发布日期这类**每份文件都有**的样板信息。
10. evidence_quote 必须只在本文档出现；不要引用在其他文件里也会出现的通用条款。
{leak_feedback(leaked)}
JSON 示例：
[
  {{
    "question": "一家给农户放款的银行，放款前必须先做什么？",
    "expected_answer": "应当先对借款人的信用状况和还款能力进行调查。",
    "page": 3,
    "evidence_quote": "……",
    "category": "同义改写"
  }}
]

文档正文：
{context}
"""
        response = await llm.answer("请按指定 JSON 结构生成评测题目。", prompt)
        for item in parse_json_array(response):
            page = int(item.get("page", 0))
            quote = str(item.get("evidence_quote", ""))
            question = str(item.get("question", "")).strip()
            leak = mentions_a_title(question, titles)
            if not question or leak or not valid_quote(quote, page, page_map):
                dropped += 1
                reason = (
                    f"问题里出现了“{leak}”"
                    if leak
                    else ("引文不在所标页码" if question else "缺少问题文本")
                )
                reject(category, question, reason, file.name)
                if leak:
                    leaked.append(leak)
                continue
            if asks_for_boilerplate(question):
                dropped += 1
                reject(
                    category,
                    question,
                    "问题在问生效/发布日期这类每份文件都有的样板信息，去泄漏后指向不唯一",
                    file.name,
                )
                continue
            if corpus_texts and shared_with_another_document(quote, file.name, corpus_texts):
                dropped += 1
                reject(
                    category,
                    question,
                    "引文在语料其他文档里也出现，文档级标签不可区分",
                    file.name,
                )
                continue
            if any(item["question"] == question for item in kept):
                continue
            kept.append(
                {
                    "question": question,
                    "expected_answer": str(item.get("expected_answer", "")).strip(),
                    "page": page,
                    "category": category,
                    "evidence_quote": quote,
                    "source_filename": file.name,
                }
            )
    return kept[:count], dropped


def leak_feedback(leaked: list[str]) -> str:
    """Show the model what it leaked, so a retry is a correction rather than a reroll."""
    if not leaked:
        return ""
    listing = "、".join(sorted(set(leaked)))
    return (
        f"\n上一轮有问题的提问里出现了这些名称：{listing}。"
        "这次一个字都不能出现，请彻底改写问法。\n"
    )


async def generate_multi_hop(
    left: Path,
    right: Path,
    llm,
    keywords: tuple[str, ...],
    topic: str,
    titles: list[str],
    attempts: int = 3,
) -> tuple[dict[str, Any] | None, int]:
    left_pages = select_pages(extract_text(left.name, left.read_bytes()), keywords)
    right_pages = select_pages(extract_text(right.name, right.read_bytes()), keywords)
    left_map = dict(left_pages)
    right_map = dict(right_pages)
    feedback = ""
    dropped = 0
    for _attempt in range(attempts):
        prompt = f"""
你是金融合规评测集设计员。下面是两份文件的节选，主题是「{topic}」。

请生成 1 道**必须同时参考两份文件才能回答**的问题，并给出两处证据。

要求：
1. 仅输出 JSON 对象，不要输出 Markdown，不要输出解释。
2. 字段：question、expected_answer、hops（数组，恰好 2 项）。
3. 每个 hop 包含 side（"left" 或 "right"）、page、evidence_quote；两跳必须一侧一个。
4. page 必须是该 side 文本里 [LEFT PAGE n] / [RIGHT PAGE n] 中的 n；
   evidence_quote 必须是那一页的连续原文，不超过 100 个汉字，且逐字可查。
5. 禁止出现书名号《》，禁止写出任何法规、办法、法律的名称（含简称）。
6. 只靠其中任意一份文件都回答不了这个问题；答案需要把两边的要求合并起来。
7. 问题必须唯一指向一个答案，不要含糊；expected_answer 用 1 至 3 句话，
   不得补充原文之外的信息。
{feedback}
JSON 示例：
{{
  "question": "…",
  "expected_answer": "…",
  "hops": [
    {{"side": "left", "page": 2, "evidence_quote": "…"}},
    {{"side": "right", "page": 5, "evidence_quote": "…"}}
  ]
}}

{page_context(left_pages, "LEFT")}

{page_context(right_pages, "RIGHT")}
"""
        response = await llm.answer("请按指定 JSON 结构生成一道多跳问题。", prompt)
        payload = parse_json_object(response)
        if not payload:
            dropped += 1
            feedback = "\n上一轮没有输出可解析的 JSON 对象，请严格按示例输出。\n"
            continue
        question = str(payload.get("question", "")).strip()
        hops = payload.get("hops") or []
        if not question:
            dropped += 1
            feedback = "\n上一轮缺少 question 字段。\n"
            continue
        leak = mentions_a_title(question, titles)
        if leak:
            dropped += 1
            feedback = f"\n上一轮的问题里出现了“{leak}”，这个名称一个字都不许出现。\n"
            continue
        if len(hops) != 2:
            dropped += 1
            feedback = "\n上一轮 hops 不是恰好 2 项，请给出一侧一项。\n"
            continue
        resolved: list[dict[str, Any]] = []
        sides = Counter()
        failure = ""
        for hop in hops:
            side = str(hop.get("side", "")).lower()
            page = int(hop.get("page", 0))
            quote = str(hop.get("evidence_quote", ""))
            if side == "left":
                page_map, source = left_map, left
            elif side == "right":
                page_map, source = right_map, right
            else:
                failure = "hop 的 side 必须是 left 或 right"
                break
            if not valid_quote(quote, page, page_map):
                failure = f"{side} 的引文在第 {page} 页找不到，必须逐字来自所标页码"
                break
            sides[side] += 1
            resolved.append(
                {
                    "source_filename": source.name,
                    "page": page,
                    "evidence_quote": quote,
                }
            )
        if failure or sides != Counter({"left": 1, "right": 1}):
            dropped += 1
            reject("多跳", question, failure or "两跳来自同一侧", f"{left.name} + {right.name}")
            feedback = f"\n上一轮未通过校验：{failure or '两跳来自同一侧'}。请重来。\n"
            continue
        return (
            {
                "question": question,
                "expected_answer": str(payload.get("expected_answer", "")).strip(),
                "category": "多跳",
                "evidence_mode": "all",
                "expected_evidence": resolved,
                "topic": topic,
            },
            0,
        )
    return None, dropped


def parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def duplicated_sentences(
    corpus: dict[str, list[tuple[int, str]]],
    minimum: int = 15,
    maximum: int = 200,
) -> list[dict[str, Any]]:
    """Sentences that appear verbatim in more than one document.

    These are the label-equivalence cases: several regulations carry the same definition,
    so a question about it has more than one correct answer. A corpus-wide scan finds them
    far more reliably than asking a model to remember what it has seen.

    Definitional sentences are ranked first and flagged; publication boilerplate is dropped
    outright because "when does this take effect" has a different answer per document, which
    is a broken evaluation item rather than a hard one.
    """
    seen: dict[str, list[dict[str, Any]]] = {}
    for filename, pages in corpus.items():
        for page, text in pages:
            for sentence in sentences_in_page(text):
                if not minimum <= len(sentence) <= maximum:
                    continue
                if any(marker in sentence for marker in BOILERPLATE_MARKERS):
                    continue
                seen.setdefault(normalize(sentence), []).append(
                    {"source_filename": filename, "page": page, "evidence_quote": sentence}
                )
    shared: list[dict[str, Any]] = []
    for occurrences in seen.values():
        documents = {entry["source_filename"] for entry in occurrences}
        if len(documents) < 2:
            continue
        deduped: dict[str, dict[str, Any]] = {}
        for entry in occurrences:
            deduped.setdefault(entry["source_filename"], entry)
        hops = list(deduped.values())
        sentence = hops[0]["evidence_quote"]
        shared.append(
            {
                "sentence": sentence,
                "hops": hops,
                "definitional": any(marker in sentence for marker in DEFINITION_MARKERS),
            }
        )
    shared.sort(key=lambda item: (not item["definitional"], -len(item["hops"]), -len(item["sentence"])))
    return shared


async def generate_equivalence(
    candidate: dict[str, Any],
    llm,
    titles: list[str],
    corpus_paths: dict[str, Path],
) -> dict[str, Any] | None:
    documents = ", ".join(hop["source_filename"] for hop in candidate["hops"])
    prompt = f"""
你是金融合规评测集设计员。下面这句话逐字出现在多份文件中：{documents}

原句：{candidate["sentence"]}

请生成 1 道可以用这句话直接回答的问题。

要求：
1. 仅输出 JSON 对象，不要输出 Markdown：字段为 question、expected_answer。
2. 禁止出现书名号《》，禁止写出任何法规、办法、法律的名称（含简称），
   也不要写“本办法”“该文件”。
3. 必须用日常说法提问，做同义改写，但答案必须仍然是上面那句话。
4. 不要暗示答案只存在于某一份文件中。
"""
    response = await llm.answer("请按指定 JSON 结构生成一道问题。", prompt)
    payload = parse_json_object(response)
    if not payload:
        return None
    question = str(payload.get("question", "")).strip()
    if not question or mentions_a_title(question, titles):
        return None
    hops = [dict(hop) for hop in candidate["hops"] if hop["source_filename"] in corpus_paths]
    if len(hops) < 2:
        return None
    return {
        "question": question,
        "expected_answer": str(payload.get("expected_answer", candidate["sentence"])).strip(),
        "category": "等价多标签",
        "evidence_mode": "any",
        "expected_evidence": hops,
        "note": "同一句话在多个文件中逐字出现，任一份都算正确答案",
    }


async def generate_refusals(llm, corpus_titles: list[str], count: int) -> list[dict[str, Any]]:
    titles = "、".join(corpus_titles)
    adjacent = "\n".join(f"- {topic}" for topic in REFUSAL_TOPICS)
    prompt = f"""
你是金融合规评测集设计员。某个知识库只收录了这些文件：{titles}。

请生成 {count} 道**该知识库无法回答**的中文金融问题，用来测试系统是否会如实说明查不到。
可以从这些相邻领域里取材（也可以另想）：

{adjacent}

要求：
1. 仅输出 JSON 数组，不要输出 Markdown。
2. 每题包含 question、why_unanswerable、category（固定为“应拒答”）。
3. 问题必须是真实用户会问的、听起来和这个知识库主题相邻的问题。
4. 但答案确实不在上面的文件里：涉及其他监管领域、其他金融产品，或尚未出台的规定。
5. 不要出现书名号《》，也不要把“无法回答”“查不到”写进问题里。
6. 直接写问题本身，不要写“请根据以上文件回答”之类的引导语。

JSON 示例：
[
  {{"question": "保险资金投资股票的集中度上限是多少？",
    "why_unanswerable": "知识库只收录银行信贷与农村土地类文件，不含保险资金运用规定。",
    "category": "应拒答"}}
]
"""
    response = await llm.answer("请按指定 JSON 结构生成题目。", prompt)
    kept: list[dict[str, Any]] = []
    for item in parse_json_array(response):
        question = str(item.get("question", "")).strip()
        if not question or TITLE_PATTERN.search(question):
            continue
        kept.append(
            {
                "question": question,
                "category": "应拒答",
                "should_refuse": True,
                "why_unanswerable": str(item.get("why_unanswerable", "")).strip(),
            }
        )
    return kept[:count]


def selected(path: Path, wanted: list[str]) -> bool:
    """Match by numeric filename prefix (`14`) or by a whole name segment (`中华人民共和国民法典`).

    Whole segments rather than substrings: `15` must not select a file whose year is `_2015`.
    """
    segments = path.stem.split("_")
    return segments[0] in wanted or any(token in segments for token in wanted)


def find_document(paths: list[Path], keyword: str) -> Path:
    matches = [path for path in paths if keyword in path.stem]
    if not matches:
        raise SystemExit(f"no corpus file matches {keyword!r}")
    return min(matches, key=lambda path: len(path.stem))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--law-dir", default="law")
    parser.add_argument("--output", default="law/golden_eval_v2.json")
    parser.add_argument("--per-document", type=int, default=4)
    parser.add_argument("--multi-hop", type=int, default=10)
    parser.add_argument("--equivalence", type=int, default=6)
    parser.add_argument("--refusal", type=int, default=8)
    parser.add_argument("--types", default="paraphrase,multi_hop,equivalence,refusal")
    parser.add_argument(
        "--documents",
        default="",
        help=(
            "restrict question generation to these files: comma-separated filename prefixes "
            "(e.g. 14,15,18) or whole name segments (e.g. 中华人民共和国民法典); empty means "
            "every file in --law-dir"
        ),
    )
    parser.add_argument("--dataset", default="rural-finance-regulations-golden-v2")
    args = parser.parse_args()

    settings = get_settings()
    llm = create_llm(settings)
    law_dir = Path(args.law_dir)
    corpus_files = sorted(
        path
        for path in law_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    # The title list and the equivalence scan must see the whole corpus, so --documents
    # only narrows which files get questions of their own.
    document_filter = [token.strip() for token in args.documents.split(",") if token.strip()]
    files = (
        [path for path in corpus_files if selected(path, document_filter)]
        if document_filter
        else corpus_files
    )
    if document_filter and not files:
        raise SystemExit(f"no file under {law_dir} matches --documents {args.documents!r}")
    titles = document_titles(corpus_files)
    wanted = {name.strip() for name in args.types.split(",") if name.strip()}
    examples: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()

    if "paraphrase" in wanted:
        # Normalized full text of every file: a quote that also lives in another document makes
        # the expected-document label ambiguous, so those questions are rejected up front.
        corpus_texts = {
            path.name: normalize(
                "\n".join(text for _page, text in extract_text(path.name, path.read_bytes()))
            )
            for path in corpus_files
        }
        for file in files:
            # Spreadsheets answer "which row" questions, not "which rule" questions; keep
            # them in their own category so the two kinds never average together.
            category = "测算表" if file.suffix.lower() == ".xlsx" else "同义改写"
            generated, failed = await generate_paraphrases(
                file,
                llm,
                args.per_document,
                titles,
                category=category,
                corpus_texts=corpus_texts,
            )
            dropped["paraphrase"] += failed
            examples.extend(generated)
            print(f"paraphrase {file.name}: kept {len(generated)} dropped {failed}")

    if "multi_hop" in wanted:
        for pair in PAIRS[: args.multi_hop]:
            left = find_document(corpus_files, pair["left"])
            right = find_document(corpus_files, pair["right"])
            example, failed = await generate_multi_hop(
                left, right, llm, pair["keywords"], pair["topic"], titles
            )
            dropped["multi_hop"] += failed
            if example:
                examples.append(example)
                print(f"multi_hop {pair['left']} + {pair['right']}: ok")
            else:
                print(f"multi_hop {pair['left']} + {pair['right']}: dropped")

    if "equivalence" in wanted:
        corpus = {
            path.name: extract_text(path.name, path.read_bytes()) for path in corpus_files
        }
        candidates = duplicated_sentences(corpus)
        definitional = [candidate for candidate in candidates if candidate["definitional"]]
        print(
            f"sentences shared by more than one file: {len(candidates)} "
            f"(definitional: {len(definitional)})"
        )
        by_name = {path.name: path for path in corpus_files}
        # Definitional sentences first, then the substantive shared requirements; both are
        # legitimate equivalence labels, while publication boilerplate was already dropped.
        pool = definitional + [c for c in candidates if not c["definitional"]]
        for candidate in pool[: args.equivalence]:
            example = await generate_equivalence(candidate, llm, titles, by_name)
            if example:
                examples.append(example)
                print(f"equivalence kept: {candidate['sentence'][:24]}…")
            else:
                dropped["equivalence"] += 1

    if "refusal" in wanted:
        refusals = await generate_refusals(llm, titles, args.refusal)
        examples.extend(refusals)
        print(f"refusal kept: {len(refusals)}")

    payload = {
        "dataset": args.dataset,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "scripts/generate_golden_set_v2.py",
        "counts": Counter(example["category"] for example in examples),
        "corpus": [path.name for path in corpus_files],
        "question_documents": [path.name for path in files],
        "examples": examples,
    }
    output = Path(args.output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"golden v2 examples: {len(examples)} -> {output}")
    print(f"dropped after verification: {dict(dropped)}")
    reasons = Counter(item["reason"] for item in REJECTED)
    print(f"rejection reasons: {dict(reasons)}")
    rejected_path = output.with_suffix(".rejected.json")
    rejected_path.write_text(
        json.dumps(REJECTED, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"rejected questions (with reasons): {rejected_path}")


if __name__ == "__main__":
    asyncio.run(main())
