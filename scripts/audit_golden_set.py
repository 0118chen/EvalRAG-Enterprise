"""Audit the golden set against the corpus it is supposed to describe.

Structural checks alone (field present, no duplicates) cannot tell whether a
label is right. The strong check is to re-extract the source documents with the
same parser the ingestion pipeline uses and confirm that every example's
evidence quote really appears on the page the example claims.

Usage:
    python -m scripts.audit_golden_set            # human readable report
    python -m scripts.audit_golden_set --json out.json
"""

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

from app.core.ingestion import extract_text

ROOT = Path(__file__).resolve().parents[1]
LAW_DIR = ROOT / "law"
GOLDEN = LAW_DIR / "golden_eval_v1.json"
BOUND = LAW_DIR / "golden_eval_v2_api.json"
MANIFEST = LAW_DIR / "import-manifest.json"

REQUIRED = ("question", "expected_answer", "page", "category", "evidence_quote", "source_filename")


def normalize(text: str) -> str:
    """Whitespace-insensitive form, so PDF line breaks do not create false misses."""
    return re.sub(r"\s+", "", text)


def load_corpus() -> dict[str, list[tuple[int, str]]]:
    pages: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(LAW_DIR.iterdir()):
        if path.suffix.lower() not in {".pdf", ".docx"}:
            continue
        pages[path.name] = extract_text(path.name, path.read_bytes())
    return pages


