"""Input handling: probing, live capture, and mezzanine (reference) creation.

The optimization loop never touches the original input per trial. Instead a
short clip is extracted once into a lossless *mezzanine* (x264 qp=0, yuv420p,
CFR, deinterlaced if requested/detected). Every trial encodes the mezzanine and
VMAF compares against it, so all trials see byte-identical reference frames and
the deinterlace cost is paid once, not per iteration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from .util import parse_fps, fingerprint_file, atomic_write_json, load_json, now_iso

LIVE_SCHEMES = ("udp", "rtp", "srt", "rist")
INTERLACED_ORDERS = ("tt", "bb", "tb", "bt")


def is_live_url(url) -> bool:
    s = str(url)
    return any(s.startswith(scheme + "://") or s.startswith(scheme + ":@")
               for scheme in LIVE_SCHEMES)


@dataclass
class SourceInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_str: str
    field_order: str
    duration: float
    has_audio: bool
    video_codec: str
    pix_fmt: str

    @property
    def interlaced(self) -> bool:
        return self.field_order in INTERLACED_ORDERS


def probe_file(ff, path, program=None) -> SourceInfo:
    return parse_probe(ff.probe(path), path, program)


def parse_probe(data, path, program=None) -> SourceInfo:
    streams = data.get("streams", [])
    if program is not None:
        for prog in data.get("programs", []):
            if prog.get("program_id") == program:
                streams = prog.get("streams") or streams
                break
        else:
            available = [p.get("program_id") for p in data.get("programs", [])]
            raise RuntimeError(f"program {program} not found in {path} "
                               f"(available: {available or 'none'})")
    video = None
    for stream in streams:
        if stream.get("codec_type") == "video" and stream.get("width"):
            video = stream
            break
    if video is None:
        raise RuntimeError(f"no video stream found in {path}")
    fps_str = video.get("avg_frame_rate") or video.get("r_frame_rate") or "25/1"
    if parse_fps(fps_str) <= 0:
        fps_str = video.get("r_frame_rate") or "25/1"
    duration = float(video.get("duration")
                     or data.get("format", {}).get("duration") or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return SourceInfo(
        path=str(path),
        width=int(video["width"]), height=int(video["height"]),
        fps=parse_fps(fps_str), fps_str=fps_str,
        field_order=video.get("field_order", "progressive") or "progressive",
        duration=duration, has_audio=has_audio,
        video_codec=video.get("codec_name", "?"),
        pix_fmt=video.get("pix_fmt", "?"),
    )


def capture_live(ff, url, seconds, out_path, extra_input_args=(), program=None,
                 log=None) -> str:
    """Record a live (multicast/unicast UDP, RTP, SRT, RIST) stream to a local
    MPEG-TS file via stream copy. All trials then work from this recording.
    `program` selects one program out of a multi-program transport stream."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"capturing {seconds:.0f}s from {url} ...")
    ff.run(["-y", "-fflags", "+genpts", *extra_input_args, "-i", url,
            "-t", f"{seconds:.3f}",
            "-map", f"0:p:{program}" if program is not None else "0",
            "-c", "copy", "-f", "mpegts", str(out_path)],
           timeout=seconds * 4 + 120)
    return str(out_path)


def get_or_capture_live(ff, url, seconds, out_path, extra_input_args=(),
                        program=None, reuse=False, log=None) -> str:
    """Capture the live url, or — when `reuse` is set — keep an existing capture
    whose sidecar meta matches url/program/input-args and covers `seconds`.
    Reuse is what lets a live optimization resume on identical reference frames."""
    out_path = Path(out_path)
    meta_path = out_path.with_suffix(out_path.suffix + ".json")
    wanted = {"url": str(url), "program": program,
              "extra_input_args": list(extra_input_args)}
    if reuse and out_path.exists():
        meta = None
        if meta_path.exists():
            try:
                meta = load_json(meta_path)
            except (json.JSONDecodeError, OSError):
                meta = None
        if meta is None:
            if log:
                log(f"reusing capture {out_path} (no capture meta; assuming it matches)")
            return str(out_path)
        stale = [k for k, v in wanted.items() if meta.get(k) != v]
        if float(meta.get("seconds") or 0.0) < float(seconds):
            stale.append("seconds")
        if not stale:
            if log:
                log(f"reusing capture {out_path} ({meta.get('seconds'):g}s of {url})")
            return str(out_path)
        if log:
            log(f"recapturing ({', '.join(stale)} changed since last capture)")
    captured = capture_live(ff, url, seconds, out_path, extra_input_args,
                            program, log)
    atomic_write_json(meta_path, {**wanted, "seconds": float(seconds),
                                  "captured_at": now_iso()})
    return captured


