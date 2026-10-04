"""Measure what the BM25 index saves on a corpus too large to re-tokenise per query.

Three timings over the same synthetic corpus and the same queries:

* ``legacy``   - the pre-index implementation, reproduced here: tokenise every document,
  count its terms, compute document frequency by scanning every document's token list for
  every query term, then score every document;
* ``no_index`` - today's ``retrieve`` without a prebuilt index. That is the one-shot scorer,
  which is what a caller pays when it will not repeat the query;
* ``indexed``  - today's ``retrieve`` with a ``BM25Index`` built once, which is what a cached
  ``LocalRetriever`` hands it.

The corpus is synthetic so the numbers are reproducible anywhere; a document is sized like
the real one (~640 characters per chunk on the 386-chunk policy corpus).

Usage:
    python -m scripts.benchmark_local_retrieval
    python -m scripts.benchmark_local_retrieval --documents 800 --queries 30 --repeats 5
"""

import argparse
import random
from collections import Counter
from math import log
from statistics import fmean
from time import perf_counter

from app.core import retrieval
from app.core.ingestion import Chunk
from app.core.retrieval import BM25Index, retrieve

# A plausible policy-document vocabulary: the point is that the tokens repeat across
# documents (IDF and length normalisation have to do real work), not the wording.
VOCABULARY = (
    "贷款", "期限", "施行", "办法", "农村", "集体", "经济组织", "承包", "经营权",
    "金融机构", "借款人", "归还", "本办法", "规定", "应当", "依法", "国务院",
    "监督管理", "财政部门", "补贴", "保险", "农业", "条例", "通知", "第三条",
    "申请", "条件", "材料", "审批", "公示",
)

CHARS_PER_DOCUMENT = 640


def build_corpus(document_count: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    documents = []
    for _ in range(document_count):
        text = ""
        while len(text) < CHARS_PER_DOCUMENT:
            text += rng.choice(VOCABULARY)
        documents.append(text)
    return documents


def build_queries(query_count: int, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    return [" ".join(rng.sample(VOCABULARY, 3)) for _ in range(query_count)]


def legacy_ranked(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[int, float]]:
    """The pre-index implementation, kept only so the benchmark can time it."""
    query_terms = set(retrieval._tokens(query))
    tokenized = [retrieval._tokens(document) for document in documents]
    average_length = sum(map(len, tokenized)) / len(tokenized) or 1.0
    document_frequency = {
        term: sum(term in terms for terms in tokenized) for term in query_terms
    }
    scored: list[tuple[int, float]] = []
    for index, terms in enumerate(tokenized):
        frequencies = Counter(terms)
        length_factor = 1 - b + b * len(terms) / average_length
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            idf = log(
                1
                + (len(documents) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            score += idf * (frequency * (k1 + 1) / (frequency + k1 * length_factor))
        if score > 0:
            scored.append((index, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def measure(call, repeats: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = perf_counter()
        call()
        samples.append((perf_counter() - started) * 1000)
    samples.sort()
    return {"p50": samples[len(samples) // 2], "mean": fmean(samples)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=400)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    documents = build_corpus(args.documents)
    queries = build_queries(args.queries)
    chunks = [
        Chunk(f"c{index}", "doc", 1, text, "latest") for index, text in enumerate(documents)
    ]
    characters = sum(len(document) for document in documents)
    print(
        f"corpus: {len(documents)} documents, {characters:,} characters "
        f"(~{characters // len(documents)} each), {len(queries)} queries x {args.repeats} repeats"
    )

    # Build the index once, the way a cached LocalRetriever does.
    started = perf_counter()
    index = BM25Index(documents)
    build_ms = (perf_counter() - started) * 1000

    def legacy() -> None:
        for query in queries:
            legacy_ranked(query, documents)

    def no_index() -> None:
        for query in queries:
            retrieve(query, chunks, 5, "sparse")

    def indexed() -> None:
        for query in queries:
            retrieve(query, chunks, 5, "sparse", index=index)

    legacy_stats = measure(legacy, args.repeats)
    no_index_stats = measure(no_index, args.repeats)
    indexed_stats = measure(indexed, args.repeats)

    per_query = len(queries)
    print(f"\none-off index build: {build_ms:.1f} ms")
    print(f"\n{'path':<12}{'total p50':>12}{'total mean':>13}{'per query p50':>15}")
    for name, stats in (
        ("legacy", legacy_stats),
        ("no_index", no_index_stats),
        ("indexed", indexed_stats),
    ):
        print(
            f"{name:<12}{stats['p50']:>10.1f} ms{stats['mean']:>11.1f} ms"
            f"{stats['p50'] / per_query:>13.2f} ms"
        )

    speedup = legacy_stats["p50"] / indexed_stats["p50"] if indexed_stats["p50"] else 0.0
    print(f"\nlegacy -> indexed per query: {speedup:.1f}x faster")
    print(
        "The ranking is identical (tests/test_bm25_index.py compares against the legacy "
        "implementation, and the CI quality gate compares the metrics)."
    )


if __name__ == "__main__":
    main()
