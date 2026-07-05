"""Netflix VMAF measurement via ffmpeg's libvmaf filter.

Uses the libvmaf default model (version=vmaf_v0.6.1 — the canonical default of
the Netflix VMAF v1 release line); override with --vmaf-model. The measurement
ffmpeg may be a different binary from the encode ffmpeg, since hardware-vendor
builds (netint, some nvenc builds) often lack libvmaf.
"""

from __future__ import annotations

import json
import math
import os


def _fesc(value) -> str:
    """Escape a value for use inside a filtergraph option."""
    out = str(value)
    for ch in ("\\", ":", ",", "'", "[", "]", ";"):
        out = out.replace(ch, "\\" + ch)
    return out


def _percentile(sorted_scores, q) -> float:
    if not sorted_scores:
        return 0.0
    if len(sorted_scores) == 1:
        return sorted_scores[0]
    pos = (len(sorted_scores) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_scores) - 1)
    frac = pos - lo
    return sorted_scores[lo] * (1 - frac) + sorted_scores[hi] * frac


def measure(ff_meas, distorted, reference, ref_width, ref_height, log_path,
            threads=0, subsample=1, model=None, timeout=None) -> dict:
    """Compare distorted vs reference and return VMAF aggregates.

    The distorted stream is scaled (bicubic) to the reference resolution first,
    matching standard VMAF practice when the encode runs at a reduced size.
    """
    opts = ["log_fmt=json",
            f"log_path={_fesc(log_path)}",
            f"n_threads={int(threads) if threads else (os.cpu_count() or 4)}"]
    if subsample and int(subsample) > 1:
        opts.append(f"n_subsample={int(subsample)}")
    if model:
        opts.append(f"model={_fesc(model)}")
    graph = (f"[0:v]scale={ref_width}:{ref_height}:flags=bicubic,"
             f"setpts=PTS-STARTPTS[dis];"
             f"[1:v]setpts=PTS-STARTPTS[ref];"
             f"[dis][ref]libvmaf=" + ":".join(opts))
    ff_meas.run(["-i", distorted, "-i", reference,
                 "-lavfi", graph, "-f", "null", "-"], timeout=timeout)

    with open(log_path) as fh:
        data = json.load(fh)
    frames = [f["metrics"]["vmaf"] for f in data.get("frames", [])
              if "vmaf" in f.get("metrics", {})]
    pooled = (data.get("pooled_metrics") or {}).get("vmaf") or {}
    if not frames and not pooled:
        raise RuntimeError(f"libvmaf produced no scores (log: {log_path})")

    if frames:
        mean = sum(frames) / len(frames)
        # Netflix-style harmonic mean, shifted by 1 to tolerate zero scores.
        harmonic = len(frames) / sum(1.0 / (s + 1.0) for s in frames) - 1.0
        lowest = min(frames)
        ordered = sorted(frames)
        p1 = _percentile(ordered, 0.01)
        p5 = _percentile(ordered, 0.05)
    else:
        mean = harmonic = lowest = p1 = p5 = None
    if pooled.get("mean") is not None:
        mean = pooled["mean"]
    if pooled.get("harmonic_mean") is not None:
        harmonic = pooled["harmonic_mean"]
    if pooled.get("min") is not None:
        lowest = pooled["min"]
        if p1 is None:
            p1 = p5 = lowest

    return {
        "vmaf_mean": round(float(mean), 4),
        "vmaf_harmonic": round(float(harmonic), 4),
        "vmaf_min": round(float(lowest), 4),
        "vmaf_p1": round(float(p1), 4),
        "vmaf_p5": round(float(p5), 4),
        "vmaf_frames": len(frames),
    }


METRIC_KEYS = {
    "mean": "vmaf_mean",
    "harmonic": "vmaf_harmonic",
    "p1": "vmaf_p1",
    "p5": "vmaf_p5",
    "min": "vmaf_min",
}
