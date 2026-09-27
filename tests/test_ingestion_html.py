"""HTML ingestion: government norms are often only published as web pages."""

from app.core.ingestion import chunk_pages, extract_text

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <title>北京市地方金融监督管理条例</title>
  <style>body { margin: 0; font-family: serif; }</style>
  <script>var _hmt = []; _hmt.push(['_trackPageview']);</script>
</head>
<body>
  <div class="nav-menu"><a href="/">首页</a><a href="/law">政策法规</a></div>
  <div class="article">
    <h1>北京市地方金融监督管理条例</h1>
    <p>第一条 为了规范地方金融组织行为，防范金融风险，制定本条例。</p>
    <p>第二条 本条例所称地方金融组织，包括小额贷款公司、融资担保公司。</p>
    <p>第三条&nbsp;地方金融组织应当依法开展业务，不得吸收公众存款。</p>
  </div>
  <div class="related-links"><p>相关链接：北京市人民代表大会常务委员会</p></div>
  <div class="footer"><p>版权所有 北京市地方金融监督管理局</p></div>
</body>
</html>
"""


def _text(payload: str) -> str:
    pages = extract_text("law.html", payload.encode("utf-8"))
    assert len(pages) == 1
    assert pages[0][0] == 1
    return pages[0][1]


def test_body_text_is_extracted() -> None:
    text = _text(PAGE)

    assert "第一条 为了规范地方金融组织行为，防范金融风险，制定本条例。" in text
    assert "第二条 本条例所称地方金融组织，包括小额贷款公司、融资担保公司。" in text


def test_markup_and_code_are_not_indexed() -> None:
    text = _text(PAGE)

    assert "<p>" not in text and "&nbsp;" not in text
    assert "font-family" not in text
    assert "_trackPageview" not in text


def test_navigation_and_footer_boilerplate_are_dropped() -> None:
    """Boilerplate is not noise the retriever should ever match on."""
    text = _text(PAGE)

    assert "首页" not in text
    assert "政策法规" not in text
    assert "版权所有" not in text
    assert "相关链接" not in text


def test_html_entities_are_decoded() -> None:
    text = _text(PAGE)

    assert "第三条 地方金融组织应当依法开展业务，不得吸收公众存款。" in text
    assert "\xa0" not in text


def test_paragraphs_stay_on_separate_lines() -> None:
    """chunk_pages collapses whitespace, so blocks must not run together here."""
    text = _text(PAGE)

    lines = [line for line in text.splitlines() if line.strip()]
    assert any(line.startswith("第一条") for line in lines)
    assert any(line.startswith("第二条") for line in lines)


def test_extracted_page_still_chunks_into_retrievable_text() -> None:
    pages = extract_text("law.html", PAGE.encode("utf-8"))
    chunks = chunk_pages("doc-html", pages, size=60, overlap=10)

    assert chunks
    assert all(chunk.page == 1 for chunk in chunks)
    assert any("小额贷款公司" in chunk.text for chunk in chunks)


def test_utf8_html_is_decoded_without_replacement_characters() -> None:
    text = _text(PAGE)

    assert "\ufffd" not in text


REAL_LEFTOVERS = """<html><body>
<div class="content">
<p>第一条 为了规范地方金融组织行为，制定本条例。</p>
</div>
<div><p>首页</p></div>
<div><p>版权所有 北京市地方金融监督管理局</p></div>
<div><p>京ICP备05060933号</p></div>
<div><p>×</p></div>
<div><p>f0917f846075c7a6414f7aeffc54fa3b</p></div>
<div><p>打印本页 关闭窗口</p></div>
<div><p>您访问的链接即将离开“首都之窗”门户网站 是否继续？</p></div>
</body></html>"""


def test_chrome_lines_that_real_pages_leak_are_removed() -> None:
    """The structural heuristic misses chrome; these are the residues observed
    on three real government pages (ICP number, copyright, session token, modal)."""
    text = _text(REAL_LEFTOVERS)

    assert "第一条 为了规范地方金融组织行为，制定本条例。" in text
    for residue in (
        "首页",
        "版权所有",
        "京ICP",
        "×",
        "f0917f846075c7a6414f7aeffc54fa3b",
        "打印本页",
        "是否继续",
    ):
        assert residue not in text


def test_a_legal_sentence_mentioning_a_chrome_word_survives() -> None:
    """The filter only drops short lines: a provision that happens to contain
    "主办单位" or "联系我们" is content, not chrome."""
    provision = (
        "第二十条 主办单位应当于每年三月三十一日前向监管部门报送上一年度工作报告，"
        "并对报告内容的真实性、准确性和完整性负责，不得有虚假记载。"
    )
    text = _text(f"<html><body><p>{provision}</p></body></html>")

    assert provision in text