def audit() -> dict[str, Any]:
    examples = json.loads(GOLDEN.read_text(encoding="utf-8"))
    bound = json.loads(BOUND.read_text(encoding="utf-8"))["examples"]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    corpus = load_corpus()

    report: dict[str, Any] = {
        "example_count": len(examples),
        "corpus_documents": len(corpus),
        "corpus_pages": {name: len(pages) for name, pages in corpus.items()},
    }

    # --- A. structure -------------------------------------------------------
    missing_fields = [
        (index, [field for field in REQUIRED if not example.get(field)])
        for index, example in enumerate(examples)
        if any(not example.get(field) for field in REQUIRED)
    ]
    questions = [example["question"] for example in examples]
    duplicates = [q for q, n in collections.Counter(questions).items() if n > 1]
    unknown_sources = [
        example["source_filename"]
        for example in examples
        if example["source_filename"] not in corpus
    ]
    report["structure"] = {
        "missing_fields": missing_fields,
        "duplicate_questions": duplicates,
        "unique_questions": len(set(questions)),
        "unknown_source_filenames": sorted(set(unknown_sources)),
        "categories": dict(collections.Counter(e["category"] for e in examples)),
        "question_length_chars": {
            "min": min(len(q) for q in questions),
            "median": sorted(len(q) for q in questions)[len(questions) // 2],
            "max": max(len(q) for q in questions),
        },
        "answer_length_chars": {
            "min": min(len(e["expected_answer"]) for e in examples),
            "median": sorted(len(e["expected_answer"]) for e in examples)[len(examples) // 2],
            "max": max(len(e["expected_answer"]) for e in examples),
        },
        "questions_naming_their_source_law": sum(
            1
            for e in examples
            if Path(e["source_filename"]).name.split("_")[-2][:4] in e["question"]
            or "办法" in e["question"]
            or "法》" in e["question"]
            or "通知" in e["question"]
        ),
    }

    # --- B. the evidence quote must exist, on the claimed page ---------------
    quote_missing: list[dict[str, Any]] = []
    page_mismatch: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    answers_with_unsupported_numbers: list[dict[str, Any]] = []
    for example in examples:
        source = example["source_filename"]
        quote = normalize(example["evidence_quote"])
        page_text = {page: normalize(text) for page, text in corpus.get(source, [])}
        containing = [page for page, text in page_text.items() if quote in text]
        if not containing:
            quote_missing.append({"question": example["question"], "source": source})
        elif example["page"] not in containing:
            page_mismatch.append(
                {
                    "question": example["question"],
                    "claimed_page": example["page"],
                    "quote_found_on": containing,
                }
            )
        # A quote found in several documents makes the document label ambiguous.
        elsewhere = [
            name
            for name, pages in corpus.items()
            if name != source and any(quote in normalize(text) for _page, text in pages)
        ]
        if elsewhere:
            ambiguous.append({"question": example["question"], "also_in": elsewhere})
        numbers = set(re.findall(r"\d+(?:\.\d+)?%?", example["expected_answer"]))
        unsupported = sorted(n for n in numbers if n not in normalize(example["evidence_quote"]))
        if unsupported:
            answers_with_unsupported_numbers.append(
                {"question": example["question"], "numbers_not_in_quote": unsupported}
            )
    report["labels"] = {
        "quote_not_found_in_source": quote_missing,
        "quote_on_a_different_page": page_mismatch,
        "quote_also_found_in_other_documents": ambiguous,
        "answers_with_numbers_absent_from_quote": answers_with_unsupported_numbers,
        "verified_examples": len(examples) - len(quote_missing) - len(page_mismatch),
    }

    # --- C. page range sanity + coverage ------------------------------------
    out_of_range = [
        {
            "question": e["question"],
            "claimed_page": e["page"],
            "pages_in_file": len(corpus[e["source_filename"]]),
        }
        for e in examples
        if e["source_filename"] in corpus and e["page"] > len(corpus[e["source_filename"]])
    ]
    per_source = collections.Counter(e["source_filename"] for e in examples)
    report["coverage"] = {
        "pages_out_of_range": out_of_range,
        "questions_per_document": dict(per_source),
        "documents_without_questions": sorted(set(corpus) - set(per_source)),
        "docx_pages_are_always_one": {
            name: sorted({e["page"] for e in examples if e["source_filename"] == name})
            for name in corpus
            if name.endswith(".docx")
        },
        "categories_per_document": {
            name: sorted({e["category"] for e in examples if e["source_filename"] == name})
            for name in sorted(per_source)
        },
    }

    # --- D. the bound copy (v2) must agree with the source of truth ---------
    v1_by_question = {e["question"]: e for e in examples}
    report["bound_copy"] = {
        "same_question_set": {e["question"] for e in bound} == set(v1_by_question),
        "page_disagreements": [
            {
                "question": e["question"],
                "v1": v1_by_question[e["question"]]["page"],
                "v2": e["expected_page"],
            }
            for e in bound
            if e["question"] in v1_by_question
            and e["expected_page"] != v1_by_question[e["question"]]["page"]
        ],
        "answer_disagreements": [
            e["question"]
            for e in bound
            if e["question"] in v1_by_question
            and e.get("expected_answer") != v1_by_question[e["question"]]["expected_answer"]
        ],
        "document_ids_known": sum(
            1
            for e in bound
            if e["expected_document_id"] in {d["document_id"] for d in manifest["documents"]}
        ),
        "document_ids_total": len(bound),
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    structure = report["structure"]
    print(f"examples: {report['example_count']}   corpus documents: {report['corpus_documents']}")
    print("\n[A] structure")
    for key in (
        "missing_fields",
        "duplicate_questions",
        "unknown_source_filenames",
        "categories",
    ):
        print(f"  {key}: {structure[key]}")
    print(f"  unique_questions: {structure['unique_questions']}")
    print(f"  question_length_chars: {structure['question_length_chars']}")
    print(f"  answer_length_chars: {structure['answer_length_chars']}")

    labels = report["labels"]
    print("\n[B] label integrity against the corpus")
    print(
        f"  verified (quote on the claimed page): {labels['verified_examples']}/{report['example_count']}"
    )
    for key in (
        "quote_not_found_in_source",
        "quote_on_a_different_page",
        "quote_also_found_in_other_documents",
        "answers_with_numbers_absent_from_quote",
    ):
        print(f"  {key}: {len(labels[key])}")
        for item in labels[key][:5]:
            print(f"      - {item}")

    coverage = report["coverage"]
    print("\n[C] coverage")
    print(f"  questions_per_document: {len(coverage['questions_per_document'])} documents")
    print(f"  pages_out_of_range: {coverage['pages_out_of_range']}")
    print(f"  documents_without_questions: {coverage['documents_without_questions']}")
    print(f"  docx pages: {coverage['docx_pages_are_always_one']}")

    bound = report["bound_copy"]
    print("\n[D] bound copy (golden_eval_v2_api.json)")
    print(f"  same_question_set: {bound['same_question_set']}")
    print(f"  page_disagreements: {bound['page_disagreements']}")
    print(f"  answer_disagreements: {bound['answer_disagreements']}")
    print(f"  document ids resolvable: {bound['document_ids_known']}/{bound['document_ids_total']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report = audit()
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
