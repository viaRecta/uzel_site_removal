"""Logging to console (rich) and to a per-run file inside the client folder. Secrets are redacted."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler


class RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True


_redactor = RedactSecrets([])


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = RichHandler(level=logging.DEBUG if verbose else logging.INFO, show_path=False, markup=False)
    console.addFilter(_redactor)
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_secrets(secrets: list[str]) -> None:
    _redactor.secrets = [s for s in secrets if s and len(s) >= 6]


def add_file_log(path: Path) -> logging.Handler:
    """Log file lives in the client's run folder (client data is sensitive; purge removes it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_redactor)
    logging.getLogger().addHandler(handler)
    return handler
