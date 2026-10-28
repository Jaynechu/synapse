from __future__ import annotations

import io
from pathlib import Path

from synapse_core.providers.cc import _drain_stderr
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class _Captured:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def test_tg_cc_stderr_sits_next_to_the_app_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _Captured)
    log_file = tmp_path / "logs" / "synapse-tg-chat" / "synapse-tg-chat.log"
    cfg = TgConfig(data_dir=tmp_path / "data", marrow_db="", log_file=log_file)
    provider = TgLoop(cfg)._make_provider()
    assert provider.kwargs["stderr_log"] == log_file.parent / "cc-stderr.log"


def test_drain_creates_the_log_dir(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "cc-stderr.log"
    _drain_stderr(io.StringIO("boom\n\n"), target)
    assert target.read_text().rstrip().endswith(" boom")
