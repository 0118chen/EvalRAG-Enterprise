from app.core.ingestion import chunk_pages, extract_text


def test_text_ingestion_and_overlap() -> None:
    pages = extract_text("policy.txt", "生效日期为2026年1月1日。".encode())
    chunks = chunk_pages("doc-1", pages, size=10, overlap=2)
    assert chunks and chunks[0].page == 1


def test_gb18030_text_file_is_decoded_instead_of_mangled() -> None:
    """Chinese government text files are routinely GBK/GB18030, not UTF-8.

    Decoding them as UTF-8 with errors="replace" silently turns every character
    into U+FFFD: the document then indexes as noise while reporting success.
    """
    original = "农户贷款管理办法：贷款人应当审查借款人的还款能力。"
    pages = extract_text("notice.txt", original.encode("gb18030"))

    assert pages[0][1] == original
    assert "\ufffd" not in pages[0][1]


def test_utf8_text_still_decodes_as_utf8() -> None:
    original = "农村集体经济组织法 自2025年5月1日起施行"
    assert extract_text("law.md", original.encode("utf-8"))[0][1] == original


def test_utf8_bom_is_not_kept_in_the_text() -> None:
    original = "小额贷款公司监督管理暂行办法"
    pages = extract_text("law.txt", original.encode("utf-8-sig"))

    assert pages[0][1] == original


def test_undecodable_bytes_do_not_raise() -> None:
    pages = extract_text("legacy.txt", b"\x81\x81\xff")

    assert isinstance(pages[0][1], str)

