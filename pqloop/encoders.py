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

from dataclasses import dataclass


@dataclass(frozen=True)
class ParamSpec:
    name: str
    values: tuple            # ordered candidates (ordinal: worse->better-ish)
    default: object
    emit: str = "kv"         # "kv" (into -x264-params style), "flag:<-ffmpeg-flag>",
                             # or "none" (searched/cached but consumed by rc_kv,
                             # never emitted itself)
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
                 rc_extra=(), rc_kv=None, gop_flags_extra=(), gop_kv_extra=None,
                 gop_kv_key=None, two_pass=None, experimental=False):
        self.name = name
        self.codec = codec
        self.params = dict(params)         # insertion order = screening tiebreak
        self.kv_flag = kv_flag
        self.rc_extra = tuple(rc_extra)
        self.rc_kv = rc_kv                 # (RateControl, effective params) -> kv
                                           # dict (private VBV etc.)
        self.gop_flags_extra = tuple(gop_flags_extra)
        self.gop_kv_extra = dict(gop_kv_extra or {})
        self.gop_kv_key = gop_kv_key       # kv key that receives int(gop_len)
        self.two_pass = two_pass           # None | "flags" (-pass) | "kv" (pass=/stats=)
        self.experimental = experimental   # not validated on real hardware yet

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

    def video_args(self, config, gop_len=None, seg_duration=None, rc=None,
                   extra_kv=None) -> list:
        eff = self.effective(config)
        args = ["-c:v", self.codec]
        kv = {}
        for name, spec in self.params.items():
            if name not in eff:
                continue
            value = eff[name]
            if spec.emit == "kv":
                kv[name] = value
            elif spec.emit != "none":
                args += [spec.emit.split(":", 1)[1], str(value)]
        if rc is not None:
            args += ["-b:v", f"{rc.bitrate_kbps}k",
                     "-maxrate", f"{rc.maxrate_kbps}k",
                     "-bufsize", f"{rc.bufsize_kbps}k"]
            args += list(self.rc_extra)
            if self.rc_kv:
                kv.update(self.rc_kv(rc, eff))
        if gop_len:
            args += ["-g", str(int(gop_len))]
            args += list(self.gop_flags_extra)
            kv.update(self.gop_kv_extra)
            if self.gop_kv_key:
                kv[self.gop_kv_key] = int(gop_len)
            if seg_duration:
                args += ["-force_key_frames", f"expr:gte(t,n_forced*{seg_duration:g})"]
        if extra_kv:
            kv.update(extra_kv)
        if kv:
            if self.kv_flag:
                args += [self.kv_flag, ":".join(f"{k}={v}" for k, v in kv.items())]
            else:
                for k, v in kv.items():
                    args += [f"-{k}", str(v)]
        return args


def codec_family(name) -> str:
    """Rough codec family for an encoder name ("h264", "hevc", "av1", or "")."""
    n = str(name).lower()
    if "265" in n or "hevc" in n:
        return "hevc"
    if "264" in n or "avc" in n:
        return "h264"
    if "av1" in n or "aom" in n:
        return "av1"
    return ""


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
                        gop_flags_extra=("-sc_threshold", "0"),
                        two_pass="flags")


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
    # ffmpeg's libx265 wrapper ignores -pass/-passlogfile; two-pass must go
    # through -x265-params pass=N:stats=FILE, hence two_pass="kv".
    return EncoderSpace("libx265", "libx265", {s.name: s for s in specs},
                        kv_flag="-x265-params",
                        gop_kv_extra={"scenecut": "0"},
                        two_pass="kv")


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


def _svtav1_space() -> EncoderSpace:
    # Verified against SVT-AV1 1.7: all keys parse via -svtav1-params.
    # sharpness/variance-boost are deliberately absent — 1.7 ignores them
    # silently, and a no-op knob would make the optimizer adopt noise.
    # ffmpeg 6.1 ignores -force_key_frames for libsvtav1 (>=7 honors it);
    # with scd=0 the -g cadence already lands keyframes on segment boundaries.
    P = ParamSpec
    specs = [
        P("preset", (12, 11, 10, 9, 8, 7, 6, 5, 4, 3), 8,
          emit="flag:-preset", priority=1, probes=(6,)),
        P("tune", (1, 0), 1, kind="categorical", priority=2, probes=(0,)),
        P("aq-mode", (0, 1, 2), 2, kind="categorical", priority=3, probes=(1,)),
        P("enable-qm", (0, 1), 0, kind="categorical", priority=4, probes=(1,)),
        P("lookahead", (20, 40, 60, 90, 120), 60, priority=5, probes=(120,)),
        P("qm-min", (0, 2, 4, 8), 8, priority=6, probes=(0,),
          requires=(("enable-qm", (1,)),)),
        P("enable-tf", (1, 0), 1, kind="categorical", priority=6, probes=(0,)),
        P("enable-overlays", (0, 1), 0, kind="categorical", priority=7, probes=(1,)),
        # film-grain synthesis usually scores poorly on plain VMAF even when it
        # looks better; expect non-adoption, but one probe is cheap.
        P("film-grain", (0, 4, 8, 15), 0, priority=8, probes=(8,)),
    ]
    return EncoderSpace("libsvtav1", "libsvtav1", {s.name: s for s in specs},
                        kv_flag="-svtav1-params",
                        gop_kv_extra={"scd": "0"})


