"""Deterministic citation checks that run before an answer is returned.

``build_context`` labels every evidence block ``[<document_id> p.<page>]`` and the
prompt tells the model to answer from those blocks only. A model that invents a
document id, or a page that was never retrieved, is the failure this module exists
to catch: an unverifiable citation is what makes a RAG answer unusable for
compliance work, and it is invisible in a text-only review.

Parsing is deliberately conservative, because a false positive here refuses a
correct answer:

* only bracket spans that *look* like one of our citations count as an attempt -
  the document part must be a document we actually retrieved, or the span must
  carry an explicit page marker (``p.4`` / ``page 4`` / ``第4页``). Ordinary
  bracketed prose, footnote markers such as ``[1]`` and asides such as ``[注]``
  are therefore ignored rather than reported as bad citations;
* an attempt whose document was not retrieved is **unknown**;
* an attempt whose document was retrieved but whose page was not is
  **ungrounded**.

``CitationReport.ok`` is false when either list is non-empty, and
``answer_with_evidence`` refuses to return such an answer. A missing citation is
a different matter: local and mock providers legitimately emit none, so requiring
one is opt-in (``Settings.citation_required``).
"""

import re
from dataclasses import dataclass

from app.core.ingestion import Chunk

# `[doc-1 p.2]` is what build_context produces; the page marker is optional so a
# dense document id without a page still needs to be checked.
_BRACKET = re.compile(r"\[([^\[\]]{1,200})\]")
_PAGE = re.compile(r"(?:pages?|p)\s*\.?\s*(\d+)|第\s*(\d+)\s*页", re.IGNORECASE)
# `[doc-1:4]`, which models produce often enough to be worth reading.
_PAGE_SUFFIX = re.compile(r"[:：]\s*(\d+)\s*$")
_TRAILING_SEPARATORS = " \t,;，；|·-–—:："


@dataclass(frozen=True)
class Citation:
    """One bracket span that was read as a citation."""

    raw: str
    document_id: str
    page: int | None


@dataclass(frozen=True)
class CitationReport:
    """Outcome of checking one answer against the evidence it was given."""

    answer_is_empty: bool = True
    evidence_count: int = 0
    citations: tuple[Citation, ...] = ()
    unknown: tuple[Citation, ...] = ()
    ungrounded: tuple[Citation, ...] = ()

    @property
    def cited(self) -> bool:
        """Whether the answer cites at least one retrieved span."""
        return bool(self.citations)

    @property
    def ok(self) -> bool:
        """An answer is usable when it is non-empty, was given evidence, and
        every citation it makes resolves to a retrieved (document, page)."""
        return (
            not self.answer_is_empty
            and self.evidence_count > 0
            and not self.unknown
            and not self.ungrounded
        )

    def __bool__(self) -> bool:  # keeps `if validate_citations(...)` readable
        return self.ok

    def summary(self) -> str:
        return (
            f"citations={len(self.citations)} unknown={len(self.unknown)} "
            f"ungrounded={len(self.ungrounded)}"
        )


def _split(content: str) -> tuple[str, int | None]:
    """Split a bracket span into its document part and its page, if any."""
    page: int | None = None
    match = _PAGE.search(content)
    if match:
        page = int(match.group(1) or match.group(2))
        content = content[: match.start()] + content[match.end() :]
    return content.strip(_TRAILING_SEPARATORS), page


def _read_as_citation(raw: str, known_pages: dict[str, set[int]]) -> Citation | None:
    """Read one bracket span as a citation, or return None if it is just prose.

    Nothing counts as a citation attempt unless it either names a retrieved document
    or carries an explicit page marker. That asymmetry is the point: ``[1]``, ``[注]``
    and ``[附件二]`` are ordinary prose and must not refuse a correct answer, while
    ``[doc-z p.1]`` and ``[doc-a p.7]`` name something we did not retrieve and must.
    """
    document_id, page = _split(raw)
    if document_id not in known_pages and page is None:
        # `[doc-a:4]`: the page rides on the document part. Only accept the split when
        # the left-hand side is a document we retrieved, so `[注:3]` stays prose.
        suffix = _PAGE_SUFFIX.search(document_id)
        if suffix is not None:
            head = document_id[: suffix.start()].strip(_TRAILING_SEPARATORS)
            if head in known_pages:
                document_id, page = head, int(suffix.group(1))
    if document_id not in known_pages and page is None:
        return None
    return Citation(raw=raw, document_id=document_id, page=page)


def citation_report(answer: str, evidence: list[Chunk]) -> CitationReport:
    """Check every citation-looking span in ``answer`` against ``evidence``."""
    known_pages: dict[str, set[int]] = {}
    for chunk in evidence:
        known_pages.setdefault(chunk.document_id, set()).add(chunk.page)

    citations: list[Citation] = []
    unknown: list[Citation] = []
    ungrounded: list[Citation] = []
    for raw in _BRACKET.findall(answer):
        citation = _read_as_citation(raw, known_pages)
        if citation is None:
            continue
        citations.append(citation)
        if citation.document_id not in known_pages:
            unknown.append(citation)
        elif citation.page is not None and citation.page not in known_pages[citation.document_id]:
            ungrounded.append(citation)

    return CitationReport(
        answer_is_empty=not answer.strip(),
        evidence_count=len(evidence),
        citations=tuple(citations),
        unknown=tuple(unknown),
        ungrounded=tuple(ungrounded),
    )


def validate_citations(answer: str, chunks: list[Chunk]) -> CitationReport:
    """Backwards-compatible entry point; ``CitationReport`` is truthy when usable."""
    return citation_report(answer, chunks)
