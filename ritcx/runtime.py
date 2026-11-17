"""Connection profiles and a local, process-safe lock for the simulator account."""

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

PRACTICE_URL = "http://flserver.rotman.utoronto.ca:16520/v1"


class TradingError(RuntimeError):
    pass


def validate_profile(cfg, practice=False, contest=False, trade=False):
    mode = cfg.get("RIT_ENVIRONMENT", "practice")
    if mode not in ("practice", "contest") or (practice and contest):
        raise TradingError("Choose one simulation environment: practice or contest")
    if (practice and mode != "practice") or (contest and mode != "contest"):
        raise TradingError("Selected mode differs from RIT_ENVIRONMENT in the config file")
    if trade and not (practice or contest):
        raise TradingError("Trading requires --practice or --contest to match the config")
    kind = cfg.get("RIT_API_KIND", "dma")
    if kind not in ("dma", "rest"):
        raise TradingError("RIT_API_KIND must be dma or rest")
    if not cfg.get("RIT_USERNAME") or not cfg.get(
        "RIT_API_KEY" if kind == "rest" else "RIT_PASSWORD"
    ):
        raise TradingError("Set the account ID and the password (DMA) or API key (client REST)")
    url = cfg.get("RIT_API_URL", "")
    if (
        mode == "practice"
        and kind == "dma"
        and urlsplit(url).hostname != "flserver.rotman.utoronto.ca"
    ):
        raise TradingError(
            "Practice DMA requires the official flserver.rotman.utoronto.ca host; use the assigned DMA port"
        )
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.port
            and parsed.path == "/v1"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise TradingError("Configure the organizer-provided simulator URL including port and /v1")


@contextmanager
def account_lock(cfg, inherited_fd=None, directory=None):
    # Key by account, rather than URL: aliases of the same host must not defeat it.
    directory = directory or ROOT / "logs" / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(cfg["RIT_USERNAME"].encode()).hexdigest()[:24]
    path = directory / (key + ".lock")
    fd = None
    try:
        if inherited_fd is None:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TradingError(
                    "Another local bot holds this account. Stop it before starting a second."
                ) from None
        else:
            try:
                expected = path.stat()
                actual = os.fstat(inherited_fd)
                if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                    raise OSError("Different account lock")
                fd = os.dup(inherited_fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TradingError("Invalid inherited account lock") from None
        yield fd
    finally:
        # Never unlink or explicitly unlock: the runner and child share the open
        # file description, and the lock must survive until both have exited.
        if fd is not None:
            os.close(fd)


@contextmanager
def open_audit_log(directory, prefix):
    directory.mkdir(exist_ok=True, parents=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.NamedTemporaryFile(
        mode="w", prefix=f"{prefix}-{stamp}-", suffix=".jsonl", dir=directory, delete=False
    ) as stream:
        yield Path(stream.name), stream


def read_config(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    keys = {
        "RIT_ENVIRONMENT",
        "RIT_API_KIND",
        "RIT_API_URL",
        "RIT_USERNAME",
        "RIT_PASSWORD",
        "RIT_API_KEY",
        "RIT_LOG_DIR",
    }
    return {
        k: os.environ[k] if k in os.environ else values[k]
        for k in keys
        if k in os.environ or k in values
    }
