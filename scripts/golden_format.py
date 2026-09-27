"""Read a golden set in either its flat (v1) or extended (v2) shape.

v1 stores one label per example (`source_filename`, `page`, `evidence_quote`). v2 adds
multi-hop examples (`expected_evidence`: one entry per hop), equivalent alternatives
(`evidence_mode: "any"`) and unanswerable examples (`should_refuse`). Both the experiment
harness, the audit and the tools around them need the same normalised view, so the shape
knowledge lives here instead of being re-derived per script.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PAGE_KEYS = ("page", "expected_page")
QUOTE_KEYS = ("evidence_quote", "quote")


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class GoldenHop:
    """One labelled passage: which file, which page, which sentence."""

    source_filename: str
    page: int | None
    quote: str


@dataclass(frozen=True)
class GoldenExample:
    question: str
    category: str
    hops: tuple[GoldenHop, ...]
    expected_answer: str | None = None
    mode: str = "all"
    should_refuse: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def source_filenames(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(hop.source_filename for hop in self.hops))

    @property
    def quotes(self) -> tuple[str, ...]:
        return tuple(hop.quote for hop in self.hops)


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _hop(payload: dict[str, Any]) -> GoldenHop | None:
    filename = payload.get("source_filename")
    quote = _first(payload, QUOTE_KEYS)
    page = _first(payload, PAGE_KEYS)
    if not filename or not quote:
        return None
    return GoldenHop(str(filename), int(page) if page is not None else None, str(quote))


def parse_example(payload: dict[str, Any]) -> GoldenExample:
    """Normalise one example from either shape into hops + mode + refusal flag."""
    question = str(payload.get("question", "")).strip()
    category = str(payload.get("category") or "general").strip()
    mode = str(payload.get("evidence_mode") or "all").strip()
    should_refuse = bool(payload.get("should_refuse", False))
    spans = payload.get("expected_evidence") or []
    hops = tuple(hop for hop in (_hop(span) for span in spans) if hop)
    if not hops and not should_refuse:
        single = _hop(payload)
        hops = (single,) if single else ()
    return GoldenExample(
        question=question,
        category=category,
        hops=hops,
        expected_answer=payload.get("expected_answer"),
        mode=mode if mode in {"all", "any"} else "all",
        should_refuse=should_refuse,
        raw=payload,
    )


def load_golden(path: Path) -> list[GoldenExample]:
    """Read a golden set file: a bare list (v1) or an object with an ``examples`` list.

    A JSON object without that list is rejected rather than read as empty: pointing the
    loader at a manifest or a results file must fail loudly, not silently score nothing.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        entries: list[Any] = payload
    elif isinstance(payload, dict) and isinstance(payload.get("examples"), list):
        entries = payload["examples"]
    else:
        raise TypeError(f"unsupported golden set shape in {path}")
    return [parse_example(entry) for entry in entries]


def resolve_document_ids(
    examples: list[GoldenExample],
    by_filename: dict[str, str],
) -> None:
    """Fail loudly on a hop that names a file the corpus does not contain."""
    missing = sorted(
        {
            hop.source_filename
            for example in examples
            for hop in example.hops
            if hop.source_filename not in by_filename
        }
    )
    if missing:
        raise SystemExit(f"golden set references files that are not in the corpus: {missing}")
