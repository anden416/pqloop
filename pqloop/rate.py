"""Bitrate sweep: sample a preset's quality-vs-rate curve and pick the rate
worth paying for.

Like optimizer.py, this module knows nothing about ffmpeg, presets, or files.
The orchestrator (cli's `pqloop bitrate`) drives a dumb loop:

    while (kbps := next_rate(anchor, settings, points, encodes)) is not None:
        points.append(<encode + measure the mezzanine at kbps>)
    analysis = analyze(anchor, settings, points)

`next_rate` is deterministic given its arguments, so a resumed sweep simply
replays with already-sampled points present in `points` — no position
bookkeeping (the optimizer's cache-replay principle).

A point is the plain dict persisted in the preset's `rate_search` block:

    {"requested_kbps": 4500, "ok": true, "source": "encode"|"preset_best",
     "metrics": {"vmaf_mean": ..., "bitrate_kbps": <measured>, ...},
     "error": "", "measured_at": "<iso>"}

Math runs on MEASURED kbps (quality per bit actually spent); planning and the
write-back use REQUESTED kbps — the only input we control, and the chosen
value's score was actually measured, never interpolated. The knee runs on the
upper concave envelope of the measured points: the true rate-quality curve is
concave, so envelope slopes are strictly decreasing (unique threshold
crossing) and every excluded point is explainable as rate-control jitter.
"""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass, field

from .ladder import _opt, _tristate
from .util import now_iso

RATE_SEARCH_SCHEMA = 1

# The non-rate config keys that change what a sampled point measures. Tracks
# cli.OBJECTIVE_KEYS minus the rate/penalty/metric keys: the target bitrate is
# the swept variable itself, penalties/tolerance only shape the tuning
# objective (raw VMAF is unaffected), and `metric` is decision-time only —
# every aggregate is stored with each point, so re-deciding under a different
# metric reuses the whole curve. maxrate/bufsize ratios stay in (VBV changes
# what each rate point measures). A test pins this relationship.
SWEEP_KEYS = ("encoder", "maxrate_ratio", "bufsize_ratio", "seg_duration",
              "gop_duration", "pix_fmt", "scale", "vmaf_model",
              "vmaf_subsample", "two_pass", "extra_video_args",
              "src_primaries", "src_trc", "tonemap", "norm_scale")

CRITERIA = ("knee", "target", "ceiling")


@dataclass
class RateSettings:
    metric_key: str = "vmaf_mean"     # vmaf.METRIC_KEYS[cfg["metric"]]
    criterion: str = "knee"           # knee | target | ceiling
    knee_gain: float = 1.5            # VMAF points per Mbps still worth paying
    target_vmaf: float = None         # quality floor for criterion "target"
    ceiling_delta: float = 1.0        # points below the observed ceiling
    min_rate_kbps: int = 0            # 0 = auto: 0.35 x anchor
    max_rate_kbps: int = 0            # 0 = auto: 1.60 x anchor
    coarse_points: int = 5            # grid size incl. endpoints
    encode_budget: int = 7            # new encodes per run incl. refinement
    explicit_rates: tuple = ()        # --rates: replaces the auto grid
    refine_stop_ratio: float = 1.10   # stop bisecting below ~10% brackets
    snap_kbps: int = 25
    rate_floor_kbps: int = 100
    min_sep_frac: float = 0.05


@dataclass
class Pick:
    kbps: int = None          # requested kbps of a sampled point; None = none
    satisfied: bool = False
    refine_lo_kbps: int = None
    refine_hi_kbps: int = None
    note: str = ""


@dataclass
class CurveAnalysis:
    points: list = field(default_factory=list)   # cleaned, ascending measured
    slopes: list = field(default_factory=list)   # adjacent, VMAF per Mbps
    hull_idx: list = field(default_factory=list)
    picks: dict = field(default_factory=dict)    # criterion -> Pick
    chosen: str = "knee"
    warnings: list = field(default_factory=list)


