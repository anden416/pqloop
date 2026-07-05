"""Run statistics: JSONL event log per run plus CSV export for offline analysis."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .util import now_iso


class StatsWriter:
    def __init__(self, stats_dir, run_id):
        self.dir = Path(stats_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.path = self.dir / f"{run_id}.jsonl"
        self._fh = open(self.path, "w")

    def event(self, kind, **payload):
        record = {"ts": now_iso(), "kind": kind}
        record.update(payload)
        self._fh.write(json.dumps(record, sort_keys=False, default=str) + "\n")
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


def read_events(jsonl_path):
    events = []
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _flatten_trial(ev) -> dict:
    row = {}
    for key, value in ev.items():
        if key == "params" and isinstance(value, dict):
            for k, v in value.items():
                row[f"param.{k}"] = v
        elif key == "metrics" and isinstance(value, dict):
            row.update(value)
        else:
            row[key] = value
    return row


def to_csv(jsonl_path, csv_path=None) -> str:
    jsonl_path = Path(jsonl_path)
    csv_path = Path(csv_path) if csv_path else jsonl_path.with_suffix(".csv")
    rows = [_flatten_trial(ev) for ev in read_events(jsonl_path)
            if ev.get("kind") == "trial"]
    header = []
    for row in rows:
        for key in row:
            if key not in header:
                header.append(key)
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(csv_path)


def summarize(jsonl_path) -> str:
    events = read_events(jsonl_path)
    meta = next((e for e in events if e.get("kind") == "meta"), {})
    trials = [e for e in events if e.get("kind") == "trial"]
    done = next((e for e in events if e.get("kind") == "done"), {})
    real = [t for t in trials if not t.get("cached")]
    ok = [t for t in real if t.get("ok")]
    lines = []
    lines.append(f"run:      {meta.get('run_id', Path(jsonl_path).stem)}")
    if meta:
        cfg = meta.get("config", {})
        lines.append(f"encoder:  {cfg.get('encoder')} @ {cfg.get('target_bitrate_kbps')}k"
                     f"  metric {cfg.get('metric')}")
        src = meta.get("source", {})
        mezz = meta.get("mezzanine", {})
        if src:
            lines.append(f"source:   {src.get('path')} {src.get('width')}x{src.get('height')}"
                         f" {src.get('field_order')} {src.get('fps')}fps")
        if mezz:
            lines.append(f"clip:     {mezz.get('duration'):.6g}s @ {mezz.get('fps'):.6g}fps"
                         f" (deinterlaced: {mezz.get('deinterlaced')})")
    lines.append(f"trials:   {len(real)} encodes ({len(trials) - len(real)} cache hits), "
                 f"{sum(t.get('metrics', {}).get('encode_time_s', 0) for t in real):.0f}s encoding")
    baseline = next((t for t in trials if t.get("phase") == "baseline"), None)
    if baseline and baseline.get("ok"):
        lines.append(f"baseline: objective {baseline['objective']:.3f}  "
                     f"(VMAF {baseline.get('metrics', {}).get('vmaf_mean')})")
    if ok:
        best = max(ok, key=lambda t: t.get("objective", float("-inf")))
        m = best.get("metrics", {})
        lines.append(f"best:     objective {best['objective']:.3f}  "
                     f"VMAF mean {m.get('vmaf_mean')} harmonic {m.get('vmaf_harmonic')} "
                     f"p1 {m.get('vmaf_p1')}  @ {m.get('bitrate_kbps')}kbps")
        if baseline and baseline.get("ok"):
            lines.append(f"gain:     +{best['objective'] - baseline['objective']:.3f} objective")
        lines.append("best params: " + json.dumps(best.get("params", {}), sort_keys=True))
        top = sorted(ok, key=lambda t: -t.get("objective", float("-inf")))[:10]
        lines.append("")
        lines.append(f"{'#':>4} {'phase':<8} {'change':<22} {'objective':>9} "
                     f"{'vmaf':>7} {'p1':>7} {'kbps':>7} {'enc_s':>6}")
        for t in top:
            m = t.get("metrics", {})
            lines.append(f"{t.get('n', 0):>4} {t.get('phase', ''):<8} "
                         f"{t.get('label', '')[:22]:<22} {t.get('objective', 0):>9.3f} "
                         f"{m.get('vmaf_mean', 0):>7.2f} {m.get('vmaf_p1', 0):>7.2f} "
                         f"{m.get('bitrate_kbps', 0):>7.0f} "
                         f"{m.get('encode_time_s', 0):>6.1f}")
    if done:
        lines.append("")
        lines.append(f"stop reason: {done.get('stop_reason')}  "
                     f"elapsed {done.get('elapsed_s', 0):.0f}s")
    return "\n".join(lines)