def _quadra_rc_kv(rc: RateControl, eff: dict) -> dict:
    # Quadra takes VBV through -xcoder-params, not -maxrate/-bufsize (the
    # netint wrapper never reads those): vbvBufferSize is msec of buffer at
    # the target bitrate (max 3000, sets HRD; the true floor is one frame
    # time, 1000/fps but never below 10 ms — fps is unknown here, so only
    # the 10 ms bound is enforced), vbvMaxRate is bits/s and must be >= -b:v.
    # The rcMode pseudo-parameter picks the rate-control mode (guide 12.4)
    # via the zero/non-zero pattern of the two VBV keys.
    mode = eff.get("rcMode", "cvbr")
    if mode == "abr":
        return {"vbvBufferSize": 0, "vbvMaxRate": 0}
    ms = round(rc.bufsize_kbps * 1000 / rc.bitrate_kbps)
    ms = max(10, min(3000, ms))
    if mode == "cbr":
        return {"vbvBufferSize": ms, "vbvMaxRate": 0}
    # cvbr and cappedcrf: VBV buffer plus an explicit peak-rate cap
    return {"vbvBufferSize": ms, "vbvMaxRate": rc.maxrate_kbps * 1000}


def _quadra_space(codec) -> EncoderSpace:
    """EXPERIMENTAL: NETINT Quadra via the netint_ffmpeg fork's -xcoder-params
    (colon-joined key=value; libxcoder matches names case-insensitively).
    Checked against the netint_ffmpeg + libxcoder v5.7 sources and the Quadra
    Integration & Programming Guide v5.7 (summarized with section references
    in docs/quadra_parameter_notes.md) but not yet validated on Quadra
    hardware — check emitted commands with --dry-run before spending encode
    time.

    The rate-control mode is itself searched, via the rcMode pseudo-parameter
    (emit="none": cached/screened/refined like any knob, but consumed by
    _quadra_rc_kv rather than emitted). Modes map to guide section 12.4:
      cvbr       RcEnable=1, vbvBufferSize>0, vbvMaxRate>0  (constrained VBR)
      cbr        RcEnable=1, vbvBufferSize>0, vbvMaxRate=0
      abr        RcEnable=1, vbvBufferSize=0, vbvMaxRate=0
      cappedcrf  crf + bitrate/VBV caps; the guide requires rcEnable=0 in CRF
                 modes, so RcEnable is gated off (hardware default 0 applies)
    CQP and uncapped CRF are excluded: neither honors a bitrate target, so
    they cannot compete under the fixed-budget objective.

    Quadra ignores -g/-maxrate/-bufsize: keyframe cadence comes from
    intraPeriod (tracks the GOP here; valid 0..1024, so seg_duration*fps must
    stay <= 1024) and VBV from vbvBufferSize/vbvMaxRate (emitted via rc_kv).
    -force_key_frames works as-is (guide section 12.6): forced IDRs are
    inserted in addition to the intraPeriod cadence.
    Deliberately not tuned:
      preset              (bundle that just sets the params tuned individually)
      hvsBaseMbComplexity (only active with hvsQPEnable=1; the guide warns it
                           can push bitrate past target at low targets)
      totalCuTreeDepth    (must exceed the *current* lookAheadDepth — a value-
                           relative constraint ParamSpec.requires can't express)
      qcomp/ipRatio/pbRatio/cplxDecay (CRF rate-curve shaping; second-order
                           until cappedcrf proves itself on hardware)
      chromaQpOffset      (VMAF is luma-only — the optimizer would learn to
                           starve chroma to feed luma bits)"""
    P = ParamSpec
    _rc_on = ("rcMode", ("cvbr", "cbr", "abr"))     # modes running RcEnable=1
    _hrd_on = ("rcMode", ("cvbr", "cbr"))           # modes with a VBV buffer
    _la_on = ("lookAheadDepth", (10, 20, 30, 40))
    specs = [
        P("rcMode", ("cvbr", "cbr", "abr", "cappedcrf"), "cvbr", emit="none",
          kind="categorical", priority=1, probes=("abr", "cbr", "cappedcrf")),
        # single-valued: emitted in every RC mode so trials run
        # bitrate-controlled (the hardware default is fixed-QP), never tuned
        P("RcEnable", (1,), 1, priority=1, requires=(_rc_on,)),
        # valid values: 0 (off) or 4..40
        P("lookAheadDepth", (0, 10, 20, 30, 40), 0, priority=1, probes=(20,)),
        # capped-CRF quality target (0-51, lower = better, guide recommends
        # 23); -b:v/vbvMaxRate/vbvBufferSize still cap the rate, so overshoot
        # stays bounded and the objective's penalty handles the rest
        P("crf", (33, 31, 29, 27, 25, 23, 21, 19, 17), 23, priority=2,
          probes=(21,), requires=(("rcMode", ("cappedcrf",)),)),
        # RDO quantization: the guide's headline quality-vs-speed knob (8.1)
        P("enableRdoQuant", (0, 1), 0, kind="categorical", priority=2, probes=(1,)),
        # I-frame QP delta under rate control (guide default -2; negative =
        # spend more on I-frames)
        P("intraQPDelta", (-8, -6, -4, -2, 0, 2), -2, priority=3, probes=(-6,),
          requires=(_rc_on,)),
        # CU-level QP adjustment for subjective quality (AQ analogue)
        P("hvsQPEnable", (0, 1), 0, kind="categorical", priority=3, probes=(1,)),
        P("cuLevelRCEnable", (0, 1), 0, kind="categorical", priority=3, probes=(1,)),
        # 0 = treat -b:v as a maximum, 1 = as the average target; only read
        # when CU-level RC is on without lookahead, and never in CRF modes
        P("bitrateMode", (0, 1), 0, kind="categorical", priority=4, probes=(1,),
          requires=(_rc_on, ("cuLevelRCEnable", (1,)), ("lookAheadDepth", (0,)))),
        # switches the ipRatio I-frame bit boost (default 1.40) on in CBR/VBR
        # (in CRF modes ipRatio is always active, so nothing to enable there)
        P("enableipRatio", (0, 1), 0, kind="categorical", priority=4, probes=(1,),
          requires=(_hrd_on,)),
        # 9=consecutive-P (low delay) 4=IBPBP(gop2) 5=IBBBP(gop4)
        # 8=random-access B-pyramid(gop8) -1=adaptive GOP (hardware default,
        # recommended by the guide for highest quality); all five are also in
        # the CRF-supported subset (guide 8.4 crf notes)
        P("gopPresetIdx", (9, 4, 5, 8, -1), -1, kind="categorical", priority=4,
          probes=(8,)),
        # lookahead-pass refinements: disable MB/CU tree, and make the 2-pass
        # GOP follow gopPresetIdx instead of the encoder's own 2-pass pattern
        P("noMbTree", (0, 1), 0, kind="categorical", priority=5, probes=(1,),
          requires=(_la_on,)),
        P("enable2PassGop", (0, 1), 0, kind="categorical", priority=5, probes=(1,),
          requires=(_la_on,)),
        # filler bits toward true CBR ("cbr" is the deprecated alias on
        # Quadra); HRD modes only — it would silently force vbvBufferSize=3000
        # back on in abr/cappedcrf
        P("fillerEnable", (0, 1), 0, kind="categorical", priority=5, probes=(1,),
          requires=(_hrd_on,)),
        # B-frame subjective tuning (1=medium needs lookahead, 2=high); trades
        # compression efficiency for stability, so expect non-adoption on
        # plain VMAF — one probe is cheap
        P("tuneBframeVisual", (0, 1, 2), 0, kind="categorical", priority=6,
          probes=(1,), requires=(_la_on,)),
    ]
    if codec_family(codec) == "hevc":
        # RDO candidate count is only tunable for HEVC (H.264 supports 1 only)
        specs.insert(5, P("rdoLevel", (1, 2, 3), 1, priority=2, probes=(3,)))
    return EncoderSpace(codec, codec, {s.name: s for s in specs},
                        kv_flag="-xcoder-params",
                        rc_kv=_quadra_rc_kv,
                        gop_kv_key="intraPeriod",
                        experimental=True)


def generic_space(codec) -> EncoderSpace:
    """Fallback for encoders without a curated space. Nothing to tune, but rate
    control, GOP and --extra-video-args still apply."""
    return EncoderSpace(codec, codec, {}, kv_flag=None)


_BUILDERS = {
    "libx264": _x264_space,
    "libx265": _x265_space,
    "libsvtav1": _svtav1_space,
    "h264_nvenc": lambda: _nvenc_space("h264_nvenc"),
    "hevc_nvenc": lambda: _nvenc_space("hevc_nvenc"),
    "h264_ni_quadra_enc": lambda: _quadra_space("h264_ni_quadra_enc"),
    "h265_ni_quadra_enc": lambda: _quadra_space("h265_ni_quadra_enc"),
}


def get_space(encoder) -> EncoderSpace:
    builder = _BUILDERS.get(encoder)
    return builder() if builder else generic_space(encoder)


def known_encoders():
    return sorted(_BUILDERS)