def deinterlace_decision(source: SourceInfo, mode) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return source.interlaced


def build_filters(deinterlace: bool, deint_mode: str) -> list:
    filters = []
    if deinterlace:
        bwdif_mode = "send_field" if deint_mode == "field" else "send_frame"
        filters.append(f"bwdif=mode={bwdif_mode}")
    return filters


def output_fps(source_fps: float, source_fps_str: str,
               deinterlace: bool, deint_mode: str):
    """(fps float, fps rational string) after optional field-rate deinterlace."""
    if deinterlace and deint_mode == "field":
        if "/" in source_fps_str:
            num, den = source_fps_str.split("/", 1)
            return source_fps * 2, f"{int(num) * 2}/{den}"
        return source_fps * 2, str(source_fps * 2)
    return source_fps, source_fps_str


@dataclass
class MezzInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_str: str
    duration: float
    fingerprint: str
    deinterlaced: bool
    filters: str
    inputs_key: str


def _mezz_inputs_key(source: SourceInfo, start, duration, deinterlaced,
                     deint_mode) -> str:
    # Content-based (not mtime-based): a recaptured-but-identical live clip or a
    # file copied between servers keeps its key, so the mezzanine is reused.
    src = Path(source.path)
    ident = {
        "src": str(src), "src_fp": fingerprint_file(src),
        "start": round(float(start), 3), "duration": round(float(duration), 3),
        "deint": bool(deinterlaced), "deint_mode": deint_mode if deinterlaced else "",
    }
    return json.dumps(ident, sort_keys=True)


def get_or_build_mezzanine(ff, source: SourceInfo, start, duration,
                           deint, deint_mode, out_path, log=None) -> MezzInfo:
    out_path = Path(out_path)
    meta_path = out_path.with_suffix(out_path.suffix + ".json")
    deinterlaced = deinterlace_decision(source, deint)
    inputs_key = _mezz_inputs_key(source, start, duration, deinterlaced, deint_mode)

    if out_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if (meta.get("inputs_key") == inputs_key
                    and meta.get("fingerprint") == fingerprint_file(out_path)):
                if log:
                    log(f"reusing mezzanine {out_path}")
                return MezzInfo(**meta)
        except (json.JSONDecodeError, TypeError, OSError):
            pass

    filters = build_filters(deinterlaced, deint_mode)
    filters.append("format=yuv420p")
    fps, fps_str = output_fps(source.fps, source.fps_str, deinterlaced, deint_mode)
    if log:
        what = f"bwdif {deint_mode} -> {fps:g}fps, " if deinterlaced else ""
        log(f"building lossless mezzanine ({what}{duration:g}s @ {start:g}s) ...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # bitexact muxing: no random SegmentUID/date, so rebuilding from unchanged
    # inputs reproduces the same bytes and the fingerprint (and with it the
    # optimizer's trial cache) survives a rebuild.
    ff.run(["-y", "-ss", f"{float(start):.3f}", "-t", f"{float(duration):.3f}",
            "-i", source.path,
            "-vf", ",".join(filters),
            "-an", "-sn", "-dn",
            "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
            "-r", fps_str, "-fflags", "+bitexact", "-f", "matroska",
            str(out_path)],
           timeout=max(600.0, duration * 30 + 300))

    probed = probe_file(ff, out_path)
    info = MezzInfo(
        path=str(out_path), width=probed.width, height=probed.height,
        fps=probed.fps, fps_str=probed.fps_str,
        duration=probed.duration or float(duration),
        fingerprint=fingerprint_file(out_path),
        deinterlaced=deinterlaced,
        filters=",".join(filters), inputs_key=inputs_key,
    )
    atomic_write_json(meta_path, asdict(info))
    return info
