"""Chunk presets, token counting, clamping and line ranges (design §6)."""

from __future__ import annotations

import pytest

from src.infra.knowledge_base.chunking import (
    CHUNK_PRESETS,
    DEFAULT_CHUNK_PRESET,
    TextChunk,
    chunk_markdown,
    count_tokens,
    normalize_preset,
)


def numbered_lines(count: int) -> str:
    return "\n".join(f"line {i} alpha beta gamma delta" for i in range(count))


def lines_in(markdown: str, chunk: TextChunk) -> list[str]:
    assert chunk.start_line is not None and chunk.end_line is not None
    return markdown.split("\n")[chunk.start_line - 1 : chunk.end_line]


def test_count_tokens_counts_words_numbers_and_cjk_chars() -> None:
    assert count_tokens("") == 0
    assert count_tokens("   \n") == 0
    assert count_tokens("hello world") == 2
    assert count_tokens("你好 world 123") == 4
    assert count_tokens("snake_case_name") == 1
    assert count_tokens("，。！") == 1  # non-blank text counts at least one token


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "general"),
        ("", "general"),
        ("unknown", "general"),
        ("naive", "general"),
        ("QA", "qa"),
        (" book ", "book"),
        ("laws", "laws"),
        ("semantic", "general"),
    ],
)
def test_normalize_preset(value: str | None, expected: str) -> None:
    assert normalize_preset(value) == expected
    assert DEFAULT_CHUNK_PRESET in CHUNK_PRESETS


def test_empty_markdown_has_no_chunks() -> None:
    for preset in CHUNK_PRESETS:
        assert chunk_markdown("", filename="a.md", preset=preset) == []
        assert chunk_markdown("  \n\n ", filename="a.md", preset=preset) == []


def test_general_chunks_are_contiguous_with_exact_line_ranges() -> None:
    markdown = numbered_lines(60)  # 6 tokens per line
    chunks = chunk_markdown(markdown, filename="a.md", chunk_token_num=100)
    assert len(chunks) > 3
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 60
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_line == previous.end_line + 1
    for chunk in chunks:
        assert "\n".join(lines_in(markdown, chunk)) == chunk.text
        assert chunk.token_count == count_tokens(chunk.text)
        assert chunk.token_count <= 150


def test_blank_lines_and_indentation_still_get_line_ranges() -> None:
    markdown = "# Title\n\n  indented paragraph one\n\nparagraph two\n\n\n" + numbered_lines(40)
    chunks = chunk_markdown(markdown, filename="a.md", chunk_token_num=100)
    source = markdown.split("\n")
    for chunk in chunks:
        chunk_lines = chunk.text.split("\n")
        covered = [line.strip() for line in lines_in(markdown, chunk) if line.strip()]
        assert covered == chunk_lines
    assert chunks[0].start_line == 1
    assert source[chunks[0].start_line - 1] == "# Title"
    assert chunks[-1].end_line == len(source)


def test_overlap_repeats_previous_tail_and_moves_range_back() -> None:
    markdown = numbered_lines(60)
    plain = chunk_markdown(markdown, filename="a.md", chunk_token_num=100, overlap_percent=0)
    overlapped = chunk_markdown(markdown, filename="a.md", chunk_token_num=100, overlap_percent=30)
    assert len(overlapped) >= len(plain)
    tail = overlapped[0].text[-10:]
    assert tail in overlapped[1].text
    assert overlapped[1].start_line is not None
    assert overlapped[1].start_line <= overlapped[0].end_line


def test_parameters_are_clamped() -> None:
    markdown = numbered_lines(400)
    base = chunk_markdown(markdown, filename="a.md", chunk_token_num=100)
    assert chunk_markdown(markdown, filename="a.md", chunk_token_num=1) == base
    assert chunk_markdown(markdown, filename="a.md", chunk_token_num=-50) == base
    assert chunk_markdown(markdown, filename="a.md", chunk_token_num=9999) == chunk_markdown(
        markdown, filename="a.md", chunk_token_num=2048
    )
    half = chunk_markdown(markdown, filename="a.md", chunk_token_num=200, overlap_percent=50)
    assert (
        chunk_markdown(markdown, filename="a.md", chunk_token_num=200, overlap_percent=99) == half
    )
    none = chunk_markdown(markdown, filename="a.md", chunk_token_num=200, overlap_percent=0)
    assert (
        chunk_markdown(markdown, filename="a.md", chunk_token_num=200, overlap_percent=-5) == none
    )
    default = chunk_markdown(markdown, filename="a.md")
    assert default == chunk_markdown(markdown, filename="a.md", chunk_token_num=512)