def validate_settings(s) -> None:
    if s.criterion not in CRITERIA:
        raise ValueError(f"unknown criterion {s.criterion!r} "
                         f"(one of: {', '.join(CRITERIA)})")
    if s.criterion == "target" and s.target_vmaf is None:
        raise ValueError("--criterion target needs --target-vmaf")
    if s.target_vmaf is not None and (
            not math.isfinite(float(s.target_vmaf))
            or not 0 < float(s.target_vmaf) <= 100):
        raise ValueError(f"--target-vmaf must be finite and in (0, 100] "
                         f"(got {s.target_vmaf!r})")
    if not math.isfinite(float(s.knee_gain)) or float(s.knee_gain) <= 0:
        raise ValueError(f"--knee-gain must be finite and > 0 "
                         f"(got {s.knee_gain!r})")
    if not math.isfinite(float(s.ceiling_delta)) \
            or float(s.ceiling_delta) < 0:
        raise ValueError(f"--within-ceiling must be finite and >= 0 "
                         f"(got {s.ceiling_delta!r})")
    if not s.explicit_rates and int(s.coarse_points) < 2:
        raise ValueError(f"--rate-points must be >= 2 "
                         f"(got {s.coarse_points!r})")
    if int(s.encode_budget) < 1:
        raise ValueError(f"--max-trials must be >= 1 "
                         f"(got {s.encode_budget!r})")


# --------------------------------------------------------------------------- #
# sampling plan
# --------------------------------------------------------------------------- #

def _snap(kbps, s) -> int:
    step = max(1, int(s.snap_kbps))
    return max(int(s.rate_floor_kbps), int(round(float(kbps) / step)) * step)


def _min_sep(lower_kbps, s) -> float:
    return max(2.0 * s.snap_kbps, s.min_sep_frac * float(lower_kbps))


def _dedupe(rates, s, protected=()) -> list:
    """Collapse rates closer than the minimum separation, preferring
    protected values (range endpoints + anchor) over interior grid points."""
    out = []
    for r in sorted(rates):
        if not out or (r - out[-1]) >= _min_sep(out[-1], s):
            out.append(r)
        elif r in protected and out[-1] not in protected:
            out[-1] = r
        elif r in protected and out[-1] in protected:
            out.append(r)   # never drop an endpoint or the anchor
    return out


def plan_rates(anchor_kbps, s) -> list:
    """The coarse sampling grid, ascending. Log-spaced: VMAF is roughly
    linear in log rate until saturation, so geometric steps buy about equal
    quality increments per encode; linear spacing would crowd the flat top
    and starve the steep low end where the knee lives. The preset's current
    bitrate (anchor) is merged in — its score is often free from the stored
    best result."""
    validate_settings(s)
    if s.explicit_rates:
        rates = sorted({_snap(r, s) for r in s.explicit_rates})
        if len(rates) < 2:
            raise ValueError("--rates needs at least two distinct rates")
        return rates
    anchor = int(anchor_kbps or 0)
    lo = int(s.min_rate_kbps) or (_snap(0.35 * anchor, s) if anchor else 0)
    hi = int(s.max_rate_kbps) or (_snap(1.60 * anchor, s) if anchor else 0)
    if not lo or not hi:
        raise ValueError("no anchor bitrate to derive the sweep range — pass "
                         "--min-rate and --max-rate, or explicit --rates")
    lo, hi = _snap(lo, s), _snap(hi, s)
    if hi <= lo:
        raise ValueError(f"--max-rate must exceed --min-rate "
                         f"(got {lo}k..{hi}k)")
    k = max(2, int(s.coarse_points))
    grid = {lo * (hi / lo) ** (i / (k - 1)) for i in range(k)}
    protected = {lo, hi}
    if anchor and lo <= anchor <= hi:
        grid.add(float(anchor))
        protected.add(_snap(anchor, s))
    return _dedupe({_snap(g, s) for g in grid}, s, protected=tuple(protected))


def sample_order(rates) -> list:
    """Deterministic coverage order: bottom, top, then repeatedly the rate
    that best splits the largest remaining log-gap — a budget-truncated sweep
    still brackets the whole range instead of clustering at one end."""
    rates = sorted(int(r) for r in rates)
    if len(rates) <= 2:
        return rates
    order = [rates[0], rates[-1]]
    chosen = sorted(order)
    remaining = rates[1:-1]
    while remaining:
        best = None
        for r in remaining:
            below = max(c for c in chosen if c < r)
            above = min(c for c in chosen if c > r)
            key = (math.log(above / below),
                   min(math.log(r / below), math.log(above / r)), -r)
            if best is None or key > best[0]:
                best = (key, r)
        order.append(best[1])
        remaining.remove(best[1])
        chosen = sorted(chosen + [best[1]])
    return order


