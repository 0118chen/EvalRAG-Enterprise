"""生成器过滤/闸门的回归测试。

这些行为都是先被真实缺陷逼出来的，不是预防性设计：

- `selected` 最初用子串匹配，`--documents 15` 会把 `10_..._2015.docx` 也选进来；
- `asks_for_boilerplate` / `shared_with_another_document` 来自 v3 第一轮生成的坏题
  （问施行日期、引文在别的文档里逐字相同），它们让去泄漏题"看得见答案却指不唯一"。
"""

from pathlib import Path

from scripts.generate_golden_set_v2 import (
    asks_for_boilerplate,
    mentions_a_title,
    selected,
    shared_with_another_document,
)

LAW = Path("law")


def test_selected_matches_numeric_prefix_exactly() -> None:
    name = LAW / "14_行政法规_国务院_融资担保公司监督管理条例_2017.pdf"
    assert selected(name, ["14"])
    assert not selected(name, ["15"])


def test_selected_does_not_match_a_year_inside_another_filename() -> None:
    """`15` 曾把 `10_基础法律_全国人大常委会_中华人民共和国商业银行法_2015.docx` 选中。"""
    other = LAW / "10_基础法律_全国人大常委会_中华人民共和国商业银行法_2015.docx"
    assert not selected(other, ["15"])
    assert not selected(other, ["18"])
    # 年份本身仍可作为完整名字段被选中，是刻意的
    assert selected(other, ["2015"])


def test_selected_matches_a_whole_name_segment() -> None:
    name = LAW / "14_行政法规_国务院_融资担保公司监督管理条例_2017.pdf"
    assert selected(name, ["融资担保公司监督管理条例"])
    # 片段不算命中：否则任何一个字都可能捞出一堆文件
    assert not selected(name, ["融资担保公司"])


def test_selected_ignores_documents_outside_the_corpus() -> None:
    assert not selected(LAW / "18_测算表_河南省财政厅_农业保险保费补贴资金测算表_2022.xlsx", ["14", "15"])


def test_asks_for_boilerplate_rejects_the_effective_date_question() -> None:
    assert asks_for_boilerplate("这份规定从哪一天开始正式生效？")
    assert asks_for_boilerplate("该办法什么时候开始施行？")
    assert not asks_for_boilerplate("一家做担保的公司，能不能给它的控股股东做担保？")


def test_shared_with_another_document_catches_a_quote_used_twice() -> None:
    corpus_texts = {
        "a.pdf": "监管部门进行现场检查，检查人员不得少于2人，并出示证件。",
        "b.html": "地方金融监督管理部门可以约谈其董事、监事、高级管理人员。",
    }
    assert shared_with_another_document("检查人员不得少于2人", "b.html", corpus_texts)
    assert not shared_with_another_document("可以约谈其董事、监事", "b.html", corpus_texts)
    # 只看别的文档：引文出现在自己文档里不算共享
    assert not shared_with_another_document("检查人员不得少于2人", "a.pdf", corpus_texts)


def test_mentions_a_title_still_catches_leaks() -> None:
    titles = ["中华人民共和国商业银行法", "商业银行法"]
    assert mentions_a_title("《中华人民共和国商业银行法》怎么规定的？", titles)
    assert mentions_a_title("商业银行法里怎么规定的？", titles)
    assert mentions_a_title("商业银行法（修正草案）说了什么？", ["商业银行法（修正草案）"])
