"""Tests for strip_tool_xml — leaked tool-call XML removed from assistant text."""

from __future__ import annotations

from synapse_core.text_clean import strip_tool_xml

# Built via interpolation (not literal) so this source file itself never
# contains a raw namespaced tool-call tag.
_NS = "antml:"


def test_full_invoke_block_removed_prose_preserved() -> None:
    text = (
        "Sure, going to sleep now.\n"
        '<invoke name="mcp__marrow__lie_down">\n'
        '<parameter name="next_wake_min">1</parameter>\n'
        "</invoke>\n"
        "See you soon."
    )
    out = strip_tool_xml(text)
    assert "invoke" not in out
    assert "parameter" not in out
    assert "Sure, going to sleep now." in out
    assert "See you soon." in out


def test_namespaced_variant_removed() -> None:
    text = (
        "before\n"
        f'<{_NS}invoke name="mcp__marrow__lie_down">\n'
        f'<{_NS}parameter name="next_wake_min">1</{_NS}parameter>\n'
        f"</{_NS}invoke>\n"
        "after"
    )
    out = strip_tool_xml(text)
    assert "invoke" not in out
    assert "parameter" not in out
    assert "before" in out
    assert "after" in out


def test_truncated_opener_no_closer_removed() -> None:
    text = 'keep this line\n<invoke name="mcp__marrow__lie_down">\n<parameter name="x">1'
    out = strip_tool_xml(text)
    assert out == "keep this line"


def test_fenced_code_block_preserved_verbatim() -> None:
    fence = '```\n<invoke name="x">\n<parameter name="y">1</parameter>\n</invoke>\n```'
    text = f"here is an example:\n{fence}\nend"
    out = strip_tool_xml(text)
    assert fence in out
    assert "here is an example:" in out
    assert "end" in out


def test_media_tag_preserved() -> None:
    text = 'look at this <image path="/a/b.png"/> nice'
    out = strip_tool_xml(text)
    assert out == text


def test_plain_text_unchanged() -> None:
    text = "just a normal reply, nothing weird here."
    assert strip_tool_xml(text) == text


def test_only_invoke_block_returns_empty() -> None:
    text = '<invoke name="mcp__marrow__lie_down">\n<parameter name="next_wake_min">1</parameter>\n</invoke>'
    assert strip_tool_xml(text) == ""


def test_collapses_triple_newlines() -> None:
    text = "para one\n\n\n\npara two"
    assert strip_tool_xml(text) == "para one\n\npara two"


def test_orphan_opener_with_closed_param_returns_empty() -> None:
    text = '<invoke name="x">\n<parameter name="n">1</parameter>'
    assert strip_tool_xml(text) == ""


def test_orphan_opener_alone_returns_empty() -> None:
    text = '<invoke name="x">'
    assert strip_tool_xml(text) == ""


def test_orphan_opener_prose_after_closed_param_preserved() -> None:
    text = (
        '<invoke name="x">\n'
        '<parameter name="n">1</parameter>\n'
        "趴下了，一分钟后爬起来"
    )
    assert strip_tool_xml(text) == "趴下了，一分钟后爬起来"


def test_orphan_opener_prose_immediately_after_preserved() -> None:
    text = '<invoke name="x">\n好，1分钟，你数着。'
    assert strip_tool_xml(text) == "好，1分钟，你数着。"