def points_for_plan(points, rates) -> list:
    """Points eligible for the current decision, by requested-rate bounds.

    Stored measurements outside a later, narrower sweep remain reusable for a
    future wider run, but they must not move a range-dependent pick or create a
    refinement beyond the range the user requested. Interior points remain
    useful when only the coarse grid density changes.
    """
    rates = list(rates)
    if not rates:
        return []
    lo, hi = min(int(r) for r in rates), max(int(r) for r in rates)
    return [p for p in points
            if lo <= int(p["requested_kbps"]) <= hi]


# --------------------------------------------------------------------------- #
# curve hygiene
# --------------------------------------------------------------------------- #

def _measured(p) -> float:
    return float((p.get("metrics") or {}).get("bitrate_kbps") or 0.0)


def _score(p, metric_key):
    value = (p.get("metrics") or {}).get(metric_key)
    return None if value is None else float(value)


def clean_curve(points, s):
    """The usable curve: successful points, ascending measured kbps, with
    near-identical spends collapsed to the better score (several low requests
    measuring alike is the encoder-rate-floor tell). Returns
    (cleaned, warnings)."""
    warnings, usable = [], []
    for p in sorted(points, key=lambda p: int(p["requested_kbps"])):
        if not p.get("ok"):
            err = (p.get("error") or "").strip()
            warnings.append(f"encode failed at {p['requested_kbps']}k"
                            + (f": {err[:80]}" if err else ""))
            continue
        if _measured(p) <= 0 or _score(p, s.metric_key) is None:
            warnings.append(
                f"{p['requested_kbps']}k has no usable measurements; ignored")
            continue
        usable.append(p)
    usable.sort(key=lambda p: (_measured(p), int(p["requested_kbps"])))
    cleaned, collapsed = [], []
    for p in usable:
        if cleaned and (_measured(p) - _measured(cleaned[-1])
                        < _min_sep(_measured(cleaned[-1]), s)):
            if _score(p, s.metric_key) > _score(cleaned[-1], s.metric_key):
                collapsed.append(int(cleaned[-1]["requested_kbps"]))
                cleaned[-1] = p
            else:
                collapsed.append(int(p["requested_kbps"]))
            continue
        cleaned.append(p)
    if collapsed:
        warnings.append(
            "near-identical measured rates collapsed (requested "
            + ", ".join(f"{r}k" for r in sorted(collapsed))
            + ("; the encoder may not reach the lowest requested rates"
               if len(collapsed) >= 2 else "") + ")")
    return cleaned, warnings


def adjacent_slopes(points, metric_key) -> list:
    """Marginal gain between neighbours, in VMAF points per Mbps of measured
    spend (the report's gain column; knee decisions use the hull's slopes)."""
    slopes = []
    for prev, cur in zip(points, points[1:]):
        dx = (_measured(cur) - _measured(prev)) / 1000.0
        dy = _score(cur, metric_key) - _score(prev, metric_key)
        slopes.append(dy / dx if dx > 0 else 0.0)
    return slopes


def concave_hull_idx(cleaned, metric_key) -> list:
    """Indices of the upper concave envelope over (measured kbps, score) —
    monotone chain, both endpoints always kept, collinear middles dropped so
    consecutive envelope slopes are strictly decreasing."""
    idx = []
    for i, p in enumerate(cleaned):
        x, y = _measured(p), _score(p, metric_key)
        while len(idx) >= 2:
            x0 = _measured(cleaned[idx[-2]])
            y0 = _score(cleaned[idx[-2]], metric_key)
            x1 = _measured(cleaned[idx[-1]])
            y1 = _score(cleaned[idx[-1]], metric_key)
            if (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0) >= 0:
                idx.pop()      # last vertex on/under the chord: jitter
            else:
                break
        idx.append(i)
    return idx


# --------------------------------------------------------------------------- #
# criteria
# --------------------------------------------------------------------------- #

def _insufficient() -> Pick:
    return Pick(note="insufficient points (need >= 2 successful rates)")


def _bracket_below(cleaned, pick_pt) -> tuple:
    """(refine_lo, refine_hi) requested kbps around a cheapest-satisfying
    pick: the crossing lies between the point just below it and the pick."""
    below = [p for p in cleaned if _measured(p) < _measured(pick_pt)]
    if not below:
        return None, None
    return (int(below[-1]["requested_kbps"]), int(pick_pt["requested_kbps"]))


