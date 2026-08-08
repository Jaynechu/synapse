"""Inbound media orchestrator (C0).

Materializes one media event from `ILinkClient.extract_media` to a local
file path. The bridge loop collects these paths per turn and injects a
`Use the Read tool to view: <path1>, <path2>` line into the prompt so cc
can pull the files via its native Read tool.

Voice events carry inline text (iLink server-side transcription) — we
write a .txt sidecar instead of downloading.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# B9: per-kind consecutive inbound failure counter (reset on success).
# Only AlertSink on 2nd+ consecutive failure (two-strike contract).
_inbound_fail_counts: dict[str, int] = {}
_inbound_alert_sink = None  # set by loop via set_inbound_alert_sink()


def set_inbound_alert_sink(sink: Any) -> None:
    """Wire the AlertSink from the bridge loop (called once at startup)."""
    global _inbound_alert_sink
    _inbound_alert_sink = sink


def _record_inbound_failure(kind: str, detail: str) -> None:
    _inbound_fail_counts[kind] = _inbound_fail_counts.get(kind, 0) + 1
    count = _inbound_fail_counts[kind]
    logger.warning("media inbound %s failed (consecutive=%d): %s", kind, count, detail)
    if count >= 2 and _inbound_alert_sink is not None:
        try:
            _inbound_alert_sink.write(
                "warn",
                "media_in_failed",
                f"kind={kind} consecutive={count}: {detail}",
                source="media.inbound",
                fingerprint="media_in_failed",
            )
        except Exception as e:
            logger.warning("alert sink write failed: %s", e)


def _record_inbound_success(kind: str) -> None:
    _inbound_fail_counts.pop(kind, None)


# Default file extensions per media type. WeChat does not advertise mime
# in extract_media output; we pick a permissive default and fall back to
# `filename` hint when present (files).
_DEFAULT_SUFFIX = {
    "image": ".jpg",
    "voice": ".m4a",  # only used when no inline text
    "video": ".mp4",
    "file": ".bin",
}

# kind → subfolder name. Capitalised + plural to match the user's hand-managed
# layout (mirrors macOS ~/Pictures, ~/Documents style). "Transcripts" covers
# both voice STT and video-frame STT, so voice items land there too.
_KIND_DIRS = {
    "image": "Images",
    "voice": "Transcripts",
    "video": "Videos",
    "file": "Files",
}

_READ_TOOL_PREFIX = "Use the Read tool to view: "

# cc Read tool can read any PDF page range via the `pages=` param, so the
# only reason to pre-extract is to save round-trips (one extract vs many
# 20-page reads). Threshold: pages > 20.
_PDF_PAGE_LIMIT = 20


def _pdf_needs_pre_extract(path: Path) -> bool:
    """True when pre-extracting the PDF to text saves cc Read round-trips.

    cc Read tool can read any PDF page range with `pages=`; pre-extract
    only when >20 pages to avoid many round-trips burning tokens. If
    `pdfinfo` is missing or parsing fails, default False (trust cc Read
    to multi-step) — capability gate is on cc's side, not ours.
    """
    if not shutil.which("pdfinfo"):
        return False

    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("pdfinfo failed: %s", e)
        return False

    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                pages = int(line.split(":", 1)[1].strip())
            except ValueError:
                return False
            return pages > _PDF_PAGE_LIMIT
    return False


def _stamp() -> str:
    """`YYYY-MM-DD_HHMMSS_<uuid6>` timestamp for media filenames."""
    return datetime.now().strftime("%Y-%m-%d_%H%M%S_") + uuid.uuid4().hex[:6]


def _suffix_for(event: dict, kind: str) -> str:
    """Pick a file suffix: prefer filename hint (files), else type default."""
    filename = event.get("filename") or event.get("file_name")
    if isinstance(filename, str) and "." in filename:
        return Path(filename).suffix or _DEFAULT_SUFFIX.get(kind, ".bin")
    return _DEFAULT_SUFFIX.get(kind, ".bin")


def materialize(event: dict, ilink: Any, dest_dir: Path) -> list[Path]:
    """Materialize one media event to one or more local Paths.

    Returns an empty list on failure. `event` shape mirrors
    `ILinkClient.extract_media`:
      - image / file: {cdn_url, aes_key, encrypt_query_param, [filename]}
      - voice: {text}  (iLink server-side STT)
      - video: {cdn_url, aes_key, encrypt_query_param, play_length, thumb:{...}}

    Video fans out to mp4 + thumb jpg + 0..N ffmpeg keyframes so cc Read
    (which can't parse .mp4 binaries) can still reason over still
    frames. All other types return a single-element list.
    """
    kind = event.get("type")
    if kind not in ("image", "voice", "video", "file"):
        logger.warning("materialize: unknown media kind %r", kind)
        return []

    kind_dir = dest_dir / _KIND_DIRS[kind]
    kind_dir.mkdir(parents=True, exist_ok=True)

    # voice path: inline transcription, no download.
    if kind == "voice":
        text = event.get("text")
        if not isinstance(text, str) or not text:
            return []
        sidecar = kind_dir / f"{_stamp()}.txt"
        sidecar.write_text(text)
        return [sidecar]

    if kind == "video":
        return _materialize_video(event, ilink, dest_dir)

    cdn_url = event.get("cdn_url", "")
    aes_key = event.get("aes_key", "")
    qp = event.get("encrypt_query_param", "")
    if not cdn_url and not qp:
        logger.warning("materialize: media event missing cdn_url + qp")
        return []

    suffix = _suffix_for(event, kind)
    out_path = kind_dir / f"{_stamp()}{suffix}"

    ok = ilink.download_media(cdn_url, aes_key, out_path, qp)
    if not ok or not out_path.exists():
        _record_inbound_failure(kind, "download_media returned falsy")
        return []

    # PDF strategy: prefer the raw .pdf path — cc Read handles any PDF
    # via the `pages=` param, so pre-extraction is a token-saving
    # optimisation (one extract vs many 20-page reads), not a capability
    # gate. Only pre-extract for >20 pages; on extractor failure, return
    # the raw path so cc Read can still multi-step.
    if kind == "file" and out_path.suffix.lower() == ".pdf":
        if _pdf_needs_pre_extract(out_path):
            from . import pdf as pdf_mod

            sidecar = pdf_mod.extract_text(out_path)
            if sidecar is not None:
                _record_inbound_success(kind)
                return [sidecar]
            # pdf extract failed — fall through to return raw path (not a failure)

    _record_inbound_success(kind)
    return [out_path]


def _materialize_video(event: dict, ilink: Any, dest_dir: Path) -> list[Path]:
    """Download mp4 + thumb, then ffmpeg-extract extra keyframes.

    Returns [mp4, thumb, *keyframes] in that order. Empty list only when
    the mp4 itself fails — thumb / ffmpeg failures degrade gracefully.
    """
    cdn_url = event.get("cdn_url", "")
    aes_key = event.get("aes_key", "")
    qp = event.get("encrypt_query_param", "")
    if not cdn_url and not qp:
        logger.warning("materialize: video event missing cdn_url + qp")
        return []

    video_dir = dest_dir / _KIND_DIRS["video"]
    image_dir = dest_dir / _KIND_DIRS["image"]
    video_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    stamp = _stamp()
    mp4_suffix = _suffix_for(event, "video")
    mp4_path = video_dir / f"{stamp}{mp4_suffix}"
    if not ilink.download_media(cdn_url, aes_key, mp4_path, qp) or not mp4_path.exists():
        _record_inbound_failure("video", "download_media returned falsy")
        return []

    _record_inbound_success("video")
    paths: list[Path] = [mp4_path]

    # Thumb pairs with the mp4 by stem so callers can match them later.
    thumb = event.get("thumb") or {}
    thumb_url = thumb.get("cdn_url", "")
    thumb_qp = thumb.get("encrypt_query_param", "")
    if thumb_url or thumb_qp:
        thumb_path = image_dir / f"{stamp}_thumb.jpg"
        try:
            ok = ilink.download_media(
                thumb_url, thumb.get("aes_key", ""), thumb_path, thumb_qp
            )
        except Exception as e:
            logger.warning("video thumb download failed: %s", e)
            ok = False
        if ok and thumb_path.exists():
            paths.append(thumb_path)

    # ffmpeg keyframes — never fail the whole materialize on this path.
    play_length = int(event.get("play_length", 0) or 0)
    try:
        from . import video as video_mod

        kf_paths = video_mod.ffmpeg_keyframes(mp4_path, image_dir, stamp, play_length)
    except Exception as e:
        logger.warning("ffmpeg keyframes failed: %s", e)
        kf_paths = []
    paths.extend(kf_paths)

    return paths


def build_read_tool_instruction(paths: list[Path]) -> str:
    """Build the `Use the Read tool to view: ...` prompt injection line.

    Returns empty string if no paths so callers can safely concat.
    """
    if not paths:
        return ""
    joined = ", ".join(str(p) for p in paths)
    return f"{_READ_TOOL_PREFIX}{joined}"
