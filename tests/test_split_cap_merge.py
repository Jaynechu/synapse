"""Tests for merge_bubbles_to_cap — outbound-edge bubble cap (quota defense)."""

from __future__ import annotations

from synapse_wx.split import merge_bubbles_to_cap


def _text(*texts: str) -> list[dict]:
    return [{"kind": "text", "text": t} for t in texts]


def test_under_cap_untouched() -> None:
    bubbles = _text("a", "b", "c")
    out = merge_bubbles_to_cap(bubbles, cap=5)
    assert out == bubbles
    # Pure function: input not mutated.
    assert bubbles == _text("a", "b", "c")


def test_over_cap_text_merges_to_cap() -> None:
    bubbles = _text("a", "b", "c", "d", "e")
    out = merge_bubbles_to_cap(bubbles, cap=2)
    assert len(out) == 2
    assert all(b["kind"] == "text" for b in out)
    # Every original fragment survives, in order.
    joined = "\n".join(b["text"] for b in out)
    for frag in ("a", "b", "c", "d", "e"):
        assert frag in joined
    assert joined.index("a") < joined.index("e")


def test_exactly_at_cap_untouched() -> None:
    bubbles = _text("a", "b", "c")
    out = merge_bubbles_to_cap(bubbles, cap=3)
    assert out == bubbles


def test_media_bubbles_unmergeable_order_preserved() -> None:
    bubbles = [
        {"kind": "text", "text": "t1"},
        {"kind": "image", "path": "/a.png"},
        {"kind": "text", "text": "t2"},
        {"kind": "text", "text": "t3"},
        {"kind": "video", "path": "/v.mp4"},
        {"kind": "text", "text": "t4"},
    ]
    out = merge_bubbles_to_cap(bubbles, cap=4)
    # Media bubbles stay intact and keep relative order.
    media = [b for b in out if b["kind"] in ("image", "video")]
    assert media == [
        {"kind": "image", "path": "/a.png"},
        {"kind": "video", "path": "/v.mp4"},
    ]
    # t2 and t3 (adjacent text) merged into one bubble.
    texts = [b["text"] for b in out if b["kind"] == "text"]
    assert "t2\nt3" in texts
    # image precedes video in the output.
    kinds = [b["kind"] for b in out]
    assert kinds.index("image") < kinds.index("video")


def test_media_only_cannot_reach_cap() -> None:
    # No adjacent text pairs → merging stops, count stays above cap.
    bubbles = [
        {"kind": "image", "path": "/1.png"},
        {"kind": "image", "path": "/2.png"},
        {"kind": "image", "path": "/3.png"},
    ]
    out = merge_bubbles_to_cap(bubbles, cap=1)
    assert out == bubbles


def test_char_ceiling_stops_growth() -> None:
    # Two big bubbles that would exceed the ceiling if merged; with a small
    # ceiling the merge is skipped so the pair is left as two bubbles.
    big = "x" * 2000
    bubbles = _text(big, big, "small")
    out = merge_bubbles_to_cap(bubbles, cap=1, char_ceiling=3900)
    # big+big = 4001 chars > 3900 → cannot merge those two. But big+"small"
    # and the trailing pairs can still merge until no pair fits.
    # Result must never contain a bubble over the ceiling.
    assert all(len(b["text"]) <= 3900 for b in out)


def test_char_ceiling_blocks_all_merges_leaves_over_cap() -> None:
    big = "x" * 3800
    bubbles = _text(big, big, big)
    out = merge_bubbles_to_cap(bubbles, cap=1, char_ceiling=3900)
    # Any merge would exceed 3900 → no merge happens, count stays at 3.
    assert len(out) == 3
    assert all(len(b["text"]) <= 3900 for b in out)