def pick_knee(cleaned, hull_idx, metric_key, threshold) -> Pick:
    """Highest sampled rate whose increment still pays >= threshold VMAF per
    Mbps, judged on the concave envelope (strictly decreasing slopes -> the
    crossing is unique)."""
    if len(hull_idx) < 2:
        return _insufficient()
    hull = [cleaned[i] for i in hull_idx]
    slopes = adjacent_slopes(hull, metric_key)
    if slopes[-1] >= threshold:
        return Pick(kbps=int(hull[-1]["requested_kbps"]), satisfied=False,
                    note=f"still gaining >= {threshold:g} vmaf/Mbps at the "
                         f"top of the range; consider raising --max-rate")
    if slopes[0] < threshold:
        return Pick(kbps=int(hull[0]["requested_kbps"]), satisfied=False,
                    note=f"already below {threshold:g} vmaf/Mbps at the "
                         f"bottom of the range; consider lowering --min-rate")
    i = max(j for j, slope in enumerate(slopes) if slope >= threshold)
    return Pick(kbps=int(hull[i + 1]["requested_kbps"]), satisfied=True,
                refine_lo_kbps=int(hull[i + 1]["requested_kbps"]),
                refine_hi_kbps=int(hull[i + 2]["requested_kbps"]),
                note=f"marginal gain falls below {threshold:g} vmaf/Mbps "
                     f"beyond this rate")


def pick_target(cleaned, hull_idx, metric_key, target) -> Pick:
    """Cheapest sampled rate whose score meets the quality floor. Never
    silently picks the top of the range when the target is out of reach."""
    if target is None:
        return Pick(note="no --target-vmaf given")
    if not cleaned:
        return _insufficient()
    target = float(target)
    satisfying = [p for p in cleaned if _score(p, metric_key) >= target]
    if not satisfying:
        top = cleaned[-1]
        note = (f"target {target:g} not reached (best "
                f"{_score(top, metric_key):.2f} at {top['requested_kbps']}k)")
        hull = [cleaned[i] for i in hull_idx]
        slopes = adjacent_slopes(hull, metric_key) if len(hull) >= 2 else []
        if slopes and slopes[-1] > 0:
            x_top = _measured(hull[-1])
            suggest = x_top + (target - _score(hull[-1], metric_key)) \
                / slopes[-1] * 1000.0
            suggest = min(suggest, 4.0 * x_top)   # extrapolation, keep sane
            note += (f"; the curve's tail suggests --max-rate "
                     f"~{int(round(suggest / 100.0) * 100)}k (extrapolated)")
        return Pick(kbps=None, satisfied=False, note=note)
    pick_pt = satisfying[0]
    lo, hi = _bracket_below(cleaned, pick_pt)
    if lo is None:
        return Pick(kbps=int(pick_pt["requested_kbps"]), satisfied=True,
                    note=f"target {target:g} met even at the bottom of the "
                         f"range; consider lowering --min-rate")
    return Pick(kbps=int(pick_pt["requested_kbps"]), satisfied=True,
                refine_lo_kbps=lo, refine_hi_kbps=hi,
                note=f"cheapest sampled rate with {metric_key} >= {target:g}")


def pick_ceiling(cleaned, metric_key, delta) -> Pick:
    """Cheapest sampled rate within delta points of the best score observed
    in the range (range-dependent by construction — documented)."""
    if not cleaned:
        return _insufficient()
    best = max(_score(p, metric_key) for p in cleaned)
    threshold = best - float(delta)
    pick_pt = next(p for p in cleaned if _score(p, metric_key) >= threshold)
    lo, hi = _bracket_below(cleaned, pick_pt)
    return Pick(kbps=int(pick_pt["requested_kbps"]), satisfied=True,
                refine_lo_kbps=lo, refine_hi_kbps=hi,
                note=f"cheapest sampled rate within {delta:g} of the "
                     f"observed ceiling {best:.2f}")


