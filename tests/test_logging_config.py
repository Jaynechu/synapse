from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from synapse_core.logging_config import configure_logging

QUIET = ("apscheduler", "httpx", "httpcore")


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {n: logging.getLogger(n).level for n in QUIET}
    yield root
    for h in list(root.handlers):
        if getattr(h, "_synapse_configured", False):
            root.removeHandler(h)
            h.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for n, lvl in saved_levels.items():
        logging.getLogger(n).setLevel(lvl)


def _ours(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_synapse_configured", False)]


def test_only_a_rotating_file_handler_is_installed(clean_root, tmp_path: Path) -> None:
    configure_logging(tmp_path / "a" / "b.log", 7)
    ours = _ours(clean_root)
    assert len(ours) == 1
    assert isinstance(ours[0], TimedRotatingFileHandler)
    assert ours[0].backupCount == 7
    assert not [h for h in clean_root.handlers
                if type(h) is logging.StreamHandler
                and h.stream in (sys.stderr, sys.stdout)]


def test_reconfigure_replaces_its_own_handler(clean_root, tmp_path: Path) -> None:
    configure_logging(tmp_path / "one.log", 14)
    configure_logging(tmp_path / "two.log", 14)
    ours = _ours(clean_root)
    assert len(ours) == 1
    assert Path(ours[0].baseFilename) == tmp_path / "two.log"


def test_http_poll_lines_are_dropped(clean_root, tmp_path: Path) -> None:
    log = tmp_path / "svc" / "svc.log"
    configure_logging(log, 14)
    for name in QUIET:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    logging.getLogger("httpx").info(
        'HTTP Request: POST https://api.telegram.org/botX/getUpdates "HTTP/1.1 200 OK"')
    logging.getLogger("httpcore.http11").debug("receive_response_headers.started")
    logging.getLogger("synapse_tg.loop").info("inbound message")
    for h in _ours(clean_root):
        h.flush()
    text = log.read_text()
    assert "getUpdates" not in text
    assert "receive_response_headers" not in text
    assert "inbound message" in text
