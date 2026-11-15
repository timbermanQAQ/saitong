"""抽取式答案的文本清洗：Markdown 表格与残缺加粗不能原样进答案。"""

from __future__ import annotations

from app.llm import ContextPiece, clean_markdown_noise, extractive_answer


def piece(text: str, *, heading: str = "提交材料", title: str = "赛题一") -> ContextPiece:
    return ContextPiece(doc_title=title, heading=heading, source_url="https://example.com/1",
                        text=text, track="seekdb", chunk_id="01_x#000")


TABLE = """| 材料 | 要求 |
| :--- | :--- |
| 部署脚本 | 可一键运行 |
| 说明文档 | **必须** |
|  |  |
"""


def test_table_rows_become_readable_and_separator_dropped() -> None:
    cleaned = clean_markdown_noise(TABLE)
    assert ":---" not in cleaned and "|" not in cleaned
    assert cleaned.splitlines() == ["材料：部署脚本；要求：可一键运行", "材料：说明文档；要求：**必须**"]


def test_unbalanced_bold_is_stripped_balanced_is_kept() -> None:
    assert clean_markdown_noise("1. **网站可访问。") == "1. 网站可访问。"
    assert clean_markdown_noise("要求 **可一键运行** 的脚本") == "要求 **可一键运行** 的脚本"


def test_prose_is_left_alone() -> None:
    text = "第一段。\n\n第二段里有 `|` 这个字符。"
    assert clean_markdown_noise(text) == text


def test_row_without_header_or_separator_is_just_joined() -> None:
    assert clean_markdown_noise("| 仓库地址 | 格式说明 |") == "仓库地址；格式说明"


def test_extractive_answer_hides_markdown_noise() -> None:
    """kb 出来的块以「【文档 › 章节】」开头，正文里就是语雀表格，答案不能带 | :--- |。"""
    crumb = ContextPiece(doc_title="赛题一", heading="提交材料", source_url="https://example.com/1",
                         text=f"【赛题一 › 提交材料】\n{TABLE}", track="seekdb", chunk_id="01_x#000")
    answer = extractive_answer("要交什么", [crumb, piece("提交截止 11 月 18 日。\n逾期不候。", heading="时间安排")])
    assert ":---" not in answer and "| 材料 |" not in answer
    assert "材料：部署脚本；要求：可一键运行" in answer
    assert "提交截止 11 月 18 日。" in answer
    assert answer.count("[1]") == 1 and "[2]" in answer
