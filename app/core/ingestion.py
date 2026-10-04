import datetime
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    page: int
    text: str
    version: str = "latest"


TEXT_ENCODINGS = ("utf-8-sig", "gb18030")

# Single source of truth for what the pipeline can read: the upload route, the
# corpus importer and the extraction dispatch all key off this.
SUPPORTED_SUFFIXES = (".pdf", ".docx", ".html", ".htm", ".xlsx", ".txt", ".md")
SUPPORTED_LABEL = "pdf, docx, html, xlsx, txt, md"

# Elements whose text is never content.
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "svg",
        "canvas",
        "form",
        "button",
        "select",
        "option",
        "textarea",
        "nav",
        "header",
        "footer",
        "aside",
    }
)

# Elements that end a line of text.
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "hr",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "tr",
        "td",
        "th",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "main",
        "address",
    }
)

VOID_TAGS = frozenset(
    {"br", "img", "meta", "link", "input", "hr", "source", "area", "base", "col", "embed", "wbr"}
)

# class/id hints that mark site chrome rather than content. A hint only counts when the
# same attribute carries no content-ish word: `site-header` is dropped, `article-header`
# (which usually holds the title) is kept.
NOISE_HINTS = (
    "nav",
    "menu",
    "header",
    "footer",
    "crumb",
    "breadcrumb",
    "sidebar",
    "side-bar",
    "share",
    "related",
    "recommend",
    "print",
    "banner",
    "copyright",
    "toolbar",
    "top-bar",
    "search",
    "advert",
)
CONTENT_HINTS = ("content", "article", "editor", "main", "text", "body", "detail", "trs", "zoom")
TOKEN = re.compile(r"[^a-z0-9]+")

# Structural hints are not enough: real pages leak chrome through generic divs.
# These are line-level residues observed on actual government pages (copyright,
# ICP filing number, session tokens, modal prompts, print/share widgets).
CHROME_WORDS = (
    "首页",
    "版权所有",
    "网站地图",
    "站点地图",
    "icp备",
    "icp证",
    "公安备案",
    "打印本页",
    "关闭窗口",
    "分享到",
    "无障碍",
    "是否继续",
    "即将离开",
    "主办单位",
    "承办单位",
    "技术支持",
    "网站声明",
    "联系我们",
    "返回顶部",
    "扫一扫",
    "关注我们",
    "手机版",
    "字体大小",
    "字号",
    "上一篇",
    "下一篇",
    "浏览次数",
    "访问量",
)
CHROME_PATTERNS = (
    re.compile(r"^[×✕✖xX]$"),
    re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE),
    re.compile(r"^[A-Za-z0-9+/=]{24,}$"),
)
# Only short lines are candidates: a provision that mentions a chrome word is a
# sentence, and sentences in these documents are long.
CHROME_MAX_LINE = 40


def _is_chrome_line(line: str) -> bool:
    if len(line) > CHROME_MAX_LINE:
        return False
    lowered = line.lower()
    if any(pattern.match(line) for pattern in CHROME_PATTERNS):
        return True
    return any(word in lowered for word in CHROME_WORDS)


