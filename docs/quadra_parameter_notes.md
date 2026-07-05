# NETINT Quadra parameter notes

Project-owned summary of the vendor documentation that pqloop's Quadra
encoder space (`_quadra_space` / `_quadra_rc_kv` in `pqloop/encoders.py`)
relies on. It exists so the parameter choices can be reviewed without the
vendor manual at hand.

**Source:** NETINT *Quadra Integration & Programming Guide*, V5.7
(© NETINT Technologies Inc., all rights reserved). The guide itself is not
redistributable and is therefore not included in this repository — obtain it
from NETINT (docs.netint.com or your NETINT support contact). Section numbers
below refer to that document. Everything here was additionally cross-checked
against the netint_ffmpeg fork and libxcoder v5.7 sources. None of it has
been validated on Quadra hardware yet — inspect emitted commands with
`--dry-run` first.

## Parameter transport

- Encoder options travel in a single `-xcoder-params` flag as colon-joined
  `key=value` pairs (guide §7/§8). libxcoder matches key names
  case-insensitively (libxcoder source).
- The netint ffmpeg wrapper does not read `-g`, `-maxrate`, or `-bufsize`.
  Keyframe cadence is set with `intraPeriod` (valid 0–1024, §8.4) and VBV
  with `vbvBufferSize`/`vbvMaxRate` (§12.4). pqloop derives `intraPeriod`
  from `--seg-duration` × fps, which must therefore stay ≤ 1024.

## Rate control (§12.4)

- The hardware default is fixed-QP; `RcEnable=1` turns rate control on.
  CRF-based modes require `rcEnable=0` (the hardware default), which is why
  pqloop gates `RcEnable` off in `cappedcrf`.
- The rate-control *mode* is selected by the zero/non-zero pattern of the two
  VBV keys, which pqloop exposes as the searchable `rcMode` pseudo-parameter:

  | pqloop `rcMode` | RcEnable | vbvBufferSize | vbvMaxRate | meaning |
  |---|---|---|---|---|
  | `cvbr` | 1 | > 0 | > 0 | constrained VBR (VBV buffer + peak cap) |
  | `cbr` | 1 | > 0 | 0 | CBR-style (VBV buffer, no explicit peak) |
  | `abr` | 1 | 0 | 0 | average bitrate only, no HRD |
  | `cappedcrf` | 0 (default) | > 0 | > 0 | `crf` quality target + bitrate/VBV caps |

- `vbvBufferSize` is expressed in milliseconds of buffer at the target
  bitrate, maximum 3000; a non-zero value also enables HRD signalling. The
  effective floor is one frame time (1000/fps), never below 10 ms. pqloop
  computes it from `--bufsize-ratio` and clamps to [10, 3000] (fps is not
  known at emission time, so only the 10 ms bound is enforced).
- `vbvMaxRate` is in bits/s and must be ≥ the `-b:v` target. pqloop derives
  it from `--maxrate-ratio`.
- CQP and uncapped CRF honor no bitrate target at all, so they are excluded
  from the search: they cannot compete under pqloop's fixed-budget objective.
- `crf` ranges 0–51, lower is better quality; the guide's recommended
  starting point is 23 (§8.4). In `cappedcrf` the `-b:v`/VBV caps still bound
  the rate, so overshoot stays limited.
- `bitrateMode`: 0 treats `-b:v` as a maximum, 1 as the average target. It is
  only read when CU-level rate control is on *without* lookahead, and never
  in CRF modes (§8.4) — mirrored by the `requires` gating.
- `enableipRatio` switches the I-frame bit boost `ipRatio` (default 1.40) on
  in CBR/VBR modes; in CRF modes `ipRatio` is always active, so there is
  nothing to enable there (§8.4).
- `fillerEnable` pads toward true CBR and is meaningful in HRD modes only;
  in non-HRD modes the hardware silently forces `vbvBufferSize=3000` back on,
  so pqloop gates it to `cvbr`/`cbr`.

## Keyframes and IDR forcing (§12.6)

- ffmpeg's `-force_key_frames` works with the netint wrapper: forced IDRs are
  inserted *in addition to* the `intraPeriod` cadence. pqloop uses this to pin
  IDRs on segment boundaries, same as its software encoders.

## Quality knobs the space tunes

| parameter | guide | facts relied on |
|---|---|---|
| `lookAheadDepth` | §8.4 | valid 0 (off) or 4–40; enables the 2-pass lookahead path |
| `enableRdoQuant` | §8.1/§8.3 | RDO quantization, the guide's headline quality-vs-speed trade |
| `rdoLevel` | §8.4 | RDO candidate count 1–3; HEVC only (H.264 supports 1) |
| `intraQPDelta` | §8.4 | I-frame QP offset under rate control, default −2 (negative = more I-frame bits) |
| `hvsQPEnable` | §8.2 | CU-level QP adjustment for subjective quality (AQ analogue) |
| `cuLevelRCEnable` | §8.2 | CU-level rate control |
| `gopPresetIdx` | §8.4 | 9 = consecutive-P (low delay), 4 = IBPBP (gop 2), 5 = IBBBP (gop 4), 8 = random-access B-pyramid (gop 8), −1 = adaptive GOP (hardware default, recommended for highest quality); these five are also in the CRF-supported subset |
| `noMbTree` | §8.4 | disables MB/CU-tree in the lookahead pass (lookahead only) |
| `enable2PassGop` | §8.4 | makes the 2-pass GOP follow `gopPresetIdx` instead of the encoder's own pattern (lookahead only) |
| `tuneBframeVisual` | §8.4 | B-frame subjective tuning; 1 = medium (needs lookahead), 2 = high |

## Deliberately not tuned, and why

- `preset` — a bundle that just sets the parameters pqloop already tunes
  individually.
- `hvsBaseMbComplexity` — only active with `hvsQPEnable=1`, and the guide
  warns it can push bitrate past target at low targets.
- `totalCuTreeDepth` — must exceed the *current* `lookAheadDepth`, a
  value-relative constraint `ParamSpec.requires` cannot express.
- `qcomp`/`ipRatio`/`pbRatio`/`cplxDecay` — CRF rate-curve shaping;
  second-order until `cappedcrf` proves itself on hardware.
- `chromaQpOffset` — VMAF is luma-only; the optimizer would learn to starve
  chroma to feed luma bits.
