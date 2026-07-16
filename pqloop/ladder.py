"""Ladder orchestration (`pqloop ladder`): one command that optimizes every
rung of an ABR ladder and packages the result.

A ladder spec is a JSON file in the presets directory carrying the rung
definitions; each rung maps to an ordinary preset (auto-named, tagged with
its ladder) so all existing optimize state/resume machinery applies
unchanged. The orchestration itself goes through the public CLI: for each
rung an `optimize` argv is built and dispatched, then one `package` argv.
Rungs run in descending-bitrate order; a fresh rung preset is seeded with
the rung above's best parameters and measured sensitivities — the optimizer
skips screening for any parameter whose sensitivity is already known, so a
seeded rung goes straight to refinement from a good starting point.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from . import __version__
from .util import atomic_write_json, load_json, now_iso, parse_bitrate_kbps


def parse_rung(text) -> dict:
    """'WxH:BITRATE' (e.g. 1280x720:2800k) or 'source:BITRATE' for a rung at
    source resolution."""
    s = str(text).strip().lower()
    scale_part, sep, rate = s.rpartition(":")
    if not sep or not scale_part or not rate:
        raise ValueError(f"--rung expects WxH:BITRATE or source:BITRATE, got {text!r}")
    kbps = parse_bitrate_kbps(rate)
    if kbps <= 0:
        raise ValueError(f"--rung bitrate must be > 0, got {text!r}")
    if scale_part == "source":
        scale = ""
    else:
        w, x, h = scale_part.partition("x")
        if x != "x" or not (w.isdigit() and h.isdigit() and int(w) and int(h)):
            raise ValueError(f"--rung expects WxH:BITRATE or source:BITRATE, got {text!r}")
        if int(w) % 2 or int(h) % 2:
            raise ValueError(f"--rung dimensions must be even for yuv420 "
                             f"subsampling, got {scale_part!r}")
        scale = f"{int(w)}x{int(h)}"
    return {"scale": scale, "bitrate_kbps": kbps}


def parse_rungs(specs) -> list:
    rungs = [parse_rung(s) for s in specs]
    bitrates = [r["bitrate_kbps"] for r in rungs]
    if len(set(bitrates)) != len(bitrates):
        raise ValueError(f"duplicate rung bitrates: "
                         f"{', '.join(str(b) + 'k' for b in bitrates)}")
    return rungs


def _rung_name(ladder_name, rung, taken) -> str:
    base = (f"{ladder_name}_source" if not rung["scale"]
            else f"{ladder_name}_{rung['scale'].split('x')[1]}p")
    if base not in taken:
        return base
    return f"{base}_{rung['bitrate_kbps']}k"


def merge_rungs(stored, new, ladder_name):
    """Combine new rung definitions with the stored set: a rung matching a
    stored (scale, bitrate) keeps its persisted preset name (names are
    assigned once, never recomputed — adding a rung later must not rename
    existing ones and orphan their search state). Returns (rungs, orphaned
    preset names of stored rungs no longer in the set)."""
    by_key = {(r["scale"], r["bitrate_kbps"]): r for r in stored}
    taken = {r["preset"] for r in stored if r.get("preset")}
    merged = []
    for rung in new:
        kept = by_key.pop((rung["scale"], rung["bitrate_kbps"]), None)
        if kept and kept.get("preset"):
            merged.append(dict(kept))
        else:
            rung = dict(rung)
            rung["preset"] = _rung_name(ladder_name, rung, taken)
            taken.add(rung["preset"])
            merged.append(rung)
    orphans = [r["preset"] for r in by_key.values() if r.get("preset")]
    return merged, orphans


# --------------------------------------------------------------------------- #
# spec persistence
# --------------------------------------------------------------------------- #

def is_ladder(data) -> bool:
    return bool(data.get("rungs"))


def load_spec(path, name) -> dict:
    path = Path(path)
    if not path.exists():
        return {"name": name, "created": now_iso(), "rungs": [],
                "last_input": ""}
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot read ladder spec {path}: {exc}")
    data.setdefault("name", name)
    data.setdefault("rungs", [])
    return data


def save_spec(path, data) -> None:
    data["updated"] = now_iso()
    data["pqloop_version"] = __version__
    atomic_write_json(path, data)


def seed_data(data, ladder_name, current=None, sens=None) -> dict:
    """Tag a rung preset with its ladder and install a warm-start prior
    (parent point + sensitivities). The sens seeding is what makes the
    optimizer skip screening: params with known sensitivity are never
    re-probed, so refinement starts immediately in parent-impact order.
    Mutates in place (an existing-but-never-optimized preset keeps whatever
    config it carries); only call when the optimizer state is empty."""
    data["ladder"] = ladder_name
    if current:
        data["optimizer"] = {"current": dict(current), "sens": dict(sens or {})}
    return data


def rung_outcome(data):
    """(best objective or None, last stop_reason) — the success signal.
    cmd_optimize's exit code is 0 for ANY StopSearch including
    baseline_failed, so the ladder judges a rung by its preset instead."""
    best = data.get("best") or {}
    runs = data.get("runs") or []
    reason = runs[-1].get("stop_reason", "") if runs else ""
    return best.get("objective"), reason


# --------------------------------------------------------------------------- #
# argv builders (dispatched through the real CLI parser)
# --------------------------------------------------------------------------- #

def _opt(flag, value):
    return [flag, str(value)] if value is not None else []


def _tristate(flag, value):
    if value is None:
        return []
    return [flag if value else flag.replace("--", "--no-", 1)]


def optimize_argv(rung, a, input_url, work_root, live=False) -> list:
    """The `pqloop optimize` invocation for one rung. Budgets (--max-trials/
    --max-seconds/--target-score) apply per rung. For live inputs capture
    reuse is forced: without it every rung would re-record over the shared
    capture, changing the mezzanine fingerprint and wiping every rung's
    cached scores on the next run."""
    work_root = Path(work_root)
    argv = ["optimize",
            "-p", rung["preset"],
            "--presets-dir", str(a.presets_dir),
            "-i", str(input_url),
            "-b", f"{rung['bitrate_kbps']}k",
            "--work-dir", str(work_root / rung["preset"]),
            "--mezz-dir", str(work_root)]
    if rung["scale"]:
        argv += ["--scale", rung["scale"]]
    if live:
        argv += ["--reuse-capture"]
    argv += _opt("--encoder", a.encoder)
    argv += _opt("--clip-start", a.clip_start)
    argv += _opt("--clip-duration", a.clip_duration)
    argv += _opt("--capture-duration", a.capture_duration)
    argv += _opt("--program", a.program)
    argv += _opt("--seg-duration", a.seg_duration)
    argv += _opt("--gop-duration", a.gop_duration)
    argv += _opt("--metric", a.metric)
    argv += _opt("--pix-fmt", a.pix_fmt)
    argv += _opt("--src-primaries", a.src_primaries)
    argv += _opt("--src-trc", a.src_trc)
    argv += _opt("--tonemap", a.tonemap)
    argv += _opt("--norm-scale", a.norm_scale)
    argv += _opt("--audio-stream", a.audio_stream)
    argv += _opt("--deinterlace", a.deinterlace)
    argv += _opt("--deint-mode", a.deint_mode)
    argv += _opt("--min-gain", a.min_gain)
    argv += _opt("--adopt-eps", a.adopt_eps)
    argv += _opt("--max-trials", a.max_trials)
    argv += _opt("--max-seconds", a.max_seconds)
    argv += _opt("--target-score", a.target_score)
    argv += _opt("--max-passes", a.max_passes)
    argv += _opt("--vmaf-model", a.vmaf_model)
    argv += _opt("--vmaf-subsample", a.vmaf_subsample)
    argv += _opt("--vmaf-threads", a.vmaf_threads)
    argv += _opt("--ffmpeg", a.ffmpeg)
    argv += _opt("--ffprobe", a.ffprobe)
    argv += _opt("--vmaf-ffmpeg", a.vmaf_ffmpeg)
    argv += _opt("--cache-salt", getattr(a, "cache_salt", None))
    argv += _opt("--extra-video-args", a.extra_video_args)
    argv += _opt("--extra-input-args", a.extra_input_args)
    argv += _tristate("--two-pass", a.two_pass)
    argv += _tristate("--keep-trials", a.keep_trials)
    if a.no_screen:
        argv += ["--no-screen"]
    if getattr(a, "reset_cache", False):
        argv += ["--reset-cache"]
    for item in a.freeze or []:
        argv += ["--freeze", item]
    if a.optimize_args:
        argv += shlex.split(a.optimize_args)
    return argv


def package_argv(rung_presets, a, input_url) -> list:
    argv = ["package"]
    for name in rung_presets:
        argv += ["-p", name]
    argv += ["--presets-dir", str(a.presets_dir),
             "-i", str(input_url),
             "-o", str(a.output_dir),
             "--format", a.format,
             "--hls-segment-type", a.hls_segment_type,
             "--audio-bitrate", str(a.audio_bitrate)]
    argv += _opt("--start", a.start)
    argv += _opt("--duration", a.duration)
    argv += _opt("--capture-duration", a.capture_duration)
    argv += _opt("--program", a.program)
    argv += _opt("--h264-level", a.h264_level)
    argv += _opt("--seg-duration", a.seg_duration)
    argv += _opt("--src-primaries", a.src_primaries)
    argv += _opt("--src-trc", a.src_trc)
    argv += _opt("--tonemap", a.tonemap)
    argv += _opt("--norm-scale", a.norm_scale)
    argv += _opt("--audio-stream", a.audio_stream)
    argv += _opt("--ffmpeg", a.ffmpeg)
    argv += _opt("--ffprobe", a.ffprobe)
    argv += _opt("--extra-input-args", a.extra_input_args)
    if a.no_audio:
        argv += ["--no-audio"]
    if a.clean:
        argv += ["--clean"]
    if a.no_verify:
        argv += ["--no-verify"]
    return argv


def summary_lines(rung_datas) -> list:
    """One line per rung from its finished preset: what the ladder achieved."""
    lines = []
    for data in rung_datas:
        cfg = data.get("config") or {}
        best = data.get("best") or {}
        metrics = best.get("metrics") or {}
        obj = best.get("objective")
        lines.append(
            f"  {data.get('name', '?'):<24} "
            f"{(cfg.get('scale') or 'source'):<10} "
            f"{cfg.get('target_bitrate_kbps', 0):>6}k  "
            f"vmaf {metrics.get('vmaf_mean', 0):6.2f}  "
            f"{metrics.get('bitrate_kbps', 0):6.0f}k actual  "
            f"obj {obj if obj is not None else float('nan'):8.3f}  "
            f"({(data.get('optimizer') or {}).get('encodes', 0)} encodes)")
    return lines
