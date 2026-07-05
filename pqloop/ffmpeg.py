"""Thin wrapper around ffmpeg/ffprobe binaries with capability probing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


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

    def version(self) -> str:
        try:
            cp = subprocess.run([self.ffmpeg, "-version"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=30)
            return (cp.stdout or "").splitlines()[0] if cp.stdout else "unknown"
        except (OSError, subprocess.SubprocessError, IndexError):
            return "unknown"