def test_oversized_single_line_is_hard_split_within_the_same_line() -> None:
    markdown = " ".join(f"w{i}" for i in range(1000))
    chunks = chunk_markdown(markdown, filename="a.txt", chunk_token_num=100)
    assert len(chunks) >= 6
    assert all(c.token_count <= 150 for c in chunks)
    assert all((c.start_line, c.end_line) == (1, 1) for c in chunks)
    assert " ".join(c.text for c in chunks) == markdown


def test_unknown_preset_uses_general() -> None:
    markdown = numbered_lines(30)
    assert chunk_markdown(markdown, filename="a.md", preset="nope") == chunk_markdown(
        markdown, filename="a.md", preset="general"
    )


def test_qa_preset_extracts_pairs_and_has_no_line_range() -> None:
    markdown = "Q: 什么是知识库？\nA: 存放文档的地方。\n\nQ: 如何分享？\nA: 输入工号。\n补充说明"
    chunks = chunk_markdown(markdown, filename="faq.md", preset="qa")
    assert [c.text for c in chunks] == [
        "问题：什么是知识库？\t回答：存放文档的地方。",
        "问题：如何分享？\t回答：输入工号。\n补充说明",
    ]
    assert all(c.start_line is None and c.end_line is None for c in chunks)
    assert [c.index for c in chunks] == [0, 1]


def test_qa_preset_heading_fallback() -> None:
    markdown = "# 怎么重置密码\n在设置里点击重置。\n\n# 怎么联系客服\n拨打内线 8000。"
    chunks = chunk_markdown(markdown, filename="faq.md", preset="qa")
    assert [c.text for c in chunks] == [
        "问题：怎么重置密码\t回答：在设置里点击重置。",
        "问题：怎么联系客服\t回答：拨打内线 8000。",
    ]


def test_book_preset_keeps_heading_path() -> None:
    markdown = "# 第一章 总则\n\n## 1.1 目的\n\n本手册用于规范报销流程。\n\n## 1.2 范围\n\n适用于全体员工。\n"
    chunks = chunk_markdown(markdown, filename="manual.md", preset="book", chunk_token_num=200)
    assert chunks
    joined = "\n".join(c.text for c in chunks)
    assert "本手册用于规范报销流程。" in joined and "适用于全体员工。" in joined
    covering = [c for c in chunks if "适用于全体员工。" in c.text]
    assert any("1.2 范围" in c.text for c in covering)
    located = [c for c in chunks if c.start_line is not None]
    assert located
    source = markdown.split("\n")
    for chunk in located:
        assert chunk.end_line is not None and chunk.start_line <= chunk.end_line <= len(source)


def test_laws_preset_splits_articles_under_chapters() -> None:
    markdown = (
        "# 某某管理条例\n\n第一章 总则\n\n第一条 为了规范管理，制定本条例。\n"
        "第二条 本条例适用于全体员工。\n\n第二章 附则\n\n第三条 本条例自公布之日起施行。\n"
    )
    chunks = chunk_markdown(markdown, filename="law.md", preset="laws")
    texts = [c.text for c in chunks]
    assert any("第一章 总则" in t and "第一条" in t and "制定本条例" in t for t in texts)
    assert any("第二章 附则" in t and "第三条" in t for t in texts)
    assert all(c.token_count > 0 for c in chunks)


def test_laws_docx_uses_heading_tree() -> None:
    markdown = "# 制度\n\n## 第一部分\n\n内容甲\n\n## 第二部分\n\n内容乙\n"
    chunks = chunk_markdown(markdown, filename="policy.docx", preset="laws")
    assert len(chunks) >= 2
    assert any("第一部分" in c.text and "内容甲" in c.text for c in chunks)
    assert any("第二部分" in c.text and "内容乙" in c.text for c in chunks)


@pytest.mark.parametrize("preset", list(CHUNK_PRESETS))
def test_no_internal_markup_leaks_into_chunks(preset: str) -> None:
    markdown = (
        "# 标题\n\n第一章 总则\n\n第一条 为了规范管理。\n\nQ: 问\nA: 答\n\n"
        + numbered_lines(120)
        + "\n\n联系：someone@example.com\n"
    )
    chunks = chunk_markdown(markdown, filename="doc.md", preset=preset, chunk_token_num=100)
    assert chunks
    for chunk in chunks:
        assert "@@" not in chunk.text and "##\t" not in chunk.text
        assert chunk.text == chunk.text.strip()
        assert chunk.text


@pytest.mark.parametrize("preset", list(CHUNK_PRESETS))
def test_indices_are_contiguous_and_chunking_is_deterministic(preset: str) -> None:
    sections = [
        f"## {i}.1 小节标题\n\n这是第 {i} 节的内容，包含若干文字用于测试。" for i in range(300)
    ]
    markdown = "# 总标题\n\n" + "\n\n".join(sections)
    first = chunk_markdown(markdown, filename="big.md", preset=preset)
    second = chunk_markdown(markdown, filename="big.md", preset=preset)
    assert first == second
    assert [c.index for c in first] == list(range(len(first)))
