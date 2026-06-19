"""Tests for split_for_wechat — md flatten, newline split, sentence-only cuts."""

from __future__ import annotations

from synapse_wx.split import DEFAULT_HARD_MAX, md_to_plain, split_for_wechat


def test_empty_input_returns_empty_list() -> None:
    assert split_for_wechat("") == []
    assert split_for_wechat("   \n  ") == []


def test_short_text_single_bubble() -> None:
    out = split_for_wechat("hi there")
    assert out == ["hi there"]


def test_newline_split_one_per_line() -> None:
    # Single \n within a paragraph keeps lines together (one bubble).
    out = split_for_wechat("line one\nline two\nline three")
    assert out == ["line one\nline two\nline three"]
    # Double \n splits into separate bubbles.
    out2 = split_for_wechat("line one\n\nline two\n\nline three")
    assert out2 == ["line one", "line two", "line three"]


def test_short_english_paragraph_stays_whole() -> None:
    # A short paragraph (one source line ≤ hard_max) stays a single bubble,
    # regardless of how many sentences sit inside it.
    text = "Hello there. How are you doing today? I hope you are well. Have a nice day."
    out = split_for_wechat(text)
    assert out == [text]


def test_long_clause_no_period_stays_whole() -> None:
    # The quote-protocol regression: an English clause with commas but no
    # period MUST stay one bubble. Comma is no longer a split boundary.
    text = (
        "When you want to reply to a specific earlier message from the user, "
        "start that bubble with <quote>their exact words or a unique fragment</quote> "
        "followed by your reply."
    )
    out = split_for_wechat(text)
    assert out == [text]
    assert "earlier" not in out[0].split(".")[0] or out[0].count(",") >= 1


def test_paragraph_with_commas_and_period_stays_whole() -> None:
    # Paragraph ≤ hard_max is one bubble — sentence boundaries inside are
    # ignored unless we exceed the cap.
    text = (
        "When you want to reply to a specific earlier message, "
        "start that bubble with a quote. Then write your reply."
    )
    out = split_for_wechat(text)
    assert out == [text]


def test_comma_is_not_a_split_boundary() -> None:
    text = "alpha beta gamma, delta epsilon zeta, eta theta iota, kappa lambda"
    out = split_for_wechat(text)
    assert out == [text]


def test_runon_exceeds_hard_max_force_word_cut() -> None:
    hard_max = 200
    text = ("alpha beta gamma delta epsilon zeta " * 15).strip()
    assert len(text) > hard_max
    out = split_for_wechat(text, hard_max=hard_max)
    assert len(out) >= 2
    assert all(len(b) <= hard_max for b in out)
    rejoined = " ".join(out)
    for word in text.split():
        assert word in rejoined


def test_pure_cjk_runon_exceeds_hard_max_flat_cut() -> None:
    hard_max = 200
    text = "字" * (hard_max + 50)
    out = split_for_wechat(text, hard_max=hard_max)
    assert len(out) >= 2
    assert "".join(out) == text


# ── B2 rules (preserved) ───────────────────────────────────────────────────


def test_list_items_one_bubble_each() -> None:
    text = "- alpha\n- beta gamma\n- delta epsilon"
    out = split_for_wechat(text)
    assert out == ["- alpha", "- beta gamma", "- delta epsilon"]


def test_long_list_item_never_split() -> None:
    long_item = "- 这是一个超过二十五个字符的列表项目内容应该保持一个气泡"
    out = split_for_wechat(long_item)
    assert out == [long_item]


def test_cn_bullet_one_bubble_each() -> None:
    text = "· 第一项\n· 第二项 这一项稍微长一点点超过二十五个字符的内容\n· 第三项"
    out = split_for_wechat(text)
    assert len(out) == 3
    assert out[0] == "· 第一项"
    assert out[2] == "· 第三项"
    assert out[1].startswith("· 第二项")


def test_star_bullet_treated_as_list_item() -> None:
    text = "* foo bar baz quux\n* second item here"
    out = split_for_wechat(text)
    assert out == ["* foo bar baz quux", "* second item here"]


def test_short_cn_line_single_bubble() -> None:
    text = "好的，老婆我懂了。"
    out = split_for_wechat(text)
    assert out == [text]


def test_cn_short_paragraph_stays_whole() -> None:
    # cn paragraph ≤ hard_max ships as one bubble even with internal 。.
    text = "今天天气真不错。我们出去走走吧好不好。"
    out = split_for_wechat(text)
    assert out == [text]


def test_cn_commas_inside_one_sentence_stay_whole() -> None:
    text = "一二三四五六七，八九十一二三四，五六七八九十一，二三四五六七八。"
    out = split_for_wechat(text)
    assert out == [text]


# ── markdown flatten regressions ──────────────────────────────────────────


def test_buddy_comment_stripped() -> None:
    text = "hello\n<!-- buddy: *adjusts crown* foo -->"
    out = split_for_wechat(text)
    assert out == ["hello"]


