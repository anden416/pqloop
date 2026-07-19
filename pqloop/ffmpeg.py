"""Thin wrapper around ffmpeg/ffprobe binaries with capability probing."""

from __future__ import annotations

import hashlib
import json
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


_TOOL_IDENTITY_CACHE = {}


def _tool_identity(binary) -> dict:
    """Stable, path-independent identity for an ffmpeg-family executable."""
    named = str(binary)
    resolved = shutil.which(named) or named
    path = Path(resolved)
    try:
        real = path.resolve(strict=True)
        stat = real.stat()
        cache_key = (str(real), stat.st_size, stat.st_mtime_ns)
    except OSError:
        real = path
        cache_key = (str(path), None, None)
    if cache_key in _TOOL_IDENTITY_CACHE:
        return dict(_TOOL_IDENTITY_CACHE[cache_key])

    try:
        cp = subprocess.run([str(real), "-version"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=30)
        version_output = cp.stdout or ""
    except (OSError, subprocess.SubprocessError):
        version_output = "unavailable"
    digest = hashlib.sha256()
    digest.update(version_output.encode("utf-8", "replace"))
    try:
        with open(real, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        # Named PATH tools and unusual wrappers can be unhashable; their full
        # version banner still differentiates normal build changes.
        pass
    first = version_output.splitlines()[0] if version_output.splitlines() else "unknown"
    identity = {"version": first, "sha256": digest.hexdigest()}
    _TOOL_IDENTITY_CACHE[cache_key] = identity
    return dict(identity)


class FFmpegError(RuntimeError):
    def __init__(self, message, cmd=None, stderr=""):
        super().__init__(message)
        self.cmd = cmd or []
        self.stderr = stderr


def _stderr_tail(text, limit=2000) -> str:
    text = (text or "").strip()
    return text[-limit:]


class FF:
    """One ffmpeg/ffprobe pair. Separate instances may point at different builds
    (e.g. a netint/nvenc encode build and a libvmaf-enabled measurement build)."""

    def __init__(self, ffmpeg="ffmpeg", ffprobe=None):
        self.ffmpeg = str(ffmpeg)
        if ffprobe:
            self.ffprobe = str(ffprobe)
        else:
            sib = Path(self.ffmpeg).parent / "ffprobe"
            self.ffprobe = str(sib) if sib.parent != Path(".") and sib.exists() else "ffprobe"
        self._caps = {}

    def run(self, args, timeout=None) -> subprocess.CompletedProcess:
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", *[str(a) for a in args]]
        try:
            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout)
        except FileNotFoundError:
            raise FFmpegError(f"ffmpeg binary not found: {self.ffmpeg}", cmd)
        except subprocess.TimeoutExpired:
            raise FFmpegError(f"ffmpeg timed out after {timeout}s", cmd)
        if cp.returncode != 0:
            raise FFmpegError(
                f"ffmpeg failed (rc={cp.returncode}): ...{_stderr_tail(cp.stderr, 800)}",
                cmd, _stderr_tail(cp.stderr))
        return cp

    def run_progress(self, args, progress, timeout=None) -> subprocess.CompletedProcess:
        """Run ffmpeg and emit each ``-progress`` record as a dictionary.

        ffmpeg normally writes its human-readable status to stderr.  ``run``
        captures that stream so callers get useful diagnostics on failure,
        which also makes long encodes appear silent.  The machine-readable
        progress pipe lets us expose status while a background thread keeps
        draining stderr for the same failure diagnostics.
        """
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-nostats",
               "-progress", "pipe:1", *[str(a) for a in args]]
        try:
            cp = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", bufsize=1)
        except FileNotFoundError:
            raise FFmpegError(f"ffmpeg binary not found: {self.ffmpeg}", cmd)

        events = queue.Queue()
        stderr_lines = deque(maxlen=200)

        def read_progress():
            try:
                for line in cp.stdout:
                    events.put(("line", line))
            finally:
                events.put(("stdout_done", None))

        def read_stderr():
            try:
                for line in cp.stderr:
                    stderr_lines.append(line)
            finally:
                events.put(("stderr_done", None))

        readers = [
            threading.Thread(target=read_progress, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for reader in readers:
            reader.start()

        started = time.monotonic()
        record = {}
        finished_streams = set()
        try:
            while len(finished_streams) < 2:
                if timeout is None:
                    wait_for = 0.25
                else:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    wait_for = min(0.25, remaining)
                try:
                    kind, value = events.get(timeout=wait_for)
                except queue.Empty:
                    continue
                if kind.endswith("_done"):
                    finished_streams.add(kind)
                    continue
                key, separator, value = value.rstrip("\r\n").partition("=")
                if not separator:
                    continue
                record[key] = value
                if key == "progress":
                    progress(dict(record))
                    record.clear()

            remaining = None if timeout is None else max(
                0.01, timeout - (time.monotonic() - started))
            returncode = cp.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            cp.kill()
            cp.wait()
            for reader in readers:
                reader.join(timeout=1)
            raise FFmpegError(f"ffmpeg timed out after {timeout}s", cmd)
        except BaseException:
            if cp.poll() is None:
                cp.kill()
                cp.wait()
            raise
        finally:
            if cp.poll() is None:
                cp.kill()
                cp.wait()
            for reader in readers:
                reader.join(timeout=1)
            if cp.stdout:
                cp.stdout.close()
            if cp.stderr:
                cp.stderr.close()

        stderr = "".join(stderr_lines)
        if returncode != 0:
            raise FFmpegError(
                f"ffmpeg failed (rc={returncode}): ...{_stderr_tail(stderr, 800)}",
                cmd, _stderr_tail(stderr))
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    def probe(self, url, input_args=(), timeout=90) -> dict:
        cmd = [self.ffprobe, "-v", "error", *[str(a) for a in input_args],
               "-print_format", "json", "-show_format", "-show_streams",
               "-show_programs", str(url)]
        try:
            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout)
        except FileNotFoundError:
            raise FFmpegError(f"ffprobe binary not found: {self.ffprobe}", cmd)
        except subprocess.TimeoutExpired:
            raise FFmpegError(f"ffprobe timed out after {timeout}s probing {url}", cmd)
        if cp.returncode != 0:
            raise FFmpegError(f"ffprobe failed on {url}: {_stderr_tail(cp.stderr, 500)}",
                              cmd, _stderr_tail(cp.stderr))
        return json.loads(cp.stdout or "{}")

    def probe_entries(self, url, section, entries, select=None,
                      read_intervals=None, timeout=600) -> list:
        """Stream per-packet/per-frame fields as a list of dicts (compact
        key=value output, so field order never matters). Used for keyframe
        alignment checks: packet=pts_time,flags over a whole VOD file is
        demux-only and fast; frame-level probes decode, so bound them with
        read_intervals."""
        return list(self.iter_probe_entries(
            url, section, entries, select=select,
            read_intervals=read_intervals, timeout=timeout))

    def iter_probe_entries(self, url, section, entries, select=None,
                           read_intervals=None, timeout=600):
        """Yield compact ffprobe rows without buffering a whole VOD in RAM."""
        cmd = [self.ffprobe, "-v", "error"]
        if select:
            cmd += ["-select_streams", select]
        if read_intervals:
            cmd += ["-read_intervals", read_intervals]
        cmd += ["-show_entries", f"{section}={entries}",
                "-of", "compact=p=0:nk=0", str(url)]
        try:
            cp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1)
        except FileNotFoundError:
            raise FFmpegError(f"ffprobe binary not found: {self.ffprobe}", cmd)
        started = time.monotonic()
        try:
            for line in cp.stdout:
                if timeout and time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                line = line.strip()
                if not line:
                    continue
                yield dict(part.partition("=")[::2] for part in line.split("|")
                           if "=" in part)
            remaining = None if not timeout else max(
                0.01, timeout - (time.monotonic() - started))
            rc = cp.wait(timeout=remaining)
            stderr = cp.stderr.read()
        except subprocess.TimeoutExpired:
            cp.kill()
            cp.wait()
            raise FFmpegError(
                f"ffprobe timed out after {timeout}s probing {url}", cmd)
        finally:
            if cp.poll() is None:
                cp.kill()
                cp.wait()
            if cp.stdout:
                cp.stdout.close()
            if cp.stderr:
                cp.stderr.close()
        if rc != 0:
            raise FFmpegError(f"ffprobe failed on {url}: {_stderr_tail(stderr, 500)}",
                              cmd, _stderr_tail(stderr))

    def _capability_list(self, kind) -> str:
        if kind not in self._caps:
            cp = subprocess.run([self.ffmpeg, "-hide_banner", f"-{kind}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=30)
            self._caps[kind] = cp.stdout or ""
        return self._caps[kind]

    def has_filter(self, name) -> bool:
        try:
            return any(line.split()[1] == name
                       for line in self._capability_list("filters").splitlines()
                       if len(line.split()) > 1)
        except (OSError, subprocess.SubprocessError):
            return False

    def has_encoder(self, name) -> bool:
        try:
            return any(line.split()[1] == name
                       for line in self._capability_list("encoders").splitlines()
                       if len(line.split()) > 1)
        except (OSError, subprocess.SubprocessError):
            return False

    def encoder_help(self, name) -> str:
        """Return cached private-option help for one encoder."""
        key = ("encoder_help", str(name))
        if key not in self._caps:
            try:
                cp = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-h", f"encoder={name}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=30)
                self._caps[key] = cp.stdout or "" if cp.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                self._caps[key] = ""
        return self._caps[key]

    def has_muxer(self, name) -> bool:
        try:
            return any(line.split()[1] == name
                       for line in self._capability_list("muxers").splitlines()
                       if len(line.split()) > 1)
        except (OSError, subprocess.SubprocessError):
            return False

    def version(self) -> str:
        try:
            cp = subprocess.run([self.ffmpeg, "-version"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=30)
            return (cp.stdout or "").splitlines()[0] if cp.stdout else "unknown"
        except (OSError, subprocess.SubprocessError, IndexError):
            return "unknown"

    def identity(self) -> dict:
        """Build identities used to namespace persisted trial scores."""
        return {"ffmpeg": _tool_identity(self.ffmpeg),
                "ffprobe": _tool_identity(self.ffprobe)}
