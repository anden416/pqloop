"""pqloop command line interface.

Subcommands:
  optimize  run the VMAF feedback loop on a clip of the input
  encode    produce the segmented deliverable using a preset's best parameters
  report    summarize a stats JSONL file (and export CSV)
  presets   list / show saved presets
  probe     inspect an input (resolution, interlacing, fps, audio)
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__, media, package, presets, stats as stats_mod, vmaf
from .encoders import codec_family, get_space, known_encoders
from .ffmpeg import FF, FFmpegError
from .optimizer import Optimizer, Settings, NEG_INF
from .runner import RunConfig, TrialRunner
from .segment import final_encode
from .util import (parse_bitrate_kbps, parse_time_seconds, coerce_value,
                   run_stamp, now_iso)

CONFIG_DEFAULTS = {
    "encoder": "libx264",
    "target_bitrate_kbps": None,
    "maxrate_ratio": 1.10,
    "bufsize_ratio": 2.0,
    "bitrate_tolerance": 0.05,
    "overshoot_penalty": 1.0,
    "undershoot_penalty": 0.0,
    "seg_duration": 4.0,
    "gop_duration": None,      # None = lock GOP to the segment (seg_duration)
    "pix_fmt": "yuv420p",
    "scale": "",
    "metric": "mean",
    "vmaf_model": "",
    "vmaf_subsample": 1,
    "vmaf_threads": 0,
    "two_pass": False,
    "deinterlace": "auto",
    "deint_mode": "field",
    "clip_start": 0.0,
    "clip_duration": 30.0,
    "capture_duration": None,
    "program": None,
    "reuse_capture": False,
    "extra_video_args": [],
    "extra_input_args": [],
    "ffmpeg": "ffmpeg",
    "ffprobe": "",
    "vmaf_ffmpeg": "",
    "frozen": {},
    "tune_params": [],
    "exclude_params": [],
    "min_gain": 0.2,
    "adopt_eps": 0.05,
    "max_passes": 6,
    "keep_trials": False,
    "last_input": "",
}


def log(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# config plumbing
# --------------------------------------------------------------------------- #

def merge_config(preset_cfg: dict, cli: dict) -> dict:
    cfg = {}
    for key, default in CONFIG_DEFAULTS.items():
        cli_val = cli.get(key)
        if cli_val is not None:
            value = cli_val
        elif preset_cfg.get(key) is not None:
            value = preset_cfg[key]
        else:
            value = default
        # copy collections so the merged config never aliases CONFIG_DEFAULTS
        # or the preset dict (mutating cfg must not leak into either)
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        cfg[key] = value
    return cfg


def validate_config(cfg) -> None:
    """Fail fast on malformed values — before any capture, mezzanine build, or
    encode has spent time (some of these are otherwise parsed mid-trial)."""
    scale = cfg.get("scale")
    if scale:
        w, x, h = str(scale).lower().partition("x")
        if x != "x" or not (w.isdigit() and h.isdigit() and int(w) and int(h)):
            raise ValueError(f"scale must be WxH, e.g. 1280x720 (got {scale!r})")
    for key in ("clip_duration", "seg_duration", "maxrate_ratio", "bufsize_ratio"):
        if float(cfg[key]) <= 0:
            raise ValueError(f"{key} must be > 0 (got {cfg[key]!r})")
    gop = cfg.get("gop_duration")
    if gop is not None:
        gop = float(gop)
        if gop <= 0:
            raise ValueError(f"gop_duration must be > 0 (got {cfg['gop_duration']!r})")
        seg = float(cfg["seg_duration"])
        mult = seg / gop
        # the segment must hold a whole number of GOPs so every segment still
        # starts on a keyframe (HLS/DASH require keyframe-aligned segment starts)
        if round(mult) < 1 or abs(mult - round(mult)) > 1e-6:
            raise ValueError(
                f"seg_duration ({seg:g}s) must be a whole multiple of "
                f"gop_duration ({gop:g}s) so every segment starts on a keyframe "
                f"(e.g. a 2s GOP inside 4s segments)")
    if float(cfg["clip_start"]) < 0:
        raise ValueError(f"clip_start must be >= 0 (got {cfg['clip_start']!r})")
    if int(cfg["vmaf_subsample"]) < 1:
        raise ValueError(f"vmaf_subsample must be >= 1 (got {cfg['vmaf_subsample']!r})")
    if cfg["metric"] not in vmaf.METRIC_KEYS:
        raise ValueError(f"unknown metric {cfg['metric']!r} "
                         f"(one of: {', '.join(sorted(vmaf.METRIC_KEYS))})")


def preset_params(data, space, name="preset", log_fn=log) -> dict:
    """A preset's encode parameters: the completed best result, else the
    current search point, else encoder defaults (with a warning)."""
    params = (data.get("best") or {}).get("params")
    if params:
        return params
    current = (data.get("optimizer") or {}).get("current")
    if current:
        log_fn(f"note: {name} has no completed best result; "
               "using current search point")
        return space.effective(current)
    log_fn(f"warning: {name} has no optimizer state; using encoder defaults")
    return space.effective(space.defaults())


def parse_freezes(space, cfg_frozen, freeze_args, unfreeze_args) -> dict:
    frozen = dict(cfg_frozen or {})
    for item in freeze_args or []:
        if "=" not in item:
            raise ValueError(f"--freeze expects name=value, got {item!r}")
        name, _, raw = item.partition("=")
        name = name.strip()
        value = coerce_value(raw)
        if name in space.params:
            spec = space.params[name]
            if value is not None and any(isinstance(v, float) for v in spec.values):
                value = float(value)
        frozen[name] = value
    for name in unfreeze_args or []:
        frozen.pop(name.strip(), None)
    return frozen


# Config keys that change what a cached objective value means. When any of
# them changes, cached trial scores are no longer comparable and must be
# dropped (encoder is included because cache signatures are bare param dicts
# and e.g. x264/x265 share parameter names).
OBJECTIVE_KEYS = ("encoder", "target_bitrate_kbps", "maxrate_ratio",
                  "bufsize_ratio", "bitrate_tolerance", "overshoot_penalty",
                  "undershoot_penalty", "seg_duration", "gop_duration",
                  "pix_fmt", "scale", "metric", "vmaf_model", "vmaf_subsample",
                  "two_pass", "extra_video_args")


def objective_key(cfg) -> str:
    # None-valued keys are omitted so adding an optional objective key (e.g.
    # gop_duration, unset) doesn't change the signature of an existing preset
    # and spuriously reset its cache; no non-optional key is ever None here.
    return json.dumps({k: cfg.get(k) for k in OBJECTIVE_KEYS
                       if cfg.get(k) is not None}, sort_keys=True)


def reset_stale_state(data, opt_state, fingerprint, okey, log_fn=log) -> list:
    """Drop cached trials/best when the reference clip or the objective
    definition changed since the preset was last saved; the current point and
    sensitivity ordering are kept as priors either way. Presets from before
    objective-key tracking are grandfathered (key stored, nothing reset)."""
    reasons = []
    if data.get("fingerprint") and data["fingerprint"] != fingerprint:
        reasons.append("reference clip changed")
    if data.get("objective_key") is None:
        if opt_state.get("cache"):
            log_fn("note: preset predates objective-key tracking; "
                   "adopting the current objective settings as its baseline")
    elif data["objective_key"] != okey:
        reasons.append("objective settings changed")
    if reasons:
        log_fn(f"{' and '.join(reasons)} since last run -> scores reset "
               "(best parameters and impact ordering carried over)")
        opt_state.pop("cache", None)
        opt_state.pop("best", None)
        opt_state["screened"] = False
        opt_state["passes_done"] = 0
    data["fingerprint"] = fingerprint
    data["objective_key"] = okey
    return reasons


def resolve_measure_ff(cfg):
    candidates = []
    for cand in (cfg.get("vmaf_ffmpeg"), cfg.get("ffmpeg"), "ffmpeg"):
        if cand and cand not in candidates:
            candidates.append(cand)
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        cand = base / "tools" / "ffmpeg-static" / "bin" / "ffmpeg"
        if str(cand) not in candidates:
            candidates.append(str(cand))
    for cand in candidates:
        if "/" in str(cand) and not Path(cand).exists():
            continue
        ff = FF(cand)
        if ff.has_filter("libvmaf"):
            return ff
    raise RuntimeError(
        "no ffmpeg with the libvmaf filter found (tried: "
        + ", ".join(str(c) for c in candidates) + "). Install one (e.g. a BtbN "
        "static build) and point --vmaf-ffmpeg at it.")


def mezz_builder_ff(ff_enc, ff_meas):
    """Mezzanine needs lossless libx264; hardware-only encode builds may lack it."""
    return ff_enc if ff_enc.has_encoder("libx264") else ff_meas


def prepare_source(ff, cfg, input_url, workdir, capture_name="capture.ts"):
    if media.is_live_url(input_url):
        seconds = cfg.get("capture_duration") or (
            float(cfg["clip_start"]) + float(cfg["clip_duration"]) + 2.0)
        if float(seconds) < float(cfg["clip_start"]) + float(cfg["clip_duration"]):
            log(f"warning: --capture-duration {seconds:g}s is shorter than "
                f"clip start + duration; the reference clip will be cut short")
        captured = media.get_or_capture_live(
            ff, input_url, float(seconds), Path(workdir) / capture_name,
            cfg.get("extra_input_args") or [],
            program=cfg.get("program"),
            reuse=bool(cfg.get("reuse_capture")), log=log)
        # the capture is a single-program TS, so no program selection here
        src = media.probe_file(ff, captured)
        return src, True
    src = media.probe_file(ff, input_url, program=cfg.get("program"))
    return src, False


def build_run_cfg(cfg) -> RunConfig:
    return RunConfig(
        encoder=cfg["encoder"],
        target_bitrate_kbps=int(cfg["target_bitrate_kbps"]),
        maxrate_ratio=float(cfg["maxrate_ratio"]),
        bufsize_ratio=float(cfg["bufsize_ratio"]),
        bitrate_tolerance=float(cfg["bitrate_tolerance"]),
        overshoot_penalty=float(cfg["overshoot_penalty"]),
        undershoot_penalty=float(cfg["undershoot_penalty"]),
        seg_duration=float(cfg["seg_duration"]),
        gop_duration=(float(cfg["gop_duration"])
                      if cfg.get("gop_duration") else None),
        pix_fmt=cfg["pix_fmt"], scale=cfg["scale"], metric=cfg["metric"],
        vmaf_model=cfg["vmaf_model"], vmaf_subsample=int(cfg["vmaf_subsample"]),
        vmaf_threads=int(cfg["vmaf_threads"]), two_pass=bool(cfg["two_pass"]),
        extra_video_args=list(cfg["extra_video_args"] or []),
        keep_trials=bool(cfg["keep_trials"]),
    )


# --------------------------------------------------------------------------- #
# optimize
# --------------------------------------------------------------------------- #

def cmd_optimize(a) -> int:
    preset_path = presets.resolve(a.preset, a.presets_dir)
    data = presets.load(preset_path)
    resuming = bool(data.get("optimizer", {}).get("cache"))

    cli_cfg = {
        "encoder": a.encoder,
        "target_bitrate_kbps": parse_bitrate_kbps(a.target_bitrate) if a.target_bitrate else None,
        "maxrate_ratio": a.maxrate_ratio,
        "bufsize_ratio": a.bufsize_ratio,
        "bitrate_tolerance": a.bitrate_tolerance,
        "overshoot_penalty": a.overshoot_penalty,
        "undershoot_penalty": a.undershoot_penalty,
        "seg_duration": a.seg_duration,
        "gop_duration": a.gop_duration,
        "pix_fmt": a.pix_fmt,
        "scale": a.scale,
        "metric": a.metric,
        "vmaf_model": a.vmaf_model,
        "vmaf_subsample": a.vmaf_subsample,
        "vmaf_threads": a.vmaf_threads,
        "two_pass": a.two_pass,
        "deinterlace": a.deinterlace,
        "deint_mode": a.deint_mode,
        "clip_start": parse_time_seconds(a.clip_start) if a.clip_start is not None else None,
        "clip_duration": a.clip_duration,
        "capture_duration": a.capture_duration,
        "program": a.program,
        "reuse_capture": a.reuse_capture,
        "extra_video_args": shlex.split(a.extra_video_args) if a.extra_video_args else None,
        "extra_input_args": shlex.split(a.extra_input_args) if a.extra_input_args else None,
        "ffmpeg": a.ffmpeg,
        "ffprobe": a.ffprobe,
        "vmaf_ffmpeg": a.vmaf_ffmpeg,
        "tune_params": [s.strip() for s in a.tune_params.split(",") if s.strip()]
                       if a.tune_params else None,
        "exclude_params": [s.strip() for s in a.exclude_params.split(",") if s.strip()]
                          if a.exclude_params else None,
        "min_gain": a.min_gain,
        "adopt_eps": a.adopt_eps,
        "max_passes": a.max_passes,
        "keep_trials": a.keep_trials,
        "last_input": a.input,
    }
    cfg = merge_config(data.get("config") or {}, cli_cfg)

    input_url = a.input or cfg.get("last_input")
    if not input_url:
        raise ValueError("no input: pass --input FILE|udp://... "
                         "(presets remember their last input)")
    cfg["last_input"] = input_url
    if not cfg["target_bitrate_kbps"]:
        raise ValueError("no target bitrate: pass --target-bitrate, e.g. -b 6000k")

    space = get_space(cfg["encoder"])
    if space.experimental:
        log(f"WARNING: the {cfg['encoder']!r} parameter space is EXPERIMENTAL "
            f"(not yet validated on hardware) — check commands with --dry-run first.")
    cfg["frozen"] = parse_freezes(space, cfg.get("frozen"), a.freeze, a.unfreeze)
    validate_config(cfg)
    data["config"] = cfg

    ff_enc = FF(cfg["ffmpeg"] or "ffmpeg", cfg["ffprobe"] or None)
    # --dry-run may legitimately run on a box without the target encoder
    # (e.g. checking a netint command before moving to Quadra hardware)
    if not a.dry_run and not ff_enc.has_encoder(cfg["encoder"]):
        raise RuntimeError(f"encoder {cfg['encoder']!r} not available in "
                           f"{cfg['ffmpeg']} ({ff_enc.version()})")

    workdir = Path(a.workdir or Path("work") / data["name"])

    if a.dry_run:
        # Genuinely dry: the input is only probed (for GOP/deinterlace), and no
        # libvmaf build is resolved, no live capture recorded, no mezzanine
        # built, nothing written to the workdir or the preset.
        src = media.probe_file(ff_enc, input_url, program=cfg.get("program"))
        deinterlaced = media.deinterlace_decision(src, cfg["deinterlace"])
        fps, fps_str = media.output_fps(src.fps, src.fps_str, deinterlaced,
                                        cfg["deint_mode"])
        mezz = media.MezzInfo(
            path=str(workdir / "mezz.mkv"), width=src.width, height=src.height,
            fps=fps, fps_str=fps_str, duration=float(cfg["clip_duration"]),
            fingerprint="", deinterlaced=deinterlaced, filters="",
            inputs_key="")
        runner = TrialRunner(build_run_cfg(cfg), ff_enc, ff_enc, space, mezz,
                             workdir, log)
        params = space.defaults()
        params.update((data.get("optimizer") or {}).get("current") or {})
        params.update(cfg["frozen"])
        cmd = [cfg["ffmpeg"] or "ffmpeg", "-hide_banner", "-nostdin",
               *runner.encode_args(params, workdir / "trials" / "tXXXX.mp4")]
        log("baseline encode command:")
        log("  " + shlex.join(str(c) for c in cmd))
        return 0

    ff_meas = resolve_measure_ff(cfg)
    workdir.mkdir(parents=True, exist_ok=True)

    src, _ = prepare_source(ff_enc, cfg, input_url, workdir)
    mezz = media.get_or_build_mezzanine(
        mezz_builder_ff(ff_enc, ff_meas), src,
        start=float(cfg["clip_start"]),
        duration=float(cfg["clip_duration"]),
        deint=cfg["deinterlace"], deint_mode=cfg["deint_mode"],
        out_path=workdir / "mezz.mkv", log=log)

    opt_state = dict(data.get("optimizer") or {})
    reset_stale_state(data, opt_state, mezz.fingerprint, objective_key(cfg), log)

    if not space.params:
        log(f"note: no curated parameter space for {cfg['encoder']!r}; only the "
            f"baseline (rate control + GOP + --extra-video-args) will be measured.")

    run_cfg = build_run_cfg(cfg)
    runner = TrialRunner(run_cfg, ff_enc, ff_meas, space, mezz, workdir, log)

    run_id = f"{run_stamp()}_{data['name']}"
    stats = stats_mod.StatsWriter(a.stats_dir or "stats", run_id)
    stats.event("meta", schema=stats_mod.SCHEMA, pqloop=__version__, config=cfg,
                **stats_mod.host_meta(),
                source=asdict(src), mezzanine=asdict(mezz),
                encode_ffmpeg=ff_enc.version(), vmaf_ffmpeg=ff_meas.version(),
                gop=runner.gop_len,
                tunables=[s.name for s in space.tunable(
                    cfg["tune_params"] or None, cfg["exclude_params"],
                    cfg["frozen"].keys())])

    log(f"pqloop {__version__} — run {run_id}"
        + (" (resuming)" if resuming else ""))
    log(f"  source:    {src.path}  {src.width}x{src.height} {src.field_order} "
        f"{src.fps:g}fps  audio={'yes' if src.has_audio else 'no'}")
    log(f"  reference: {mezz.path}  {mezz.width}x{mezz.height} {mezz.fps:g}fps "
        f"{mezz.duration:.1f}s  deinterlaced={mezz.deinterlaced}")
    log(f"  encoder:   {cfg['encoder']} @ {cfg['target_bitrate_kbps']}k "
        f"(maxrate {runner.rc.maxrate_kbps}k, bufsize {runner.rc.bufsize_kbps}k, "
        f"GOP {runner.gop_len} ({run_cfg.gop_duration or run_cfg.seg_duration:g}s), "
        f"seg {cfg['seg_duration']:g}s"
        + (", two-pass" if run_cfg.two_pass else "") + ")")
    log(f"  metric:    vmaf {cfg['metric']} "
        f"(model {cfg['vmaf_model'] or 'vmaf_v0.6.1 default'}, "
        f"subsample {cfg['vmaf_subsample']}); objective = vmaf - bitrate overshoot penalty")
    if cfg["frozen"]:
        log(f"  frozen:    {cfg['frozen']}")
    log("")

    counter = {"n": 0}
    first = {"baseline": None}
    holder = {}   # filled with the Optimizer once constructed

    def checkpoint():
        opt_now = holder.get("opt")
        if opt_now is None:
            return
        data["optimizer"] = opt_now.state()
        data["best"] = data["optimizer"]["best"]
        presets.save(preset_path, data)

    def on_trial(phase, label, params, outcome, cached, best, encodes):
        counter["n"] += 1
        n = counter["n"]
        eff = space.effective(params)
        if phase == "baseline" and first["baseline"] is None and outcome.ok:
            first["baseline"] = outcome
        if outcome.ok:
            m = outcome.metrics
            is_best = outcome.objective >= best - 1e-9
            line = (f"[{n:3d}] {phase:<8} {label:<24} "
                    f"vmaf {m.get('vmaf_mean', 0):6.2f} hm {m.get('vmaf_harmonic', 0):6.2f} "
                    f"p1 {m.get('vmaf_p1', 0):6.2f}  {m.get('bitrate_kbps', 0):6.0f}k "
                    f"{m.get('encode_time_s', 0):6.1f}s  obj {outcome.objective:8.3f}"
                    f"{' *' if is_best else '  '}{' (cached)' if cached else ''}")
        else:
            line = (f"[{n:3d}] {phase:<8} {label:<24} FAILED: "
                    f"{outcome.error[:90]}{' (cached)' if cached else ''}")
        log(line)
        stats.event("trial", n=n, phase=phase, label=label, cached=cached,
                    ok=outcome.ok,
                    objective=None if outcome.objective == NEG_INF else outcome.objective,
                    params=eff, metrics=outcome.metrics, error=outcome.error,
                    best_objective=None if best == NEG_INF else best)
        if not cached:
            # Crash/kill safety: every real encode is expensive — persist the
            # optimizer state (including the trial cache) as we go.
            checkpoint()

    settings = Settings(
        min_pass_gain=float(cfg["min_gain"]),
        adopt_eps=float(cfg["adopt_eps"]),
        max_trials=int(a.max_trials or 0),
        max_seconds=float(a.max_seconds or 0),
        target_score=float(a.target_score or 0),
        max_passes=int(cfg["max_passes"]),
        screen=not a.no_screen,
    )
    opt = Optimizer(space, runner.evaluate, settings, state=opt_state,
                    include=cfg["tune_params"] or None,
                    exclude=cfg["exclude_params"], frozen=cfg["frozen"],
                    on_trial=on_trial, log=log)
    holder["opt"] = opt

    def _sigterm(_sig, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass  # not the main thread / unsupported platform

    started = time.monotonic()
    started_iso = now_iso()
    stop_reason = "interrupted"
    exit_code = 0
    try:
        stop_reason = opt.run()
    except KeyboardInterrupt:
        log("\ninterrupted — saving state ...")
        exit_code = 130
    finally:
        elapsed = time.monotonic() - started
        data["optimizer"] = opt.state()
        data["best"] = data["optimizer"]["best"]
        data["runs"].append({
            "run_id": run_id, "started": started_iso, "ended": now_iso(),
            "input": input_url, "clip_start": cfg["clip_start"],
            "clip_duration": cfg["clip_duration"],
            "encodes": opt.run_encodes, "stop_reason": stop_reason,
            "elapsed_s": round(elapsed, 1),
        })
        presets.save(preset_path, data)
        stats.event("done", stop_reason=stop_reason, elapsed_s=round(elapsed, 1),
                    encodes=opt.run_encodes,
                    best=data["best"])
        stats.close()
        csv_path = stats_mod.to_csv(stats.path)

    log("")
    log(f"stopped: {stop_reason}  ({opt.run_encodes} encodes, {elapsed:.0f}s)")
    baseline = first["baseline"]
    if opt.best_params is not None and opt.best_objective != NEG_INF:
        bm = opt.best_metrics
        log(f"best: objective {opt.best_objective:.3f}  "
            f"VMAF mean {bm.get('vmaf_mean')} harmonic {bm.get('vmaf_harmonic')} "
            f"p1 {bm.get('vmaf_p1')} @ {bm.get('bitrate_kbps')}kbps")
        if baseline is not None:
            log(f"      vs baseline {baseline.objective:.3f}  "
                f"(gain +{opt.best_objective - baseline.objective:.3f})")
        log(f"best params: {json.dumps(opt.best_params, sort_keys=True)}")
    log(f"preset: {preset_path}")
    log(f"stats:  {stats.path}  |  {csv_path}")
    if stop_reason == "max_passes":
        log("tip: resume with a higher --max-passes to keep refining")
    elif stop_reason in ("max_trials", "time_limit"):
        log("tip: re-run the same command to resume where the budget cut off")
    return exit_code


# --------------------------------------------------------------------------- #
# encode
# --------------------------------------------------------------------------- #

def cmd_encode(a) -> int:
    preset_path = presets.resolve(a.preset, a.presets_dir)
    if not Path(preset_path).exists():
        raise ValueError(f"preset not found: {preset_path}")
    data = presets.load(preset_path)
    cfg = merge_config(data.get("config") or {}, {
        "seg_duration": a.seg_duration,
        "gop_duration": a.gop_duration,
        "ffmpeg": a.ffmpeg,
        "ffprobe": a.ffprobe,
        "extra_input_args": shlex.split(a.extra_input_args) if a.extra_input_args else None,
        "target_bitrate_kbps": parse_bitrate_kbps(a.target_bitrate) if a.target_bitrate else None,
        "program": a.program,
    })
    validate_config(cfg)
    space = get_space(cfg["encoder"])

    params = preset_params(data, space, name=data.get("name", "preset"))

    if not cfg["target_bitrate_kbps"]:
        raise ValueError("preset has no target bitrate; pass --target-bitrate")

    ff = FF(cfg["ffmpeg"] or "ffmpeg", cfg["ffprobe"] or None)
    input_url = a.input or cfg.get("last_input")
    if not input_url:
        raise ValueError("no input: pass --input FILE|udp://...")

    out_dir = Path(a.output_dir)
    if media.is_live_url(input_url):
        if not a.capture_duration:
            raise ValueError("live input: pass --capture-duration SECONDS to bound the recording")
        captured = media.capture_live(ff, input_url, float(a.capture_duration),
                                      out_dir / "_capture.ts",
                                      cfg.get("extra_input_args") or [],
                                      program=cfg.get("program"), log=log)
        src = media.probe_file(ff, captured)
    else:
        src = media.probe_file(ff, input_url, program=cfg.get("program"))

    log(f"encoding with params: {json.dumps(params, sort_keys=True)}")
    result = final_encode(ff, space, params, cfg, src, out_dir,
                          fmt=a.format, hls_segment_type=a.hls_segment_type,
                          audio_kbps=parse_bitrate_kbps(a.audio_bitrate),
                          want_audio=not a.no_audio,
                          start=parse_time_seconds(a.start) if a.start else None,
                          duration=a.duration, log=log)
    log(f"done: {result['output']}  ({result['segments']} media files, "
        f"GOP {result['gop']} @ {result['fps']:g}fps"
        f"{', deinterlaced' if result['deinterlaced'] else ''})")
    return 0


# --------------------------------------------------------------------------- #
# package
# --------------------------------------------------------------------------- #

def cmd_package(a) -> int:
    rungs, last_inputs = [], []
    for name in a.preset:
        preset_path = presets.resolve(name, a.presets_dir)
        if not Path(preset_path).exists():
            raise ValueError(f"preset not found: {preset_path}")
        data = presets.load(preset_path)
        stored = data.get("config") or {}
        cfg = merge_config(stored, {
            "seg_duration": a.seg_duration,
            "extra_input_args": shlex.split(a.extra_input_args) if a.extra_input_args else None,
            "program": a.program,
        })
        validate_config(cfg)
        if not cfg["target_bitrate_kbps"]:
            raise ValueError(f"preset {data['name']} has no target bitrate")
        if (a.seg_duration and stored.get("seg_duration")
                and float(stored["seg_duration"]) != float(a.seg_duration)):
            log(f"warning: {data['name']}: --seg-duration {a.seg_duration:g} overrides "
                f"the value its parameters were tuned at ({stored['seg_duration']:g}s)")
        space = get_space(cfg["encoder"])
        params = preset_params(data, space, name=data["name"])
        ff = FF(cfg["ffmpeg"] or "ffmpeg", cfg["ffprobe"] or None)
        rungs.append(package.Rung(preset_name=data["name"], cfg=cfg, space=space,
                                  params=params, ff=ff))
        last_inputs.append(cfg.get("last_input") or "")

    input_url = a.input or next((u for u in last_inputs if u), "")
    if not input_url:
        raise ValueError("no input: pass --input FILE|udp://... "
                         "(or optimize the presets so they remember one)")
    if not a.input and len({u for u in last_inputs if u}) > 1:
        log(f"warning: presets disagree on their last input; "
            f"using {input_url} (pass -i to override)")

    out_dir = Path(a.output_dir)
    work_dir = out_dir / "_work"
    ff_mux = FF(a.ffmpeg, a.ffprobe or None) if a.ffmpeg else rungs[0].ff
    first_cfg = rungs[0].cfg

    if media.is_live_url(input_url):
        if not a.capture_duration:
            raise ValueError("live input: pass --capture-duration SECONDS to bound the recording")
        captured = media.get_or_capture_live(
            ff_mux, input_url, float(a.capture_duration), work_dir / "capture.ts",
            first_cfg.get("extra_input_args") or [],
            program=first_cfg.get("program"), reuse=not a.no_reuse, log=log)
        src = media.probe_file(ff_mux, captured)
    else:
        src = media.probe_file(ff_mux, input_url, program=first_cfg.get("program"))

    rungs.sort(key=lambda r: r.target_kbps)
    for rung in rungs:
        package.resolve_output(rung, src)
    package.assign_names(rungs)
    if a.h264_level:
        for rung in rungs:
            if codec_family(rung.space.codec) == "h264":
                rung.cfg["extra_video_args"] = list(
                    rung.cfg.get("extra_video_args") or []) + ["-level", str(a.h264_level)]
    for warning in package.validate_rungs(rungs):
        log(f"warning: {warning}")

    log(f"packaging {len(rungs)} rung(s) of {src.path} "
        f"({src.width}x{src.height} {src.fps:g}fps, "
        f"audio={'yes' if src.has_audio else 'no'}) -> {a.format}")
    start = parse_time_seconds(a.start) if a.start else None
    use_audio = not a.no_audio and src.has_audio
    inter = package.build_intermediates(
        rungs, src, work_dir, want_audio=use_audio,
        audio_kbps=parse_bitrate_kbps(a.audio_bitrate),
        start=start, duration=a.duration, reuse=not a.no_reuse,
        ff_audio=ff_mux, log=log)

    seg = float(rungs[0].cfg["seg_duration"])
    mux, main_output = package.mux_args(
        a.format, out_dir, inter["video"], [r.name for r in rungs],
        audio_path=inter["audio"], seg_duration=seg,
        hls_segment_type=a.hls_segment_type)
    dur = a.duration or max(0.0, (src.duration or 0.0) - (start or 0.0))
    log(f"muxing {a.format} package -> {main_output}")
    ff_mux.run(mux, timeout=package.mux_timeout(dur, len(rungs)))

    if a.format in ("hls", "cmaf"):
        fixed = package.fixup_master(out_dir / "master.m3u8", fps=rungs[0].fps,
                                     audio_codec=package.AAC_CODEC if inter["audio"] else None)
        if fixed:
            log("master playlist fixup: " + "; ".join(fixed))

    lines, level_warnings = package.stream_report(rungs, inter["video"])
    log("rungs:")
    for line in lines:
        log(line)
    problems = []
    if not a.no_verify:
        problems = package.verify_package(rungs, inter["video"], seg, log=log)
        if not problems:
            log("verified: keyframes aligned across all rungs, IDR on segment boundaries")
    for issue in level_warnings + problems:
        log(f"warning: {issue}")

    if a.clean:
        shutil.rmtree(work_dir, ignore_errors=True)
        log("removed _work/ intermediates (--clean)")
    else:
        log(f"intermediates kept in {work_dir} — re-run with another --format "
            f"to re-package without re-encoding (--clean to remove)")
    log(f"done: {main_output}")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# report / presets / probe
# --------------------------------------------------------------------------- #

def cmd_report(a) -> int:
    print(stats_mod.summarize(a.stats_file))
    csv_path = stats_mod.to_csv(a.stats_file, a.csv)
    print(f"\ncsv: {csv_path}")
    return 0


def cmd_presets(a) -> int:
    if a.show:
        path = presets.resolve(a.show, a.presets_dir)
        # load() creates a fresh preset for missing paths (optimize's
        # create-on-first-run behavior); --show should not present one as real
        if not Path(path).exists():
            raise ValueError(f"preset not found: {path}")
        print(json.dumps(presets.load(path), indent=2))
        return 0
    rows = presets.list_presets(a.presets_dir)
    if not rows:
        print(f"no presets in {a.presets_dir}/")
        return 0
    print(f"{'name':<20} {'encoder':<12} {'target':>8} {'best obj':>9} "
          f"{'encodes':>8}  updated")
    for r in rows:
        best = f"{r['best_objective']:.2f}" if r["best_objective"] is not None else "-"
        target = f"{r['target_kbps']}k" if r["target_kbps"] else "-"
        print(f"{r['name']:<20} {r['encoder']:<12} {target:>8} {best:>9} "
              f"{r['encodes']:>8}  {r['updated']}")
    return 0


def cmd_probe(a) -> int:
    ff = FF(a.ffmpeg or "ffmpeg", a.ffprobe or None)
    if media.is_live_url(a.input):
        log(f"probing live stream {a.input} ...")
    data = ff.probe(a.input)
    src = media.parse_probe(data, a.input, program=a.program)
    interlaced = src.interlaced
    fps_out, _ = media.output_fps(src.fps, src.fps_str, interlaced, "field")
    print(f"input:       {src.path}")
    programs = [p.get("program_id") for p in data.get("programs") or []]
    if programs:
        print(f"programs:    {', '.join(str(p) for p in programs)}"
              "   (select one with --program)")
    print(f"video:       {src.video_codec} {src.width}x{src.height} "
          f"{src.pix_fmt} {src.fps:g}fps field_order={src.field_order}")
    print(f"duration:    {src.duration:.1f}s   audio: {'yes' if src.has_audio else 'no'}")
    print(f"interlaced:  {interlaced}"
          + (f" -> --deinterlace auto will apply bwdif "
             f"(field mode gives {fps_out:g}fps)" if interlaced else ""))
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pqloop",
        description="VMAF-feedback picture quality optimization loop for ffmpeg")
    p.add_argument("--version", action="version", version=f"pqloop {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    Bool = argparse.BooleanOptionalAction

    o = sub.add_parser("optimize", help="run the optimization loop")
    o.add_argument("-i", "--input", help="input file or live url (udp/rtp/srt/rist)")
    o.add_argument("-p", "--preset", default="default",
                   help="preset name (in --presets-dir) or path to .json")
    o.add_argument("--presets-dir", default="presets")
    o.add_argument("--encoder", help=f"video encoder (curated: {', '.join(known_encoders())}; "
                                     "others get rate control + GOP only)")
    o.add_argument("-b", "--target-bitrate", help="video target bitrate, e.g. 6000k")
    o.add_argument("--clip-start", help="clip start in source (seconds or HH:MM:SS)")
    o.add_argument("--clip-duration", type=float, help="loop clip length in seconds (default 30)")
    o.add_argument("--capture-duration", type=float,
                   help="live input: seconds to record "
                        "(default clip start + clip duration + 2)")
    o.add_argument("--program", type=int,
                   help="MPTS input: transport stream program to capture/probe")
    o.add_argument("--reuse-capture", action=Bool, default=None,
                   help="live input: reuse an existing capture (same url/program) "
                        "instead of recapturing, so runs can resume")
    o.add_argument("--seg-duration", type=float,
                   help="segment duration in seconds (default 4); also the GOP "
                        "unless --gop-duration is given")
    o.add_argument("--gop-duration", type=float,
                   help="GOP/keyframe interval in seconds, independent of the "
                        "segment (default: = --seg-duration). Must divide the "
                        "segment evenly, e.g. 2 with --seg-duration 4")
    o.add_argument("--scale", help="encode at WxH (VMAF still compares at source resolution)")
    o.add_argument("--pix-fmt", help="encode pixel format (default yuv420p)")
    o.add_argument("--deinterlace", choices=("auto", "on", "off"))
    o.add_argument("--deint-mode", choices=("field", "frame"),
                   help="field: bwdif to double rate (50i->50p); frame: keep rate")
    o.add_argument("--metric", choices=sorted(vmaf.METRIC_KEYS),
                   help="vmaf aggregate to optimize (default mean)")
    o.add_argument("--min-gain", type=float,
                   help="stop when a full pass gains less than this (default 0.2)")
    o.add_argument("--adopt-eps", type=float,
                   help="minimum improvement to adopt a change (default 0.05)")
    o.add_argument("--max-trials", type=int, default=0, help="encode budget for this run")
    o.add_argument("--max-seconds", type=float, default=0, help="time budget for this run")
    o.add_argument("--target-score", type=float, default=0,
                   help="stop once the objective reaches this")
    o.add_argument("--max-passes", type=int, help="refinement pass budget (default 6)")
    o.add_argument("--no-screen", action="store_true",
                   help="skip the sensitivity screening phase")
    o.add_argument("--tune-params", help="only tune these (comma separated)")
    o.add_argument("--exclude-params", help="never tune these (comma separated)")
    o.add_argument("--freeze", action="append", metavar="NAME=VALUE",
                   help="pin a parameter (repeatable); excluded from tuning")
    o.add_argument("--unfreeze", action="append", metavar="NAME")
    o.add_argument("--two-pass", action=Bool, default=None,
                   help="two-pass rate control (libx264/libx265)")
    o.add_argument("--vmaf-model", help="libvmaf model spec, e.g. version=vmaf_v0.6.1")
    o.add_argument("--vmaf-subsample", type=int, help="score every Nth frame (default 1)")
    o.add_argument("--vmaf-threads", type=int)
    o.add_argument("--ffmpeg", help="encode ffmpeg binary (nvenc/netint builds etc.)")
    o.add_argument("--ffprobe")
    o.add_argument("--vmaf-ffmpeg", help="measurement ffmpeg with libvmaf "
                                         "(auto-detected if omitted)")
    o.add_argument("--work-dir", "--workdir", dest="workdir",
                   help="working directory (default work/<preset>)")
    o.add_argument("--stats-dir", help="statistics directory (default stats/)")
    o.add_argument("--keep-trials", action=Bool, default=None,
                   help="keep every trial encode + per-frame vmaf log")
    o.add_argument("--maxrate-ratio", type=float, help="maxrate = ratio*target (default 1.10)")
    o.add_argument("--bufsize-ratio", type=float, help="bufsize = ratio*target (default 2.0)")
    o.add_argument("--bitrate-tolerance", type=float,
                   help="tolerated overshoot fraction before penalty (default 0.05)")
    o.add_argument("--overshoot-penalty", type=float,
                   help="objective points per %% beyond tolerance (default 1.0)")
    o.add_argument("--undershoot-penalty", type=float,
                   help="objective points per %% under target beyond tolerance "
                        "(default 0 = undershoot not penalized)")
    o.add_argument("--extra-video-args", help="extra ffmpeg output args for every encode")
    o.add_argument("--extra-input-args", help="extra ffmpeg input args (live tuning etc.)")
    o.add_argument("--dry-run", action="store_true",
                   help="print the baseline encode command and exit")
    o.set_defaults(func=cmd_optimize)

    e = sub.add_parser("encode", help="produce segmented output from a preset's best params")
    e.add_argument("-p", "--preset", required=True)
    e.add_argument("--presets-dir", default="presets")
    e.add_argument("-i", "--input", help="input file or live url (default: preset's last input)")
    e.add_argument("-o", "--output-dir", required=True)
    e.add_argument("--format", choices=("hls", "dash", "cmaf", "fmp4", "mp4"),
                   default="hls",
                   help="cmaf = one fMP4 segment set with both DASH and HLS "
                        "manifests; fmp4 = single fragmented mp4 file; "
                        "mp4 = single progressive (faststart) mp4 file")
    e.add_argument("--hls-segment-type", choices=("fmp4", "mpegts"), default="fmp4")
    e.add_argument("--seg-duration", type=float)
    e.add_argument("--gop-duration", type=float,
                   help="GOP/keyframe interval in seconds (default: = seg duration)")
    e.add_argument("--target-bitrate", help="override preset bitrate")
    e.add_argument("--audio-bitrate", default="128k",
                   help="audio bitrate (default 128k)")
    e.add_argument("--no-audio", action="store_true")
    e.add_argument("--start", help="start position in the input (seconds or HH:MM:SS)")
    e.add_argument("--duration", type=float, help="only encode N seconds")
    e.add_argument("--capture-duration", "--record-duration",
                   dest="capture_duration", type=float,
                   help="live input: seconds to record")
    e.add_argument("--program", type=int,
                   help="MPTS input: transport stream program to capture/probe")
    e.add_argument("--ffmpeg")
    e.add_argument("--ffprobe")
    e.add_argument("--extra-input-args",
                   help="extra ffmpeg input args (live tuning etc.)")
    e.set_defaults(func=cmd_encode)

    pk = sub.add_parser("package",
                        help="package multiple presets into one ABR HLS/DASH output")
    pk.add_argument("-p", "--preset", action="append", required=True,
                    help="ladder rung preset; repeat per rung (order is "
                         "irrelevant — variants are sorted by bitrate)")
    pk.add_argument("--presets-dir", default="presets")
    pk.add_argument("-i", "--input",
                    help="input file or live url (default: the presets' last input)")
    pk.add_argument("-o", "--output-dir", required=True)
    pk.add_argument("--format", choices=("hls", "dash", "cmaf"), default="hls",
                    help="cmaf = one fMP4 segment set with both manifests")
    pk.add_argument("--hls-segment-type", choices=("fmp4", "mpegts"), default="fmp4")
    pk.add_argument("--seg-duration", type=float,
                    help="override every rung's segment duration (warns: "
                         "parameters were tuned at the preset's value)")
    pk.add_argument("--audio-bitrate", default="128k",
                    help="shared audio rendition bitrate (default 128k)")
    pk.add_argument("--no-audio", action="store_true")
    pk.add_argument("--start", help="start position in the input (seconds or HH:MM:SS)")
    pk.add_argument("--duration", type=float, help="only encode N seconds")
    pk.add_argument("--capture-duration", "--record-duration",
                    dest="capture_duration", type=float,
                    help="live input: seconds to record")
    pk.add_argument("--program", type=int,
                    help="MPTS input: transport stream program to capture/probe")
    pk.add_argument("--h264-level",
                    help="cap the H.264 level at encode time, e.g. 4.1 (the "
                         "encoder clamps refs to fit; re-optimizing with "
                         "--freeze refs=N is the better long-term fix)")
    pk.add_argument("--no-reuse", action="store_true",
                    help="re-encode intermediates even when they match")
    pk.add_argument("--clean", action="store_true",
                    help="delete _work/ intermediates after a successful package")
    pk.add_argument("--no-verify", action="store_true",
                    help="skip the cross-rung keyframe alignment check")
    pk.add_argument("--ffmpeg",
                    help="binary for capture/audio/mux steps (default: first "
                         "rung's; each rung encodes with its own preset's ffmpeg)")
    pk.add_argument("--ffprobe")
    pk.add_argument("--extra-input-args",
                    help="extra ffmpeg input args for live capture")
    pk.set_defaults(func=cmd_package)

    r = sub.add_parser("report", help="summarize a stats .jsonl (writes CSV too)")
    r.add_argument("stats_file")
    r.add_argument("--csv", help="CSV output path (default: alongside the jsonl)")
    r.set_defaults(func=cmd_report)

    pr = sub.add_parser("presets", help="list or show presets")
    pr.add_argument("--presets-dir", default="presets")
    pr.add_argument("--show", metavar="NAME", help="print one preset as JSON")
    pr.set_defaults(func=cmd_presets)

    pb = sub.add_parser("probe", help="inspect an input")
    pb.add_argument("-i", "--input", required=True)
    pb.add_argument("--program", type=int,
                    help="MPTS input: transport stream program to inspect")
    pb.add_argument("--ffmpeg")
    pb.add_argument("--ffprobe")
    pb.set_defaults(func=cmd_probe)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (ValueError, RuntimeError, FFmpegError) as exc:
        print(f"pqloop error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
