"""Saveable / resumable presets.

A preset is a JSON file carrying the encode configuration, the optimizer state
(current point, sensitivities, full trial cache) and the best result found so
far. Re-running `pqloop optimize` with the same preset resumes the search:
the deterministic walk replays against the cache and continues from new
ground. If the reference clip changed (different content/window/filters), the
scores are no longer comparable — the cache is reset while the best-known
parameters and sensitivity ordering carry over as the starting point.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .util import now_iso, atomic_write_json


def resolve(name_or_path, presets_dir) -> Path:
    s = str(name_or_path)
    if s.endswith(".json") or "/" in s or "\\" in s:
        return Path(s)
    return Path(presets_dir) / f"{s}.json"


def fresh(name) -> dict:
    return {
        "pqloop_version": __version__,
        "name": name,
        "created": now_iso(),
        "updated": now_iso(),
        "config": {},
        "fingerprint": None,
        "optimizer": {},
        "best": {},
        "runs": [],
    }


def load(path) -> dict:
    path = Path(path)
    if not path.exists():
        return fresh(path.stem)
    with open(path) as fh:
        data = json.load(fh)
    data.setdefault("name", path.stem)
    for key, default in (("config", {}), ("optimizer", {}), ("best", {}),
                         ("runs", []), ("fingerprint", None)):
        data.setdefault(key, default)
    return data


def save(path, data) -> None:
    data["updated"] = now_iso()
    data["pqloop_version"] = __version__
    atomic_write_json(path, data)


def list_presets(presets_dir):
    d = Path(presets_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = load(p)
        except (json.JSONDecodeError, OSError):
            continue
        best = data.get("best") or {}
        cfg = data.get("config") or {}
        out.append({
            "name": data.get("name", p.stem),
            "path": str(p),
            "encoder": cfg.get("encoder", "?"),
            "target_kbps": cfg.get("target_bitrate_kbps"),
            "best_objective": best.get("objective"),
            "encodes": (data.get("optimizer") or {}).get("encodes", 0),
            "updated": data.get("updated", ""),
        })
    return out
