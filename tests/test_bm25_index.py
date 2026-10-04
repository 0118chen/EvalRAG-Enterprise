"""The inverted index must rank exactly like the direct implementation it replaced.

`bm25_scores` re-tokenised every document on every query and computed a `Counter` per
document, for term statistics that cannot change while the corpus does not. The index
moves that to construction time and answers a query from the postings of its terms only.
Both properties are pinned here: identical output, and a per-query cost that does not grow
with the corpus.
"""

import asyncio
import math
from collections import Counter

import pytest

from app.core import retrieval
from app.core.backends import LocalRetriever
from app.core.ingestion import Chunk
from app.core.retrieval import (
    BM25Index,
    bm25_score,
    bm25_scores,
    query_terms,
    retrieve,
)

CORPUS = [
    "第七十四条 本办法自2021年6月1日起施行。",
    "贷款人是指在中华人民共和国境内依法设立的银行业金融机构。",
    "农户贷款是指银行业金融机构向农户发放的贷款，用于生产经营。",
    "本办法所称贷款期限，是指借款人取得贷款到归还贷款的时间。",
    "农村集体经济组织成员依法享有土地承包经营权。",
    "The loan policy effective date is January first and the term is thirty years.",
]

QUERIES = [
    "贷款期限",
    "贷款人 定义",
    "effective date",
    "土地承包经营权",
    "本办法从哪一天开始施行",
    "完全无关的查询词",
]


def _reference_ranking(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[int, float]]:
    """The pre-index implementation, written out independently of the index internals.

    Deliberately repeats the formula instead of calling into `BM25Index`: a cross-check
    that shares the implementation checks nothing.
    """
    terms = query_terms(query)
    tokenized = [retrieval._tokens(document) for document in documents]
    average = sum(map(len, tokenized)) / len(tokenized) or 1.0
    document_frequency = {
        term: sum(term in tokens for tokens in tokenized) for term in terms
    }
    scored: list[tuple[int, float]] = []
    for index, tokens in enumerate(tokenized):
        frequencies = Counter(tokens)
        length_factor = 1 - b + b * len(tokens) / average
        score = 0.0
        for term in terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            idf = math.log(
                1
                + (len(documents) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            score += idf * (frequency * (k1 + 1) / (frequency + k1 * length_factor))
        if score > 0:
            scored.append((index, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


@pytest.mark.parametrize("query", QUERIES)
def test_the_index_reproduces_the_direct_implementation(query: str) -> None:
    index = BM25Index(CORPUS)

    # Exact float equality, not approx: both accumulate the per-term contributions in
    # query-term order, so a difference means the index changed the arithmetic.
    assert index.ranked(query) == _reference_ranking(query, CORPUS)
    assert index.scores(query) == bm25_scores(query, [*CORPUS])


def test_document_frequency_and_idf_follow_the_formula() -> None:
    index = BM25Index(["a b c", "b c d", "c d e"])

    assert index.document_frequency("c") == 3
    assert index.document_frequency("a") == 1
    assert index.document_frequency("absent") == 0
    assert index.idf("absent") == pytest.approx(math.log(1 + (3 - 0 + 0.5) / (0 + 0.5)))
    assert index.idf("c") == pytest.approx(math.log(1 + (3 - 3 + 0.5) / (3 + 0.5)))


def test_documents_without_a_query_term_score_zero_and_are_not_ranked() -> None:
    index = BM25Index(["alpha beta", "gamma delta"])

    scores = index.scores("alpha")

    assert scores[0] > 0
    assert scores[1] == 0.0
    assert [position for position, _ in index.ranked("alpha")] == [0]
    assert index.ranked("nothing-matches") == []


def test_equal_scores_keep_corpus_order() -> None:
    # Every document holds the term once and has the same length, so the scores tie and
    # the original corpus order has to survive - the fusion downstream reads it as rank.
    index = BM25Index(["c", "c", "c"])

    assert [position for position, _ in index.ranked("c")] == [0, 1, 2]


def test_an_empty_corpus_is_not_a_division_by_zero() -> None:
    index = BM25Index([])

    assert len(index) == 0
    assert index.scores("anything") == []
    assert index.ranked("anything") == []


def test_query_terms_are_deduplicated_in_first_appearance_order() -> None:
    # A set would put these in hash order, which depends on PYTHONHASHSEED; the float sum
    # then differs in its last bits between runs and can flip a tie.
    assert query_terms("beta alpha beta") == ["beta", "alpha"]


def test_retrieve_accepts_a_prebuilt_index_and_returns_the_same_ranking() -> None:
    chunks = [
        Chunk(f"c{index}", "doc", 1, text, "latest") for index, text in enumerate(CORPUS)
    ]
    index = BM25Index([chunk.text for chunk in chunks])

    with_index = retrieve("贷款期限", chunks, 3, "sparse", index=index)
    without_index = retrieve("贷款期限", chunks, 3, "sparse")

    assert with_index == without_index
    assert with_index


def test_a_query_does_not_retokenise_the_corpus(monkeypatch) -> None:
    """The whole point of the index, asserted without a stopwatch.

    A timing assertion would be flaky; counting tokeniser calls is deterministic and says
    the same thing: the corpus is tokenised once at construction, and each query costs one
    query's worth of tokenising regardless of how large the corpus is.
    """
    corpus_size = 50
    chunks = [
        Chunk(f"c{index}", "doc", 1, f"第{index}条 贷款 期限 施行 办法 归还", "latest")
        for index in range(corpus_size)
    ]
    calls: list[str] = []
    original = retrieval._tokens

    def counting_tokens(text: str) -> list[str]:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(retrieval, "_tokens", counting_tokens)
    retriever = LocalRetriever(chunks, "sparse")
    after_construction = len(calls)

    assert after_construction == corpus_size  # one pass over the corpus, once

    asyncio.run(retriever.search("贷款期限", 5))
    assert len(calls) - after_construction == 1  # only the query

    asyncio.run(retriever.search("办法施行", 5))
    assert len(calls) - after_construction == 2  # and it stays one per query


def test_a_hybrid_retriever_shares_one_index_across_queries(monkeypatch) -> None:
    chunks = [
        Chunk(f"c{index}", "doc", 1, f"第{index}条 贷款 期限 施行", "latest")
        for index in range(20)
    ]
    calls: list[str] = []
    original = retrieval._tokens

    def counting_tokens(text: str) -> list[str]:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(retrieval, "_tokens", counting_tokens)
    retriever = LocalRetriever(chunks, "hybrid")
    after_construction = len(calls)

    asyncio.run(retriever.search("贷款期限", 5))

    # The query itself plus the corpus is what the embedding cache does not cover, so a
    # hybrid query may add the query vectors; what it must not add is corpus tokenising.
    assert len(calls) - after_construction == 1
    assert after_construction == 20


def test_bm25_score_single_document_still_works() -> None:
    assert bm25_score("loan", "loan policy") > 0
    assert bm25_score("absent", "loan policy") == 0.0
