"""Encoder parameter spaces and ffmpeg argument emission.

Each encoder gets an EncoderSpace: an ordered set of ParamSpecs describing the
tunable knobs, their candidate values (ordered for hill-climbing), an expected
impact priority (1 = screen first), screening probe values, and dependency
rules ("merange only matters when me=umh").

The space also knows how to turn a parameter dict plus rate-control/GOP
settings into concrete ffmpeg arguments, merging private options into a single
-x264-params / -x265-params flag where required.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParamSpec:
    name: str
    values: tuple            # ordered candidates (ordinal: worse->better-ish)
    default: object
    emit: str = "kv"         # "kv" (into -x264-params style) or "flag:<-ffmpeg-flag>"
    kind: str = "ordinal"    # "ordinal" (hill-climb neighbors) | "categorical" (try all)
    priority: int = 5        # 1 = highest expected VMAF impact, screened first
    probes: tuple = ()       # values tried during the screening phase
    requires: tuple = ()     # ((other_param, (allowed values...)), ...)


@dataclass
class RateControl:
    bitrate_kbps: int
    maxrate_kbps: int
    bufsize_kbps: int


class EncoderSpace:
    def __init__(self, name, codec, params, kv_flag=None,
                 rc_extra=(), gop_flags_extra=(), gop_kv_extra=None):
        self.name = name
        self.codec = codec
        self.params = dict(params)         # insertion order = screening tiebreak
        self.kv_flag = kv_flag
        self.rc_extra = tuple(rc_extra)
        self.gop_flags_extra = tuple(gop_flags_extra)
        self.gop_kv_extra = dict(gop_kv_extra or {})

    # ---- parameter space queries -------------------------------------------

    def defaults(self) -> dict:
        return {n: s.default for n, s in self.params.items()}

    def tunable(self, include=None, exclude=None, frozen=()) -> list:
        """Specs eligible for optimization, sorted by (priority, definition order)."""
        include = set(include) if include else None
        exclude = set(exclude or ())
        frozen = set(frozen or ())
        for group in (include or ()), exclude:
            unknown = set(group or ()) - set(self.params)
            if unknown:
                raise ValueError(
                    f"unknown parameter(s) for {self.name}: {', '.join(sorted(unknown))}"
                    f" (available: {', '.join(self.params)})")
        order = {n: i for i, n in enumerate(self.params)}
        specs = [s for n, s in self.params.items()
                 if len(s.values) > 1
                 and (include is None or n in include)
                 and n not in exclude and n not in frozen]
        return sorted(specs, key=lambda s: (s.priority, order[s.name]))

    def active(self, config, name) -> bool:
        """Whether a parameter has any effect under the given configuration."""
        spec = self.params[name]
        return all(config.get(dep) in allowed for dep, allowed in spec.requires)

    def candidate_valid(self, config, spec) -> bool:
        return self.active(config, spec.name)

    def effective(self, config) -> dict:
        """The parameters that actually shape the encode: known, active, non-None.
        Cache signatures use this, so configs differing only in inert values
        (e.g. merange while me=hex) count as the same encode."""
        return {n: v for n, v in config.items()
                if n in self.params and v is not None and self.active(config, n)}

    # ---- ffmpeg argument emission ------------------------------------------

    def video_args(self, config, gop_len=None, seg_duration=None, rc=None) -> list:
        eff = self.effective(config)
        args = ["-c:v", self.codec]
        kv = {}
        for name, spec in self.params.items():
            if name not in eff:
                continue
            value = eff[name]
            if spec.emit == "kv":
                kv[name] = value
            else:
                args += [spec.emit.split(":", 1)[1], str(value)]
        if rc is not None:
            args += ["-b:v", f"{rc.bitrate_kbps}k",
                     "-maxrate", f"{rc.maxrate_kbps}k",
                     "-bufsize", f"{rc.bufsize_kbps}k"]
            args += list(self.rc_extra)
        if gop_len:
            args += ["-g", str(int(gop_len))]
            args += list(self.gop_flags_extra)
            kv.update(self.gop_kv_extra)
            if seg_duration:
                args += ["-force_key_frames", f"expr:gte(t,n_forced*{seg_duration:g})"]
        if kv:
            if self.kv_flag:
                args += [self.kv_flag, ":".join(f"{k}={v}" for k, v in kv.items())]
            else:
                for k, v in kv.items():
                    args += [f"-{k}", str(v)]
        return args


# ---- concrete spaces ---------------------------------------------------------

def _x264_space() -> EncoderSpace:
    P = ParamSpec
    _bf_on = ("bframes", (1, 2, 3, 4, 5, 6, 8))
    specs = [
        P("preset", ("ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"),
          "medium", emit="flag:-preset", priority=1, probes=("slow",)),
        P("psy-rd", (0.0, 0.3, 0.6, 1.0), 1.0, priority=2, probes=(0.0,)),
        P("aq-mode", (0, 1, 2, 3), 1, kind="categorical", priority=3, probes=(3,)),
        P("subme", (5, 6, 7, 8, 9, 10, 11), 7, priority=4, probes=(9,)),
        P("aq-strength", (0.5, 0.7, 0.85, 1.0, 1.2, 1.4), 1.0, priority=5, probes=(0.7,)),
        P("rc-lookahead", (20, 30, 40, 50, 60, 70), 40, priority=6, probes=(60,)),
        P("bframes", (0, 1, 2, 3, 4, 5, 6, 8), 3, priority=6, probes=(5,)),
        P("qcomp", (0.5, 0.6, 0.7, 0.8), 0.6, priority=7, probes=(0.7,)),
        P("refs", (1, 2, 3, 4, 5, 6), 3, priority=7, probes=(5,)),
        P("me", ("dia", "hex", "umh"), "hex", kind="categorical", priority=8,
          probes=("umh",)),
        P("trellis", (0, 1, 2), 1, priority=8, probes=(2,)),
        P("deblock", (-3, -2, -1, 0, 1), 0, priority=9, probes=(-1,)),
        P("merange", (16, 24, 32, 48), 16, priority=9, probes=(32,),
          requires=(("me", ("umh",)),)),
        P("b-adapt", (0, 1, 2), 1, priority=9, probes=(2,), requires=(_bf_on,)),
        P("direct", ("none", "spatial", "auto"), "spatial", kind="categorical",
          priority=10, probes=("auto",), requires=(_bf_on,)),
        P("tune", (None, "film", "grain", "animation"), None, emit="flag:-tune",
          kind="categorical", priority=10, probes=("film",)),
    ]
    return EncoderSpace("libx264", "libx264", {s.name: s for s in specs},
                        kv_flag="-x264-params",
                        gop_flags_extra=("-sc_threshold", "0"))


def _x265_space() -> EncoderSpace:
    P = ParamSpec
    specs = [
        P("preset", ("ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower"),
          "medium", emit="flag:-preset", priority=1, probes=("slow",)),
        P("psy-rd", (0.0, 0.5, 1.0, 1.5, 2.0, 3.0), 2.0, priority=2, probes=(0.0,)),
        P("aq-mode", (0, 1, 2, 3, 4), 2, kind="categorical", priority=3, probes=(3,)),
        P("aq-strength", (0.5, 0.7, 0.85, 1.0, 1.2), 1.0, priority=4, probes=(0.7,)),
        P("rc-lookahead", (20, 30, 40, 60), 20, priority=5, probes=(40,)),
        P("psy-rdoq", (0.0, 1.0, 2.0, 5.0), 0.0, priority=6, probes=(1.0,)),
        P("bframes", (3, 4, 5, 8), 4, priority=7, probes=(8,)),
        P("sao", (0, 1), 1, kind="categorical", priority=7, probes=(0,)),
        P("rd", (2, 3, 4), 3, priority=8, probes=(4,)),
        P("ref", (2, 3, 4, 5), 3, priority=8, probes=(4,)),
        P("qcomp", (0.5, 0.6, 0.7), 0.6, priority=9, probes=(0.7,)),
        P("ctu", (32, 64), 64, kind="categorical", priority=10, probes=(32,)),
    ]
    return EncoderSpace("libx265", "libx265", {s.name: s for s in specs},
                        kv_flag="-x265-params",
                        gop_kv_extra={"scenecut": "0"})


def _nvenc_space(codec) -> EncoderSpace:
    P = ParamSpec
    specs = [
        P("preset", ("p1", "p2", "p3", "p4", "p5", "p6", "p7"), "p4",
          emit="flag:-preset", priority=1, probes=("p6",)),
        P("multipass", ("disabled", "qres", "fullres"), "disabled",
          emit="flag:-multipass", kind="categorical", priority=2, probes=("fullres",)),
        P("spatial-aq", (0, 1), 0, emit="flag:-spatial-aq", kind="categorical",
          priority=3, probes=(1,)),
        P("rc-lookahead", (0, 8, 20, 32, 48), 20, emit="flag:-rc-lookahead",
          priority=4, probes=(32,)),
        P("temporal-aq", (0, 1), 0, emit="flag:-temporal-aq", kind="categorical",
          priority=5, probes=(1,), requires=(("rc-lookahead", (8, 20, 32, 48)),)),
        P("aq-strength", (4, 6, 8, 10, 12, 15), 8, emit="flag:-aq-strength",
          priority=6, probes=(12,), requires=(("spatial-aq", (1,)),)),
        P("bf", (0, 1, 2, 3, 4), 3, emit="flag:-bf", priority=7, probes=(4,)),
        P("b_ref_mode", ("disabled", "middle", "each"), "disabled",
          emit="flag:-b_ref_mode", kind="categorical", priority=7,
          probes=("middle",), requires=(("bf", (2, 3, 4)),)),
    ]
    return EncoderSpace(codec, codec, {s.name: s for s in specs},
                        kv_flag=None,
                        rc_extra=("-rc", "vbr"),
                        gop_flags_extra=("-no-scenecut", "1", "-forced-idr", "1"))


def generic_space(codec) -> EncoderSpace:
    """Fallback for encoders without a curated space (e.g. netint h264_ni_quadra_enc).
    Nothing to tune yet, but rate control, GOP and --extra-video-args still apply."""
    return EncoderSpace(codec, codec, {}, kv_flag=None)


_BUILDERS = {
    "libx264": _x264_space,
    "libx265": _x265_space,
    "h264_nvenc": lambda: _nvenc_space("h264_nvenc"),
    "hevc_nvenc": lambda: _nvenc_space("hevc_nvenc"),
}


def get_space(encoder) -> EncoderSpace:
    builder = _BUILDERS.get(encoder)
    return builder() if builder else generic_space(encoder)


def known_encoders():
    return sorted(_BUILDERS)