def _is_boilerplate(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in SKIP_TAGS:
        return True
    for name, value in attrs:
        if name not in ("class", "id", "role", "aria-label") or not value:
            continue
        tokens = {token for token in TOKEN.split(value.lower()) if token}
        if tokens & set(CONTENT_HINTS):
            continue
        if any(hint in tokens or hint in value.lower() for hint in NOISE_HINTS):
            return True
    return False


class _HtmlTextExtractor(HTMLParser):
    """Turn a web page into text, leaving out code and site chrome.

    Government norms are frequently published only as HTML pages, and the page
    around the text (menus, breadcrumbs, share widgets, footers) is exactly the
    kind of boilerplate that produces confident wrong answers if it is indexed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def _line_break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipping:
            if tag not in VOID_TAGS:
                self._skipping += 1
            return
        if _is_boilerplate(tag, attrs):
            self._skipping = 1
            return
        if tag in BLOCK_TAGS:
            self._line_break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipping:
            return
        if _is_boilerplate(tag, attrs):
            return
        if tag in BLOCK_TAGS:
            self._line_break()

    def handle_endtag(self, tag: str) -> None:
        if self._skipping:
            self._skipping -= 1
            return
        if tag in BLOCK_TAGS:
            self._line_break()

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        self.parts.append(data)


def _extract_html(payload: bytes) -> str:
    """Extract the readable text of an HTML document.

    The declared charset is deliberately ignored: in practice these pages are
    UTF-8 or GB18030/GB2312, and the same encoding ladder used for plain text
    covers both without trusting a meta tag that is often wrong.
    """
    extractor = _HtmlTextExtractor()
    extractor.feed(_decode_text(payload))
    extractor.close()
    lines = (
        re.sub(r"[ \t\u3000\xa0]+", " ", line).strip()
        for line in "".join(extractor.parts).splitlines()
    )
    return "\n".join(line for line in lines if line and not _is_chrome_line(line))


def _cell_text(cell) -> str:
    """Serialise a cell the way the document displays it.

    A cell holding 0.4 formatted as a percentage reads "40%" on screen and in
    every question asked about it, so the number format is part of the content.
    """
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.isoformat()[:10]
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float):
        if "%" in cell.number_format:
            return f"{value * 100:g}%"
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _row_pairs(header: list[str], row) -> list[str]:
    pairs: list[str] = []
    for index, cell in enumerate(row):
        text = _cell_text(cell)
        if not text:
            continue
        name = header[index] if index < len(header) and header[index] else f"第{index + 1}列"
        pairs.append(f"{name}: {text}")
    return pairs


def _non_empty(row) -> list[str]:
    return [text for cell in row if (text := _cell_text(cell))]


def _extract_xlsx(payload: bytes) -> list[tuple[int, str]]:
    """One page per worksheet, and every row repeats its column names.

    A sheet flattened into a single string loses which column a number belongs to
    the moment the text is cut into chunks: the header row ends up hundreds of
    characters away from the value it describes. Serialising each row as
    self-describing "列名: 值" pairs means a chunk only has to contain the row,
    not the whole sheet.

    Real sheets start with title rows, not headers - the sample that drove this
    had "附件1", then "102个县（市）名单", then a blank row, and only then the
    header - so the header is the first row with at least two filled cells and
    everything above it is kept as content.

    Limitations worth knowing: a merged or two-row header leaves later names as
    第N列 and repeated names stay ambiguous (the sample header repeats 序号/省辖市/
    市县区 across two column blocks); formulas are read from their cached values;
    the workbook is loaded in full.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(payload), data_only=True)
    pages: list[tuple[int, str]] = []
    for number, sheet in enumerate(workbook.worksheets, start=1):
        rows = [list(row) for row in sheet.iter_rows()]
        header_index = next(
            (index for index, row in enumerate(rows) if len(_non_empty(row)) >= 2),
            None,
        )
        lines: list[str] = []
        if header_index is None:
            lines = [" | ".join(texts) for row in rows if (texts := _non_empty(row))]
        else:
            lines = [
                " | ".join(texts) for row in rows[:header_index] if (texts := _non_empty(row))
            ]
            header = [_cell_text(cell) for cell in rows[header_index]]
            for position, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
                pairs = _row_pairs(header, row)
                if pairs:
                    lines.append(f"{sheet.title} 第{position}行 " + " | ".join(pairs))
            if not lines:
                # A header with no data rows is still content, and emitting
                # nothing would fail the document at the empty-extraction guard.
                lines = [f"{sheet.title} 表头 " + " | ".join(name for name in header if name)]
        pages.append((number, "\n".join(lines)))
    return pages


def _decode_text(payload: bytes) -> str:
    """Decode a plain-text upload the way Chinese corpora actually arrive.

    UTF-8 first (BOM tolerated and stripped), then GB18030 - a superset of GBK,
    which is what most legacy government text files use. Decoding those as UTF-8
    with errors="replace" silently turns the whole document into U+FFFD and the
    ingestion still reports success. Anything that is neither encoding still
    decodes, with replacement characters, instead of failing the upload.
    """
    for encoding in TEXT_ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def extract_text(filename: str, payload: bytes) -> list[tuple[int, str]]:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        import pymupdf

        pdf = pymupdf.open(stream=payload, filetype="pdf")
        # Indexed access rather than iterating the document: pymupdf's Document iterates
        # through the legacy __getitem__ protocol, which its stubs do not declare.
        return [(index + 1, pdf[index].get_text()) for index in range(pdf.page_count)]
    if lower.endswith(".docx"):
        from docx import Document

        # A separate name on purpose: reusing `document` here made one variable hold both
        # a pymupdf and a python-docx document, and the second branch then "inherited" the
        # first one's type.
        docx_document = Document(BytesIO(payload))
        return [(1, "\n".join(paragraph.text for paragraph in docx_document.paragraphs))]
    if lower.endswith((".html", ".htm")):
        return [(1, _extract_html(payload))]
    if lower.endswith(".xlsx"):
        return _extract_xlsx(payload)
    if lower.endswith((".txt", ".md")):
        return [(1, _decode_text(payload))]
    raise ValueError(f"supported file types: {SUPPORTED_LABEL}")


def chunk_pages(document_id: str, pages: list[tuple[int, str]], size: int = 800, overlap: int = 120) -> list[Chunk]:
    if size <= overlap:
        raise ValueError("chunk size must be greater than overlap")
    chunks: list[Chunk] = []
    for page, text in pages:
        clean = " ".join(text.split())
        start = 0
        while start < len(clean):
            end = min(start + size, len(clean))
            chunks.append(Chunk(f"{document_id}:{len(chunks)}", document_id, page, clean[start:end]))
            if end == len(clean):
                break
            start = end - overlap
    return chunks
