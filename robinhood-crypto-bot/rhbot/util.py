from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import requests

log = logging.getLogger("rhbot")


def write_json_atomic(path: str | Path, obj) -> None:
    """Write to a temp file then rename over the target, so a crash or power
    cut mid-write leaves the previous state intact instead of a truncated,
    unreadable file (which would make the bot forget its open positions)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def notify(message: str, title: str = "rhbot", priority: str = "default") -> None:
    """Push a phone notification via ntfy.sh if RHBOT_NTFY_TOPIC is set.
    Install the ntfy app, subscribe to a long random topic name, and put the
    same name in the env file. Failures are logged, never raised."""
    topic = os.environ.get("RHBOT_NTFY_TOPIC")
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode(),
                      headers={"Title": title, "Priority": priority}, timeout=10)
    except Exception:
        log.warning("notification failed", exc_info=True)


def single_instance(path: str | Path):
    """Hold an exclusive lock on `path` for the life of the process, so two
    copies of the bot can never trade the same account at once. Returns the
    open file (keep a reference); exits if another copy holds the lock."""
    import sys

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"another rhbot is already running (lock held on {path})")
    return f