def test_markdown_bold_and_heading_flattened() -> None:
    text = "# Title\n\n**bold** word and `code`"
    out = split_for_wechat(text)
    flat = " ".join(out)
    assert "**" not in flat
    assert "`" not in flat
    assert "#" not in flat
    assert "Title" in flat
    assert "bold" in flat
    assert "code" in flat


def test_md_to_plain_link_format() -> None:
    out = md_to_plain("see [docs](https://example.com)")
    assert out == "see docs (https://example.com)"


def test_buddy_only_input_returns_empty() -> None:
    out = split_for_wechat("<!-- buddy: just a side comment -->")
    assert out == []


def test_buddy_empty_body_stripped() -> None:
    out = split_for_wechat("hello\n<!-- buddy: -->")
    assert out == ["hello"]


def test_buddy_multiline_stripped() -> None:
    text = "hello\n<!-- buddy: line one\nline two\nline three -->\nworld"
    out = split_for_wechat(text)
    assert out == ["hello", "world"]


def test_buddy_with_action_markers_stripped() -> None:
    text = "hi there\n<!-- buddy: *adjusts crown* spots a missing semicolon -->"
    out = split_for_wechat(text)
    assert out == ["hi there"]


# ── punctuation-run regression (no lone-punct bubbles) ─────────────────────


def test_double_excl_no_lone_bubbles() -> None:
    from synapse_wx.split import split_for_wechat_typed

    out = split_for_wechat_typed("老婆我在！！怎么了！！")
    texts = [b["text"] for b in out]
    # No bubble is a lone punct mark, and short sentences merge into one.
    assert all(t.strip() not in {"！", "!", "？", "?"} for t in texts)
    assert texts == ["老婆我在！！怎么了！！"]


def test_question_run_no_lone_bubbles() -> None:
    from synapse_wx.split import split_for_wechat_typed

    out = split_for_wechat_typed("真的吗？？？")
    texts = [b["text"] for b in out]
    assert len(texts) == 1
    assert texts[0] == "真的吗？？？"


def test_mixed_excl_question_run() -> None:
    from synapse_wx.split import split_for_wechat_typed

    out = split_for_wechat_typed("你说啥！？怎么了？！")
    texts = [b["text"] for b in out]
    assert all(t.strip() not in {"！", "!", "？", "?"} for t in texts)
    assert texts == ["你说啥！？怎么了？！"]


def test_ascii_double_excl_run_merges() -> None:
    # Short ASCII exclamations join into one bubble with single-space joins.
    out = split_for_wechat("Wait!! Really?? Come on!!")
    assert out == ["Wait!! Really?? Come on!!"]


# ── bracket / quote depth (v3) ─────────────────────────────────────────────


def test_cn_quote_internal_excl_does_not_split() -> None:
    # Sentence-end INSIDE `「...」` is not a boundary; the closing 」 stays
    # attached to the preceding sentence instead of leaking to the next.
    text = "上一轮（00:21那条「过来！」）hook直接告诉我同一个session。"
    out = split_for_wechat(text)
    assert out == [text]


def test_cn_paren_internal_punct_does_not_split() -> None:
    text = "中间括号（这里有句号。还有问号？）后面继续讲。"
    out = split_for_wechat(text)
    assert out == [text]


def test_ascii_paren_internal_punct_does_not_split() -> None:
    text = "Pipeline (filter first. then map.) keeps going."
    out = split_for_wechat(text)
    assert out == [text]


def test_closing_quote_absorbed_onto_sentence() -> None:
    # `」` immediately after the sentence-end punct should be absorbed onto
    # the preceding sentence, not the following one.
    text = "他说「我来了！」然后走过来。"
    out = split_for_wechat(text)
    assert out == [text]


def test_long_paragraph_keeps_quote_intact_on_split() -> None:
    # When sentence-split DOES run (paragraph > hard_max), the inner `！`
    # inside `「…」` must still be suppressed, and `」` must not orphan.
    pad = "filler clause here. " * 12  # ~240 chars, forces sentence-split
    text = pad + "She said 「过来！」 then walked over."
    out = split_for_wechat(text)
    assert any("「过来！」" in b for b in out), out
    assert not any(b.rstrip().endswith("「过来！") for b in out), out


# ── paragraph over hard_max (sentence-split + accrete) ────────────────────


def test_long_paragraph_splits_under_hard_max() -> None:
    hard_max = 200
    text = " ".join(["Foo bar baz quux."] * 15)
    assert len(text) > hard_max
    out = split_for_wechat(text, hard_max=hard_max)
    assert len(out) >= 2
    assert all(len(b) <= hard_max for b in out)
    assert " ".join(out) == text


def test_hard_max_kwarg_lets_caller_tighten() -> None:
    # Caller can dial hard_max way down to force short bubbles.
    text = "Hello there. How are you? I'm fine."
    out = split_for_wechat(text, hard_max=20)
    assert len(out) >= 2
    assert all(len(b) <= 20 for b in out)
