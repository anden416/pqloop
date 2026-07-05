"""Sensitivity-guided greedy search over an encoder parameter space.

Strategy ("clever, not random"):

1. Baseline: evaluate the current/default configuration.
2. Screening: for each tunable parameter in expected-impact order, evaluate its
   probe value(s) one-at-a-time to *measure* how much VMAF each knob moves.
   Improvements are adopted greedily as they are found.
3. Refinement passes: parameters ordered by measured sensitivity; each is
   hill-climbed through its ordered candidate values (categoricals try all
   values). Adoptions bump a config version; a parameter is re-examined in a
   later pass only if something else changed since it was last tried.
4. Stop when a full pass gains less than min_pass_gain (diminishing returns),
   or on trial/time/target budgets.

Every evaluated configuration is cached by its *effective* parameter signature.
The search is deterministic given its state, so resuming simply replays the
walk with cache hits until it reaches new ground — no bookkeeping of "where we
were" is needed, and nothing is re-encoded.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

NEG_INF = float("-inf")


@dataclass
class TrialOutcome:
    ok: bool
    objective: float
    metrics: dict = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict:
        return {"ok": self.ok,
                "objective": None if self.objective == NEG_INF else self.objective,
                "metrics": self.metrics, "error": self.error}

    @classmethod
    def from_json(cls, d) -> "TrialOutcome":
        obj = d.get("objective")
        return cls(ok=bool(d.get("ok")),
                   objective=NEG_INF if obj is None else float(obj),
                   metrics=d.get("metrics") or {}, error=d.get("error") or "")


class StopSearch(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Settings:
    min_pass_gain: float = 0.2    # VMAF points a full pass must gain to continue
    adopt_eps: float = 0.05       # minimum improvement to adopt a change
    max_trials: int = 0           # real encodes this run; 0 = unlimited
    max_seconds: float = 0.0      # wall clock this run; 0 = unlimited
    target_score: float = 0.0     # stop once best objective reaches this; 0 = off
    max_passes: int = 6
    screen: bool = True


class Optimizer:
    def __init__(self, space, evaluate, settings, state=None,
                 include=None, exclude=None, frozen=None,
                 on_trial=None, log=None):
        self.space = space
        self.evaluate = evaluate
        self.s = settings
        self.frozen = dict(frozen or {})
        self.on_trial = on_trial or (lambda **kw: None)
        self.log = log or (lambda msg: None)

        st = state or {}
        params = space.defaults()
        params.update(st.get("current") or {})
        params.update(self.frozen)
        self.current = params
        self.cache = {k: TrialOutcome.from_json(v)
                      for k, v in (st.get("cache") or {}).items()}
        self.sens = {k: float(v) for k, v in (st.get("sens") or {}).items()}
        best = st.get("best") or {}
        self.best_params = best.get("params")
        self.best_objective = best.get("objective")
        self.best_objective = NEG_INF if self.best_objective is None else float(self.best_objective)
        self.best_metrics = best.get("metrics") or {}
        self.screened = bool(st.get("screened"))
        self.passes_done = int(st.get("passes_done") or 0)
        self.total_encodes = int(st.get("encodes") or 0)

        self.tunables = space.tunable(include, exclude, frozen=self.frozen.keys())
        self.stop_reason = None
        self.run_encodes = 0
        self.cur_obj = NEG_INF
        self._version = 0            # bumped on every adoption (session-local)
        self._last_ver = {}
        self._t0 = None

    # ---- persistence ---------------------------------------------------------

    def state(self) -> dict:
        return {
            "current": self.current,
            "cache": {k: v.to_json() for k, v in self.cache.items()},
            "sens": {k: round(v, 4) for k, v in self.sens.items()},
            "best": {"params": self.best_params,
                     "objective": None if self.best_objective == NEG_INF else self.best_objective,
                     "metrics": self.best_metrics},
            "screened": self.screened,
            "passes_done": self.passes_done,
            "encodes": self.total_encodes,
        }

    # ---- evaluation ----------------------------------------------------------

    def signature(self, params) -> str:
        return json.dumps(self.space.effective(params), sort_keys=True, default=str)

    def _eval(self, params, phase, label) -> TrialOutcome:
        sig = self.signature(params)
        cached = sig in self.cache
        if cached:
            outcome = self.cache[sig]
        else:
            if self.s.max_trials and self.run_encodes >= self.s.max_trials:
                raise StopSearch("max_trials")
            if self.s.max_seconds and time.monotonic() - self._t0 >= self.s.max_seconds:
                raise StopSearch("time_limit")
            outcome = self.evaluate(dict(self.space.effective(params)), label)
            self.cache[sig] = outcome
            self.run_encodes += 1
            self.total_encodes += 1
        if outcome.ok and outcome.objective > self.best_objective:
            self.best_objective = outcome.objective
            self.best_params = dict(self.space.effective(params))
            self.best_metrics = dict(outcome.metrics)
        self.on_trial(phase=phase, label=label, params=params, outcome=outcome,
                      cached=cached, best=self.best_objective,
                      encodes=self.run_encodes)
        if (self.s.target_score and outcome.ok
                and self.best_objective >= self.s.target_score):
            raise StopSearch("target_score")
        return outcome

    def _adopt(self, params, objective):
        self.current = dict(params)
        self.cur_obj = objective
        self._version += 1

    # ---- phases ----------------------------------------------------------------

    def run(self) -> str:
        self._t0 = time.monotonic()
        try:
            baseline = self._eval(self.current, "baseline", "baseline")
            if not baseline.ok:
                raise StopSearch(f"baseline_failed: {baseline.error}")
            self.cur_obj = baseline.objective
            if self.s.screen and not self.screened:
                self.log("— screening: measuring parameter impact —")
                self._screen()
                self.screened = True
            self.stop_reason = self._refine()
        except StopSearch as exc:
            self.stop_reason = exc.reason
        except KeyboardInterrupt:
            self.stop_reason = "interrupted"
            raise
        return self.stop_reason

    def _screen(self):
        for spec in self.tunables:
            # Already measured in a previous (interrupted) run: keep that
            # sensitivity and the adoption it led to, don't re-probe.
            if spec.name in self.sens:
                continue
            best_value, best_obj = None, self.cur_obj
            for value in spec.probes:
                if value == self.current.get(spec.name):
                    continue
                cand = dict(self.current)
                cand[spec.name] = value
                if not self.space.candidate_valid(cand, spec):
                    continue
                outcome = self._eval(cand, "screen", f"{spec.name}={value}")
                if outcome.ok and outcome.objective > best_obj:
                    best_obj, best_value = outcome.objective, value
            gain = best_obj - self.cur_obj
            self.sens[spec.name] = max(gain, 0.0)
            if best_value is not None and gain > self.s.adopt_eps:
                cand = dict(self.current)
                cand[spec.name] = best_value
                self._adopt(cand, best_obj)
                self.log(f"  adopted {spec.name}={best_value} (+{gain:.2f})")

    def _refine(self) -> str:
        while self.s.max_passes == 0 or self.passes_done < self.s.max_passes:
            order = sorted(self.tunables,
                           key=lambda s: (-self.sens.get(s.name, 0.0), s.priority))
            self.log(f"— refine pass {self.passes_done + 1} "
                     f"(impact order: {', '.join(s.name for s in order[:6])}"
                     f"{', ...' if len(order) > 6 else ''}) —")
            pass_gain, attempted = 0.0, 0
            for spec in order:
                # Skip only if this param was already refined and nothing else
                # has been adopted since (its local neighborhood is unchanged).
                if self._last_ver.get(spec.name) == self._version:
                    continue
                attempted += 1
                gain = self._refine_param(spec)
                self._last_ver[spec.name] = self._version
                pass_gain += gain
                self.sens[spec.name] = 0.5 * self.sens.get(spec.name, 0.0) + gain
            self.passes_done += 1
            self.log(f"pass {self.passes_done} gain: +{pass_gain:.2f} VMAF")
            if attempted == 0:
                return "converged"
            if pass_gain < self.s.min_pass_gain:
                return "diminishing_returns"
        return "max_passes"

    def _refine_param(self, spec) -> float:
        name = spec.name
        start_obj = self.cur_obj
        if not self.space.active(self.current, name):
            return 0.0
        if spec.kind == "categorical":
            best_value, best_obj = None, self.cur_obj
            for value in spec.values:
                if value == self.current.get(name):
                    continue
                cand = dict(self.current)
                cand[name] = value
                if not self.space.candidate_valid(cand, spec):
                    continue
                outcome = self._eval(cand, "refine", f"{name}={value}")
                if outcome.ok and outcome.objective > best_obj:
                    best_obj, best_value = outcome.objective, value
            if best_value is not None and best_obj - self.cur_obj > self.s.adopt_eps:
                cand = dict(self.current)
                cand[name] = best_value
                self._adopt(cand, best_obj)
        else:
            try:
                idx = spec.values.index(self.current.get(name))
            except ValueError:
                return 0.0
            for direction in (1, -1):
                nxt = idx + direction
                while 0 <= nxt < len(spec.values):
                    cand = dict(self.current)
                    cand[name] = spec.values[nxt]
                    if not self.space.candidate_valid(cand, spec):
                        break
                    outcome = self._eval(cand, "refine", f"{name}={spec.values[nxt]}")
                    if outcome.ok and outcome.objective > self.cur_obj + self.s.adopt_eps:
                        self._adopt(cand, outcome.objective)
                        idx = nxt
                        nxt += direction
                    else:
                        break
        return max(0.0, self.cur_obj - start_obj)