def analyze(s, points) -> CurveAnalysis:
    """Everything derived from the sampled points: the cleaned curve, its
    envelope, and all three criterion picks (the chosen criterion only
    decides what gets applied)."""
    cleaned, warnings = clean_curve(points, s)
    if len(cleaned) < 2:
        picks = {name: _insufficient() for name in CRITERIA}
        return CurveAnalysis(points=cleaned, picks=picks, chosen=s.criterion,
                             warnings=warnings)
    slopes = adjacent_slopes(cleaned, s.metric_key)
    hull_idx = concave_hull_idx(cleaned, s.metric_key)
    on_hull = set(hull_idx)
    excluded = [int(cleaned[i]["requested_kbps"])
                for i in range(len(cleaned)) if i not in on_hull]
    if excluded:
        warnings.append("under the concave envelope (rate-control noise), "
                        "excluded from knee slopes: "
                        + ", ".join(f"{r}k" for r in excluded))
        if s.metric_key in ("vmaf_min", "vmaf_p1") \
                and len(excluded) > len(cleaned) / 3:
            warnings.append(f"{s.metric_key} is noisy across rates here; "
                            f"knee confidence is low")
    picks = {
        "knee": pick_knee(cleaned, hull_idx, s.metric_key, s.knee_gain),
        "target": pick_target(cleaned, hull_idx, s.metric_key, s.target_vmaf),
        "ceiling": pick_ceiling(cleaned, s.metric_key, s.ceiling_delta),
    }
    hull_slopes = adjacent_slopes([cleaned[i] for i in hull_idx], s.metric_key)
    if hull_slopes and hull_slopes[-1] >= s.knee_gain:
        warnings.append("the curve is still rising at the top of the range; "
                        "the ceiling pick is range-limited")
    if s.target_vmaf is not None and picks["target"].kbps is not None:
        pick_pt = next((p for p in cleaned
                        if int(p["requested_kbps"]) == picks["target"].kbps),
                       None)
        if pick_pt is not None and any(
                _measured(p) > _measured(pick_pt)
                and _score(p, s.metric_key) < float(s.target_vmaf)
                for p in cleaned):
            warnings.append("scores dip below the target above the chosen "
                            "rate (non-monotone curve)")
    return CurveAnalysis(points=cleaned, slopes=slopes, hull_idx=hull_idx,
                         picks=picks, chosen=s.criterion, warnings=warnings)


# --------------------------------------------------------------------------- #
# the sweep walk
# --------------------------------------------------------------------------- #

def next_rate(anchor_kbps, s, points, encodes_spent):
    """The next requested rate to encode, or None when the sweep is done:
    coarse grid first (coverage order), then bisection of the chosen
    criterion's bracket with the leftover budget. Deterministic given its
    arguments; already-sampled rates (including failed ones) are never
    re-issued, which also guarantees refinement terminates — a failed or
    under-the-envelope refinement point leaves the bracket unchanged, so the
    same midpoint would recur and is refused as a collision."""
    if encodes_spent >= int(s.encode_budget):
        return None
    have = {int(p["requested_kbps"]) for p in points}
    for r in sample_order(plan_rates(anchor_kbps, s)):
        if r not in have:
            return r
    analysis = analyze(s, points)
    pick = analysis.picks.get(s.criterion)
    if pick is None or pick.refine_lo_kbps is None \
            or pick.refine_hi_kbps is None:
        return None
    lo, hi = int(pick.refine_lo_kbps), int(pick.refine_hi_kbps)
    if lo <= 0 or hi <= lo or hi / lo <= float(s.refine_stop_ratio):
        return None
    mid = _snap(math.sqrt(lo * hi), s)
    if mid in have or mid <= lo or mid >= hi:
        return None
    return mid


# --------------------------------------------------------------------------- #
# persistence shapes (still pure: dicts in, dicts out)
# --------------------------------------------------------------------------- #

def point_from_outcome(kbps, outcome, source="encode") -> dict:
    return {"requested_kbps": int(kbps), "ok": bool(outcome.ok),
            "source": source, "metrics": dict(outcome.metrics or {}),
            "error": outcome.error or "", "measured_at": now_iso()}


def sweep_context(cfg, params_sig, encoder_space, encode_tools, measure_tools,
                  fingerprint) -> dict:
    """Everything required for stored rate points to remain comparable —
    the rate-sweep analogue of cli.trial_context."""
    sweep_key = json.dumps({k: cfg.get(k) for k in SWEEP_KEYS
                            if cfg.get(k) is not None},
                           sort_keys=True, default=str)
    return {"schema": RATE_SEARCH_SCHEMA, "reference": fingerprint,
            "params_sig": params_sig, "encoder_space": encoder_space,
            "sweep_key": sweep_key,
            "encode_tools": encode_tools, "measure_tools": measure_tools,
            "cache_salt": cfg.get("cache_salt") or ""}


_CONTEXT_REASONS = (
    ("schema", "rate-search schema changed"),
    ("reference", "reference clip changed"),
    ("params_sig", "encoder parameters changed"),
    ("encoder_space", "encoder-space definition changed"),
    ("sweep_key", "encode/measurement settings changed"),
    ("encode_tools", "encode toolchain changed"),
    ("measure_tools", "measurement toolchain changed"),
    ("cache_salt", "cache salt changed"),
)


