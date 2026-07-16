"""Small shared helpers: parsing, hashing, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run_stamp() -> str:
    # Seconds alone collide when two runs of one preset start together.
    fraction = time.time_ns() % 1_000_000_000
    return (f"{time.strftime('%Y%m%d-%H%M%S')}-{fraction:09d}-"
            f"{os.getpid()}-{uuid.uuid4().hex[:8]}")


@contextmanager
def advisory_lock(path, label="resource"):
    """Hold a fail-fast cross-process lock without deleting its stable inode."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                if not fh.read(1):
                    fh.seek(0)
                    fh.write(" ")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError):
            fh.seek(0)
            owner = fh.read().strip()
            detail = f" ({owner})" if owner else ""
            raise RuntimeError(f"{label} is already in use{detail}")
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                             "started": now_iso()}))
        fh.flush()
        yield p
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def parse_bitrate_kbps(text) -> int:
    """Parse '6000k', '6M', '6.5m' or plain '6000' (kbps) into integer kbps."""
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().lower()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([km]?)(?:bps)?", s)
    if not m:
        raise ValueError(f"cannot parse bitrate {text!r} (use e.g. 6000k or 6M)")
    v = float(m.group(1))
    if m.group(2) == "m":
        v *= 1000.0
    return int(round(v))


def parse_time_seconds(text) -> float:
    """Parse seconds ('95', '95.5') or timestamps ('MM:SS', 'HH:MM:SS[.frac]')."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if re.fullmatch(r"[0-9]*\.?[0-9]+", s):
        return float(s)
    parts = s.split(":")
    if len(parts) in (2, 3) and all(p != "" for p in parts):
        parts = [float(p) for p in parts]
        secs = 0.0
        for p in parts:
            secs = secs * 60.0 + p
        return secs
    raise ValueError(f"cannot parse time {text!r} (use seconds or HH:MM:SS)")


def parse_fps(rate: str) -> float:
    """Parse an ffprobe rational like '25/1' into a float fps."""
    if not rate:
        return 0.0
    if "/" in str(rate):
        num, den = str(rate).split("/", 1)
        num, den = float(num), float(den)
        return num / den if den else 0.0
    return float(rate)


def fingerprint_file(path, chunk=32 * 1024 * 1024) -> str:
    """Cheap content fingerprint: sha256 over head + tail chunks + size."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(p, "rb") as fh:
        h.update(fh.read(chunk))
        if size > 2 * chunk:
            fh.seek(size - chunk)
            h.update(fh.read(chunk))
    return h.hexdigest()


def atomic_write_json(path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text) -> None:
    """Atomically replace a UTF-8 text file in the same directory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(text))
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def coerce_value(text: str):
    """Parse a CLI-supplied parameter value into int/float/None/str."""
    s = str(text).strip()
    if s.lower() in ("none", "null", "unset"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s
