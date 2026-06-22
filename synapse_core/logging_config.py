"""Shared logging setup for Synapse bridges."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
BACKUP_COUNT = 14


def configure_logging(log_path: Path) -> None:
    level = getattr(
        logging,
        os.environ.get("SYNAPSE_LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    )
    log_path = log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        if getattr(handler, "_synapse_configured", False):
            root.removeHandler(handler)
            handler.close()

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._synapse_configured = True  # type: ignore[attr-defined]

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    stream_handler._synapse_configured = True  # type: ignore[attr-defined]

    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