def context_matches(stored, current):
    """(matches, human-readable per-field mismatch reasons)."""
    if not stored:
        return False, ["no stored curve context"]
    reasons = [phrase for key, phrase in _CONTEXT_REASONS
               if stored.get(key) != current.get(key)]
    return not reasons, reasons


def usable_points(block, context):
    """Stored points that are still valid under the current context, keyed by
    requested kbps. Failed points are never reused (they retry); a context
    mismatch discards the whole stored curve (history lives in stats)."""
    block = block or {}
    stored = block.get("points") or []
    if not stored:
        return {}, []
    matches, reasons = context_matches(block.get("context"), context)
    if not matches:
        return {}, reasons
    return {int(p["requested_kbps"]): p for p in stored if p.get("ok")}, []


# --------------------------------------------------------------------------- #
# reporting + retune
# --------------------------------------------------------------------------- #

_SHORT = {"vmaf_mean": "mean", "vmaf_harmonic": "harm", "vmaf_min": "min",
          "vmaf_p1": "p1", "vmaf_p5": "p5"}


def table_lines(analysis, s, anchor_kbps=0) -> list:
    """The curve table: measured spend, scores, marginal gain, and markers
    for every criterion's pick, the anchor, and envelope-excluded noise."""
    cols = [s.metric_key] + [c for c in ("vmaf_mean", "vmaf_p1")
                             if c != s.metric_key]
    lines = [f"{'requested':>10} {'measured':>9} "
             + " ".join(f"{_SHORT.get(c, c):>7}" for c in cols)
             + f" {'gain/Mbps':>10}"]
    on_hull = set(analysis.hull_idx)
    for i, p in enumerate(analysis.points):
        gain = f"{analysis.slopes[i - 1]:+.2f}" if i else "-"
        tags = [name for name in CRITERIA
                if analysis.picks[name].kbps == int(p["requested_kbps"])]
        if anchor_kbps and int(p["requested_kbps"]) == int(anchor_kbps):
            tags.append("anchor")
        if i not in on_hull:
            tags.append("~noise")
        scores = " ".join(
            f"{_score(p, c):>7.2f}" if _score(p, c) is not None
            else f"{'-':>7}" for c in cols)
        lines.append(f"{p['requested_kbps']:>9}k {_measured(p):>8.0f}k "
                     f"{scores} {gain:>10}"
                     + (f"   <- {', '.join(tags)}" if tags else ""))
    return lines


def retune_argv(a, input_url, kbps) -> list:
    """The chained `pqloop optimize` invocation at the chosen rate
    (ladder.optimize_argv's pattern: forward only what the user passed; the
    warm start needs nothing special — writing the new bitrate flips the
    preset's objective key, which resets cached scores while keeping best
    parameters and sensitivity ordering as priors)."""
    argv = ["optimize",
            "-p", str(a.preset),
            "--presets-dir", str(a.presets_dir),
            "-i", str(input_url),
            "-b", f"{int(kbps)}k"]
    argv += _opt("--work-dir", a.workdir)
    argv += _opt("--mezz-dir", a.mezz_dir)
    argv += _opt("--stats-dir", a.stats_dir)
    argv += _opt("--clip-start", a.clip_start)
    argv += _opt("--clip-duration", a.clip_duration)
    argv += _opt("--program", a.program)
    argv += _opt("--metric", a.metric)
    argv += _opt("--deinterlace", a.deinterlace)
    argv += _opt("--deint-mode", a.deint_mode)
    argv += _opt("--vmaf-model", a.vmaf_model)
    argv += _opt("--vmaf-subsample", a.vmaf_subsample)
    argv += _opt("--vmaf-threads", a.vmaf_threads)
    argv += _opt("--ffmpeg", a.ffmpeg)
    argv += _opt("--ffprobe", a.ffprobe)
    argv += _opt("--vmaf-ffmpeg", a.vmaf_ffmpeg)
    argv += _opt("--cache-salt", a.cache_salt)
    argv += _opt("--src-primaries", a.src_primaries)
    argv += _opt("--src-trc", a.src_trc)
    argv += _opt("--tonemap", a.tonemap)
    argv += _opt("--norm-scale", a.norm_scale)
    argv += _opt("--audio-stream", a.audio_stream)
    argv += _opt("--extra-input-args", a.extra_input_args)
    argv += _tristate("--keep-trials", a.keep_trials)
    if getattr(a, "retune_args", None):
        argv += shlex.split(a.retune_args)
    return argv
