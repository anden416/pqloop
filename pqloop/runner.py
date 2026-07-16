"""Trial evaluation: encode the mezzanine with candidate parameters, measure
bitrate + VMAF, and produce the search objective (VMAF minus an overshoot
penalty when the encode busts the bitrate target beyond tolerance)."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .encoders import RateControl
from .ffmpeg import FFmpegError
from .optimizer import TrialOutcome, NEG_INF
from . import vmaf
from .util import atomic_write_json, load_json


@dataclass
class RunConfig:
    encoder: str = "libx264"
    target_bitrate_kbps: int = 0
    maxrate_ratio: float = 1.10
    bufsize_ratio: float = 2.0
    bitrate_tolerance: float = 0.05     # fraction over target tolerated
    overshoot_penalty: float = 1.0      # objective points per % beyond tolerance
    undershoot_penalty: float = 0.0     # objective points per % under target beyond tolerance
    seg_duration: float = 4.0
    gop_duration: float = None           # None = lock GOP to the segment
    pix_fmt: str = "yuv420p"
    scale: str = ""                     # "WxH" or empty
    metric: str = "mean"
    vmaf_model: str = ""
    vmaf_subsample: int = 1
    vmaf_threads: int = 0
    two_pass: bool = False
    extra_video_args: list = field(default_factory=list)
    keep_trials: bool = False

    @property
    def metric_key(self) -> str:
        return vmaf.METRIC_KEYS[self.metric]


class TrialRunner:
    def __init__(self, cfg: RunConfig, ff_enc, ff_meas, space, mezz,
                 workdir, log=None, best_objective=NEG_INF):
        self.cfg = cfg
        self.ff_enc = ff_enc
        self.ff_meas = ff_meas
        self.space = space
        self.mezz = mezz
        self.workdir = Path(workdir)
        self.trials_dir = self.workdir / "trials"
        self.log = log or (lambda m: None)
        # With --keep-trials a resumed run must not overwrite t0001.* from the
        # previous process, so continue numbering after existing artifacts.
        self.counter = 0
        if cfg.keep_trials and self.trials_dir.is_dir():
            found = [re.match(r"t(\d+)\.", p.name)
                     for p in self.trials_dir.iterdir()]
            self.counter = max((int(m.group(1)) for m in found if m), default=0)
        # Seeded from the optimizer's persisted, constraint-eligible best on a
        # resume. Otherwise the first new (possibly inferior) trial would
        # overwrite best_trial.* merely because it is new to this process.
        self.best_objective = best_objective
        self.rc = RateControl(
            bitrate_kbps=int(cfg.target_bitrate_kbps),
            maxrate_kbps=int(round(cfg.target_bitrate_kbps * cfg.maxrate_ratio)),
            bufsize_kbps=int(round(cfg.target_bitrate_kbps * cfg.bufsize_ratio)),
        )
        gop_dur = cfg.gop_duration or cfg.seg_duration
        self.gop_len = max(1, int(round(gop_dur * mezz.fps)))
        self._two_pass = space.two_pass if cfg.two_pass else None
        if cfg.two_pass and not self._two_pass:
            self.log(f"note: --two-pass not supported for {space.name}; "
                     f"using single pass")

    # ---- command construction ------------------------------------------------

    def encode_args(self, params, out_path, pass_num=0, passlog=None) -> list:
        args = ["-y", "-i", self.mezz.path]
        if self.cfg.scale:
            w, h = self.cfg.scale.lower().split("x", 1)
            args += ["-vf", f"scale={int(w)}:{int(h)}:flags=lanczos"]
        args += self.space.video_args(params, gop_len=self.gop_len,
                                      seg_duration=self.cfg.seg_duration,
                                      rc=self.rc, pass_num=pass_num,
                                      passlog=passlog)
        args += ["-pix_fmt", self.cfg.pix_fmt]
        args += list(self.cfg.extra_video_args)
        args += ["-an", "-sn", "-dn"]
        if pass_num == 1:
            args += ["-f", "null", "-"]
        else:
            args += ["-f", "mp4", str(out_path)]
        return args

    # ---- evaluation ------------------------------------------------------------

    def evaluate(self, params, label) -> TrialOutcome:
        self.trials_dir.mkdir(parents=True, exist_ok=True)
        self.counter += 1
        n = self.counter
        out_path = self.trials_dir / f"t{n:04d}.mp4"
        vmaf_log = self.trials_dir / f"t{n:04d}.vmaf.json"
        encode_timeout = max(900.0, self.mezz.duration * 120 + 300)
        started = time.monotonic()
        try:
            if self._two_pass:
                passlog = self.trials_dir / f"t{n:04d}.passlog"
                self.ff_enc.run(self.encode_args(params, out_path, 1, passlog),
                                timeout=encode_timeout)
                self.ff_enc.run(self.encode_args(params, out_path, 2, passlog),
                                timeout=encode_timeout)
                for leftover in self.trials_dir.glob(f"t{n:04d}.passlog*"):
                    leftover.unlink(missing_ok=True)
            else:
                self.ff_enc.run(self.encode_args(params, out_path),
                                timeout=encode_timeout)
            encode_time = time.monotonic() - started

            probe_started = time.monotonic()
            probe = self.ff_enc.probe(out_path)
            fmt = probe.get("format", {})
            size = int(fmt.get("size") or out_path.stat().st_size)
            duration = float(fmt.get("duration") or self.mezz.duration or 1.0)
            bitrate_kbps = size * 8.0 / duration / 1000.0
            probe_time = time.monotonic() - probe_started

            vmaf_started = time.monotonic()
            scores = vmaf.measure(
                self.ff_meas, str(out_path), self.mezz.path,
                self.mezz.width, self.mezz.height, str(vmaf_log),
                threads=self.cfg.vmaf_threads, subsample=self.cfg.vmaf_subsample,
                model=self.cfg.vmaf_model or None,
                timeout=max(900.0, self.mezz.duration * 60 + 300))
            vmaf_time = time.monotonic() - vmaf_started

            raw = scores[self.cfg.metric_key]
            target = float(self.cfg.target_bitrate_kbps)
            over_pct = max(0.0, (bitrate_kbps / target - 1.0) * 100.0) if target else 0.0
            under_pct = max(0.0, (1.0 - bitrate_kbps / target) * 100.0) if target else 0.0
            tolerance_pct = self.cfg.bitrate_tolerance * 100.0
            penalty = (self.cfg.overshoot_penalty * max(0.0, over_pct - tolerance_pct)
                       + self.cfg.undershoot_penalty * max(0.0, under_pct - tolerance_pct))
            objective = raw - penalty

            metrics = dict(scores)
            metrics.update({
                "bitrate_kbps": round(bitrate_kbps, 1),
                "size_bytes": size,
                "encode_time_s": round(encode_time, 2),
                "probe_time_s": round(probe_time, 3),
                "vmaf_time_s": round(vmaf_time, 2),
                "trial_time_s": round(time.monotonic() - started, 2),
                "encode_fps": round((self.mezz.duration * self.mezz.fps) / encode_time, 1)
                              if encode_time > 0 else 0.0,
                "over_target_pct": round(over_pct, 2),
                "under_target_pct": round(under_pct, 2),
                "penalty": round(penalty, 3),
                "objective": round(objective, 4),
            })
            outcome = TrialOutcome(ok=True, objective=objective, metrics=metrics)
        except (FFmpegError, RuntimeError, OSError, KeyError, ValueError) as exc:
            self._cleanup(out_path, vmaf_log,
                          *self.trials_dir.glob(f"t{n:04d}.passlog*"))
            return TrialOutcome(ok=False, objective=NEG_INF, error=str(exc))

        self._retain_or_drop(out_path, vmaf_log, params, outcome)
        return outcome

    # ---- housekeeping ------------------------------------------------------------

    def reconcile_best_artifacts(self, params, objective) -> None:
        """Remove a stale visual-inspection artifact after constraints/reset."""
        paths = [self.workdir / "best_trial.mp4",
                 self.workdir / "best_trial.vmaf.json",
                 self.workdir / "best_trial.json"]
        if not any(p.exists() for p in paths):
            return
        matches = False
        try:
            meta = load_json(paths[-1])
            recorded_obj = meta.get("objective")
            if recorded_obj is None:
                recorded_obj = (meta.get("metrics") or {}).get("objective")
            matches = (params is not None and meta.get("params") == params
                       and recorded_obj is not None
                       and abs(float(recorded_obj) - float(objective)) < 1e-6)
        except (OSError, ValueError, TypeError):
            matches = False
        if not matches:
            self._cleanup(*paths)
            self.log("note: removed stale best_trial artifacts; the next "
                     "improving real trial will recreate them")

    def _retain_or_drop(self, out_path, vmaf_log, params, outcome):
        if outcome.objective > self.best_objective:
            self.best_objective = outcome.objective
            best_video = self.workdir / "best_trial.mp4"
            best_log = self.workdir / "best_trial.vmaf.json"
            try:
                shutil.copy2(out_path, best_video) if self.cfg.keep_trials \
                    else out_path.replace(best_video)
                shutil.copy2(vmaf_log, best_log) if self.cfg.keep_trials \
                    else vmaf_log.replace(best_log)
                atomic_write_json(self.workdir / "best_trial.json",
                                  {"params": params, "objective": outcome.objective,
                                   "metrics": outcome.metrics})
            except OSError:
                pass
        if not self.cfg.keep_trials:
            self._cleanup(out_path, vmaf_log)

    @staticmethod
    def _cleanup(*paths):
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
