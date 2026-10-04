"""Citation checking: an answer may only cite evidence that was actually retrieved.

The prompt asks the model to answer from the ``[<document_id> p.<page>]`` blocks that
``build_context`` produces. A model that invents a document, or a page that was never
retrieved, produces an answer that reads as authoritative and is not - which is exactly
the failure a compliance RAG system has to refuse.
"""

from app.core.citations import citation_report, validate_citations
from app.core.ingestion import Chunk

EVIDENCE = [
    Chunk("c1", "doc-a", 1, "第七十四条 本办法自2021年6月1日起施行。"),
    Chunk("c2", "doc-a", 4, "第四条 申请材料应当包括下列内容。"),
    Chunk("c3", "doc-b", 2, "第二条 本办法适用于农村集体经济组织。"),
]


def test_an_answer_without_evidence_is_never_usable() -> None:
    assert citation_report("answer", []).ok is False
    assert not validate_citations("answer", [])


def test_an_empty_answer_is_never_usable() -> None:
    assert citation_report("   \n", EVIDENCE).ok is False


def test_a_grounded_citation_passes() -> None:
    report = citation_report("依据 [doc-a p.1] 的规定，自2021年6月1日起施行。", EVIDENCE)

    assert report.ok is True
    assert report.cited is True
    assert [(c.document_id, c.page) for c in report.citations] == [("doc-a", 1)]
    assert report.unknown == () and report.ungrounded == ()


def test_a_document_we_never_retrieved_is_rejected() -> None:
    report = citation_report("依据 [doc-z p.1] 的规定。", EVIDENCE)

    assert report.ok is False
    assert [c.document_id for c in report.unknown] == ["doc-z"]


def test_a_page_we_never_retrieved_is_rejected() -> None:
    # The document is real, the page is not: still a fabricated citation.
    report = citation_report("依据 [doc-a p.7] 的规定。", EVIDENCE)

    assert report.ok is False
    assert report.unknown == ()
    assert [(c.document_id, c.page) for c in report.ungrounded] == [("doc-a", 7)]


def test_a_document_citation_without_a_page_is_accepted_when_the_document_matched() -> None:
    report = citation_report("见 [doc-b]。", EVIDENCE)

    assert report.ok is True
    assert report.citations[0].page is None


def test_the_page_marker_formats_models_actually_emit_are_read() -> None:
    for text in (
        "[doc-a p.4]",
        "[doc-a p4]",
        "[doc-a page 4]",
        "[doc-a 第4页]",
        "[doc-a, p.4]",
        "[doc-a:4]",
    ):
        report = citation_report(f"见 {text} 的规定。", EVIDENCE)
        assert report.ok is True, text
        assert [(c.document_id, c.page) for c in report.citations] == [("doc-a", 4)], text


def test_ordinary_brackets_are_not_mistaken_for_citations() -> None:
    # Footnotes, asides and appendix references must not refuse a correct answer.
    report = citation_report("第一，[注]见备注[1]；另见[附件二]；[注:3]为补充说明。", EVIDENCE)

    assert report.citations == ()
    assert report.ok is True


def test_a_missing_citation_is_not_an_error_on_its_own() -> None:
    # Mock and some local providers emit no citation at all; refusing every such answer
    # is a separate policy (Settings.citation_required), not an integrity failure.
    report = citation_report("已检索到 2 条相关证据。", EVIDENCE)

    assert report.cited is False
    assert report.ok is True


def test_one_bad_citation_refuses_the_whole_answer() -> None:
    report = citation_report("见 [doc-a p.1]，另据 [doc-q p.9]。", EVIDENCE)

    assert report.ok is False
    assert len(report.citations) == 2
    assert [c.document_id for c in report.unknown] == ["doc-q"]
    assert report.summary() == "citations=2 unknown=1 ungrounded=0"
