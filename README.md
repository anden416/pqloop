# pqloop

Picture-quality optimization loop for ffmpeg. pqloop re-encodes a short clip of
your content over and over, measures each attempt with
[Netflix VMAF](https://github.com/Netflix/vmaf), and searches the encoder's
parameter space — always at your target bitrate — until improvements show
diminishing returns. The result is a saved, resumable preset you can use to
produce segmented streaming output (HLS/DASH/CMAF) for a packager origin, or
a single (fragmented or progressive) mp4 file. For VOD, `pqloop ladder` runs
the whole flow across an ABR ladder in one command: every rung optimized,
then packaged into one keyframe-aligned multi-rendition HLS/DASH/CMAF output.

```
             ┌────────────────────────────────────────────┐
             │                                            │
input ──► mezzanine ──► encode trial ──► VMAF vs mezz ──► pick next params
(file /   (lossless      (ffmpeg,          (libvmaf)       (impact-guided
 multicast) clip, deint)  target bitrate)                   hill climbing)
```

## Requirements

- Python ≥ 3.9 (stdlib only, no pip dependencies)
- ffmpeg/ffprobe for encoding (any build: stock, nvenc, netint/quadra, ...).
  `--ffmpeg` picks the binary; ffprobe is expected next to it (or on PATH),
  `--ffprobe` overrides.
- an ffmpeg with the `libvmaf` filter for measurement. This may be a
  *different* binary from the encode ffmpeg — pqloop auto-detects one from:
  `--vmaf-ffmpeg`, the encode ffmpeg, `ffmpeg` on PATH, then
  `tools/ffmpeg-static/bin/ffmpeg`. (Ubuntu's distro ffmpeg lacks libvmaf; a
  [BtbN static build](https://github.com/BtbN/FFmpeg-Builds/releases) dropped
  into `tools/ffmpeg-static/` works out of the box.)

Run from the repo (`python3 -m pqloop ...`) or install the `pqloop` command:
`pip install -e .` — editable, so it always runs the current checkout. On
PEP 668 distros (Ubuntu 23.04+) outside a venv:
`python3 -m pip install --user --break-system-packages -e .`
(user-site only; pqloop has no dependencies, so nothing system-owned is touched).

## Quick start

```bash
# what does pqloop see? (resolution, fps, interlacing, audio, programs)
python3 -m pqloop probe -i input/match.ts

# optimize a "sports" preset: 6 Mbps, 20 s clip starting 65 min in
python3 -m pqloop optimize -i input/match.ts -p sports -b 6000k \
    --clip-start 01:05:08 --clip-duration 20

# keep tweaking later — the preset remembers its input and settings, cached
# trials replay for free, and the search continues where it stopped
python3 -m pqloop optimize -p sports

# see what the baseline command would be without encoding anything
python3 -m pqloop optimize -p sports --dry-run

# produce packager-ready fMP4 HLS segments with the tuned parameters
python3 -m pqloop encode -p sports -i input/match.ts -o output/sports_hls \
    --duration 60

# or a CMAF package: one fMP4 segment set, DASH + HLS manifests
python3 -m pqloop encode -p sports -i input/match.ts -o output/sports_cmaf \
    --format cmaf --duration 60

# or a whole ABR ladder in one command — optimize every rung, then package
# (full story under "Multi-resolution ladders" below)
python3 -m pqloop ladder -p sports_abr -i input/match.ts \
    --rung 1280x720:2800k --rung 640x360:700k -o output/abr

# offline analysis
python3 -m pqloop report stats/<run_id>.jsonl     # summary + CSV
python3 -m pqloop presets                          # list saved presets
python3 -m pqloop presets --show sports            # dump one preset as JSON
```

Common variations:

```bash
# bound a run: 25 encodes max, or 30 minutes, whichever comes first
python3 -m pqloop optimize -p sports --max-trials 25 --max-seconds 1800

# optimize a lower ladder rung: encode at 1280x720, VMAF still scores at
# source resolution (see "Multi-resolution ladders" below)
python3 -m pqloop optimize -i input/match.ts -p sports_720p -b 2800k \
    --scale 1280x720

# protect worst-case frames instead of the average (see "Choosing a metric")
python3 -m pqloop optimize -p sports --metric p5

# two-pass rate control (libx264 and libx265) — typically lands on target
python3 -m pqloop optimize -p sports --two-pass
```

Every flag is listed by `python3 -m pqloop <command> --help`; anything not
covered in this README is a plumbing override documented there.

## Multi-resolution ladders

`pqloop ladder` is the one-command VOD flow: define the rungs once, and it
optimizes every rung and packages the result —

```bash
python3 -m pqloop ladder -p sports_abr -i input/match.ts \
    --rung 1920x1080:5800k --rung 1280x720:2800k --rung 960x540:1600k \
    --rung 640x360:700k --rung 426x240:350k \
    --clip-start 300 --clip-duration 20 --max-seconds 1800 \
    -o output/abr --format hls
```

Each rung becomes an ordinary preset (`sports_abr_1080p`, ...) sharing one
capture and one lossless mezzanine, optimized top-down with **warm starts**:
every rung below the first begins from the rung above's best parameters and
measured impact ordering, skipping the screening those measurements already
paid for. Budgets (`--max-seconds`, `--max-trials`) apply per rung. Re-running
the same command resumes every rung's search (cached trials replay instantly),
re-uses packaging intermediates, and the ladder spec (`presets/sports_abr.json`)
remembers the rungs and input. Omit `-o` to only optimize; `source:BITRATE`
makes a rung at source resolution; `--no-seed` cold-starts every rung instead.
Live inputs record the shared capture once and reuse it across rungs and
re-runs automatically (fresh scores would otherwise reset every rung's cache);
packaging a live input (`-o`) needs `--capture-duration` to bound the
deliverable recording.

Rungs can also be managed by hand — each is its own preset: same input, one
preset per resolution, with `--scale` and that rung's bitrate. VMAF always
compares against the source-resolution mezzanine (the trial encode is
upscaled back for measurement), so scores are directly comparable across
rungs and each rung's parameters get tuned for its own scaling/bitrate
trade-offs:

```bash
python3 -m pqloop optimize -i input/match.ts -p sports_1080p -b 6000k
python3 -m pqloop optimize -i input/match.ts -p sports_720p  -b 3500k --scale 1280x720
python3 -m pqloop optimize -i input/match.ts -p sports_540p  -b 2000k --scale 960x540
```

Every preset remembers its own scale, bitrate, and search state
(`work/<preset>/` holds its mezzanine and artifacts), so rungs resume
independently and can be tuned one after another or on different machines.
Then package the rungs into one ABR deliverable — a master playlist (or MPD)
whose variants are keyframe-aligned by construction, with one shared audio
rendition:

```bash
python3 -m pqloop package -p sports_1080p -p sports_720p -p sports_540p \
    -i input/match.ts -o output/abr --format hls    # or dash / cmaf
```

`package` encodes one video-only intermediate per rung (each with its own
preset's tuned parameters and ffmpeg binary — hardware and software rungs
mix freely), then remuxes everything in a single stream-copy pass. The
intermediates are kept under `output/abr/_work/` and re-used: re-running
with another `--format` re-packages without re-encoding, and an interrupted
ladder resumes at the first missing rung. It validates cross-rung
consistency (segment duration, frame rate, codec family), measures real
peak/average `BANDWIDTH` into the master playlist, and verifies keyframe
alignment across the finished intermediates. Each rung's codec profile/level
is reported, with a warning when H.264 exceeds Level 4.1 (a common device
ceiling — inherent for 1080p50+, otherwise usually the optimizer picking many
reference frames; `--h264-level 4.1` clamps at encode time, or re-optimize
that rung with `--freeze refs=4`). Single-rung output is still available via
`pqloop encode -p sports_1080p -o output/1080p`.

Changing `--scale` (or the bitrate) on an existing preset resets its cached
scores — they were measured against a different objective — but keeps the
best-known parameters and impact ordering as priors. For a 4K ladder, also
switch the model: `--vmaf-model version=vmaf_4k_v0.6.1`.

## Live / multicast inputs

Live URLs (`udp://`, `rtp://`, `srt://`, `rist://`) work like files: pqloop
records the stream first (stream copy to `work/<preset>/capture.ts`), then
loops on the recording. The capture length defaults to
`clip_start + clip_duration + 2 s` (`--capture-duration` overrides), and
`--clip-start` skips into the capture
— use a few seconds to jump past the multicast join junk (PAT/PMT
acquisition, the wait for the first IDR):

```bash
python3 -m pqloop optimize -i "udp://239.1.1.1:5000" -p channel4 -b 4500k \
    --clip-start 5 --clip-duration 30
```

**Resuming on live content.** By default every run recaptures — fresh content
means fresh scores, so the trial cache resets. To iterate on encoder
parameters against the *same* captured clip across many runs (hours or days
apart), add `--reuse-capture`:

```bash
# first run captures; every later run reuses the same recording, so cached
# trials replay for free and the search genuinely resumes
python3 -m pqloop optimize -i "udp://239.1.1.1:5000" -p channel4 -b 4500k \
    --clip-start 5 --clip-duration 30 --reuse-capture
```

The capture's sidecar meta (`capture.ts.json`) records url/program/input-args;
if any of those change — or you ask for a longer capture than exists — pqloop
recaptures and tells you why.

**Multi-program transport streams.** Contribution multicast often carries
several programs. Pick one with `--program` (works on `optimize`, `encode`
and `probe`; pqloop errors with the available IDs if yours isn't found):

```bash
python3 -m pqloop probe -i "udp://239.1.1.1:5000" --program 1010
python3 -m pqloop optimize -i "udp://239.1.1.1:5000" --program 1010 \
    -p channel4 -b 4500k --reuse-capture
```

**Lossy networks.** Captures run with `-fflags +genpts` for TS discontinuity
tolerance, but socket-level tuning belongs in the URL or in
`--extra-input-args`:

```bash
python3 -m pqloop optimize \
    -i "udp://239.1.1.1:5000?fifo_size=1000000&overrun_nonfatal=1" \
    -p channel4 -b 4500k
```

## How the search works

Not random, and not exhaustive:

1. **Baseline** — encode with the encoder's defaults at the target bitrate.
2. **Screening** — each tunable parameter is probed one-at-a-time (in
   curated expected-impact order) to *measure* how many VMAF points it is
   worth on *your* content. Improvements are adopted immediately.
3. **Refinement passes** — parameters are revisited in measured-impact order;
   ordinal parameters hill-climb through their value ladder, categorical ones
   try all values. A change is only adopted if it wins by at least
   `--adopt-eps` (default 0.05 points), so measurement noise doesn't steer
   the walk. Because knobs interact (a preset step that lost at baseline
   settings often wins after AQ/psy tuning), passes repeat — but a parameter
   is only re-examined if something else changed since it was last tried.
4. **Exit** — when a full pass gains less than `--min-gain` (default 0.2 VMAF
   points): diminishing returns — or after `--max-passes` refinement passes
   (default 6). Budgets (`--max-trials`, `--max-seconds`, `--target-score`)
   also stop the run; resuming continues the same walk.

Every evaluated configuration is cached in the preset by its *effective*
parameter signature (inert knobs stripped — `merange` doesn't count while
`me=hex`), so nothing is ever encoded twice. That cache is also what makes
resume free and deterministic.

**The objective** is the chosen VMAF aggregate (`--metric`) minus a penalty
when the measured bitrate overshoots the target beyond tolerance
(`--bitrate-tolerance`, default 5%; `--overshoot-penalty` points per percent
beyond it). Undershoot is not penalized by default — the rate controller
already had its chance to spend those bits — but single-pass ABR often leaves
real money on the table (10%+ under target is common on sports). Two ways to
push back:

```bash
# make the encoder actually hit the target
python3 -m pqloop optimize -p sports --two-pass

# or penalize configs that leave the budget unspent (symmetric to overshoot)
python3 -m pqloop optimize -p sports --undershoot-penalty 0.5
```

Encodes are VBV-constrained (`-maxrate`/`-bufsize` from
`--maxrate-ratio`/`--bufsize-ratio`, defaults 1.10/2.0) so trials behave like
real streaming ladder encodes.

### Choosing a metric

`--metric mean` (default) optimizes the average frame. For live sports that
average hides exactly the moments viewers complain about — fast pans over
grass and crowd, where per-frame VMAF craters. The aggregates are all
recorded on every trial regardless; pick the one that matches what you're
protecting:

| metric       | optimizes for                             |
|--------------|-------------------------------------------|
| `mean`       | the average frame (default)               |
| `harmonic`   | mean with low frames weighted heavier     |
| `p5` / `p1`  | the worst 5% / 1% of frames               |
| `min`        | the single worst frame (noisy — beware)   |

```bash
python3 -m pqloop optimize -p sports --metric p5
```

Changing the metric on an existing preset resets its cached scores (they were
measured against a different objective) but keeps the best-known parameters
and impact ordering as the starting point.

### The reference (mezzanine)

The clip (`--clip-start`/`--clip-duration`, default 30 s) is extracted once
into a lossless x264 mezzanine — deinterlaced, CFR, yuv420p, bit-exact muxed
so a rebuild from unchanged inputs reproduces the same file (and keeps the
trial cache valid). All trials encode this mezzanine and VMAF compares
against it, so every trial sees byte-identical reference frames and the
deinterlace cost is paid once. Reuse is keyed on the *content* of the source,
not timestamps, so recapturing identical bytes or copying inputs between
servers doesn't force a rebuild.

VMAF uses libvmaf's default model `vmaf_v0.6.1` (the Netflix VMAF v1
default); override with `--vmaf-model`, e.g. `version=vmaf_4k_v0.6.1` for a
4K ladder, and speed up long clips with `--vmaf-subsample N` (score every Nth
frame). `--vmaf-threads` caps scoring threads (default: all cores).

Note the mezzanine is 8-bit yuv420p: 10-bit/HDR sources are measured through
an 8-bit reference for now. You can still *encode* 10-bit
(`--pix-fmt yuv420p10le`).

### Interlaced sources

`--deinterlace auto` (default) checks the stream's field order.
`--deint-mode field` (default) uses `bwdif=mode=send_field` — 1080i25 becomes
1080p50, the right call for sports; `--deint-mode frame` keeps 25p.

## Encoders

GOP/keyframe placement is *not* tuned — it is fixed, not searched, so trials
measure what a packager-fed production encode ships. By default the GOP equals
your segment (`--seg-duration`, default 4 s: `-g`, scene-cut off, forced IDR at
segment boundaries). To run a shorter keyframe interval than the segment — say a
2 s GOP inside 4 s segments (100 frames at 50 fps) — set `--gop-duration`:

```bash
python3 -m pqloop optimize -i input/match.ts -p sports -b 6000k \
    --seg-duration 4 --gop-duration 2
```

`--gop-duration` must divide the segment evenly so every segment still *starts*
on a keyframe; the extra keyframes land inside each segment while the segment
boundaries stay IDR-aligned. The value is stored in the preset, so `encode`
ships the same cadence (override it there with `--gop-duration` too). Note for
`libsvtav1`: ffmpeg forces keyframes purely from `-g` there (it ignores
`-force_key_frames`), so the divides-evenly rule is what keeps its segments
keyframe-aligned.

Curated parameter spaces and how to run them:

**libx264** (default) — 16 knobs: preset, psy-rd, aq-mode, subme, aq-strength,
rc-lookahead, bframes, qcomp, refs, me, trellis, deblock, merange, b-adapt,
direct, tune.

```bash
python3 -m pqloop optimize -i input/match.ts -p sports -b 6000k
```

**libx265** — 12 knobs (preset, psy-rd, psy-rdoq, aq, sao, rd, refs, ctu...).
Two-pass works here too (pqloop routes it through `-x265-params pass=/stats=`
internally, because ffmpeg's x265 wrapper silently ignores `-pass`).

```bash
python3 -m pqloop optimize -i input/match.ts -p sports_hevc \
    --encoder libx265 -b 3500k --two-pass
```

**libsvtav1** — preset (12→3), tune, aq-mode, quant matrices, lookahead,
temporal filtering, overlays, film-grain. Scene-cut detection is disabled
(`scd=0`) so keyframes land on segment boundaries.

```bash
python3 -m pqloop optimize -i input/match.ts -p sports_av1 \
    --encoder libsvtav1 -b 2500k
```

**h264_nvenc / hevc_nvenc** — preset p1–p7, multipass, spatial/temporal AQ,
lookahead, B-ref. Point `--ffmpeg` at your nvenc-enabled build if the default
one lacks it.

```bash
python3 -m pqloop optimize -i input/match.ts -p sports_nv \
    --encoder hevc_nvenc -b 3500k --ffmpeg /usr/local/cuda-ffmpeg/bin/ffmpeg
```

**h264_ni_quadra_enc / h265_ni_quadra_enc** (*experimental*) — NETINT Quadra
via the netint ffmpeg fork's `-xcoder-params`. Uniquely here, the
rate-control *mode* is part of the search (`rcMode`: constrained VBR, CBR,
ABR, capped CRF — same bitrate target, four different ways to spend it),
alongside lookAheadDepth, RDO quantization (`enableRdoQuant`, plus
`rdoLevel` on h265), `crf` (capped-CRF mode only), `intraQPDelta`,
`hvsQPEnable`, `cuLevelRCEnable`, `bitrateMode`, `enableipRatio`,
`gopPresetIdx`, the lookahead refinements (`noMbTree`, `enable2PassGop`,
`tuneBframeVisual`) and `fillerEnable`. Mode- and lookahead-dependent knobs
are dependency-gated, so inert combinations are never encoded. Quadra
ignores `-g`/`-maxrate`/`-bufsize`: pqloop drives the keyframe cadence with
`intraPeriod` (tracks the GOP) and emits `RcEnable`/`vbvBufferSize`/
`vbvMaxRate` per rate-control mode, derived from the usual ratios. The
space is checked against the netint_ffmpeg + libxcoder v5.7 sources and the
Quadra Integration & Programming Guide v5.7 (the facts relied on are
summarized in `docs/quadra_parameter_notes.md`), but has not been validated
on hardware yet, so inspect the emitted command first — `--dry-run` works even
on a machine without the encoder installed (it only probes the input; no
libvmaf build, live capture, or mezzanine is needed):

```bash
python3 -m pqloop optimize -i input/match.ts -p quadra_test \
    --encoder h264_ni_quadra_enc -b 6000k --dry-run
# on the Quadra box:
python3 -m pqloop optimize -i input/match.ts -p quadra_test -b 6000k \
    --ffmpeg /opt/netint/bin/ffmpeg
```

Any other encoder runs with rate control + GOP + `--extra-video-args`
passthrough — no curated knobs, only the baseline is measured. Adding a space
is one list of `ParamSpec`s in `pqloop/encoders.py`.

### Steering the search

```bash
# restrict the search to a few knobs
python3 -m pqloop optimize -p sports --tune-params preset,aq-mode,psy-rd

# drop knobs entirely
python3 -m pqloop optimize -p sports --exclude-params tune

# pin a value and keep it out of the search (see the psy caveat below);
# --unfreeze psy-rd reverses it. Freezes are remembered by the preset.
python3 -m pqloop optimize -p sports --freeze psy-rd=1.0

# skip screening (e.g. when resuming with known sensitivities)
python3 -m pqloop optimize -p sports --no-screen

# pass raw ffmpeg args into every trial AND the final encode
python3 -m pqloop optimize -p sports --extra-video-args "-flags +cgop"
```

## Presets

One JSON file per preset (`presets/<name>.json`) holding the full
configuration, optimizer state (current point, per-parameter sensitivities,
every trial's cached result) and the best parameters found. Re-running
`optimize` with the same preset resumes; `encode` consumes the best result.
CLI flags always override stored values; the preset remembers its last input.

Cached scores are dropped — with best-known parameters and measured impact
ordering carried over as priors — whenever they'd no longer be comparable:

- the reference clip changed (different content, clip window, or deinterlace
  settings), or
- the objective changed (encoder, target bitrate, VBV ratios, tolerance or
  penalties, metric, scale, pix_fmt, seg duration, GOP duration, two-pass, VMAF
  model or subsampling, extra video args).

Presets travel between servers: the tuned parameters are plain encoder
settings and transfer as-is. The trial cache is tied to the mezzanine
fingerprint, so on a new machine the reference is rebuilt and scores
re-measure from the carried-over priors.

## Statistics

Each run writes `stats/<run_id>.jsonl` — a `meta` record (schema version,
hostname/platform, full config, source, mezzanine, both ffmpeg versions), one
`trial` record per evaluation (parameters, VMAF mean/harmonic/min/p1/p5,
bitrate, over/under-target %, encode time, penalty, objective) and a `done`
record — plus a flattened CSV next to it for spreadsheets/pandas. Every
record carries the `run_id`, so rows from many runs and many servers combine
without losing which machine produced what (column sets vary per encoder, so
merge with something union-aware rather than shell-concatenating):

```bash
python3 -m pqloop report stats/20260705-161125_sports.jsonl   # summary + CSV
python3 -c "import glob, pandas as pd; \
    pd.concat(map(pd.read_csv, glob.glob('stats/*.csv'))).to_csv('all.csv')"
```

`report --csv PATH` picks where the CSV lands (default: next to the jsonl).

The best trial's encode and its per-frame VMAF log are kept as
`work/<preset>/best_trial.*` (add `--keep-trials` to keep every trial).

## Segmented output

`pqloop encode` re-runs the tuned parameters on the full input (or
`--start`/`--duration` a window) at the preset's bitrate (`--target-bitrate`
overrides) with the same deinterlace/GOP discipline and packages:

- `--format hls` (default): fMP4 — `init.mp4` + `seg_%05d.m4s` +
  `index.m3u8` + `master.m3u8` (`--hls-segment-type mpegts` for TS segments)
- `--format dash`: `manifest.mpd` + templated segments
- `--format cmaf`: one fMP4 segment set served to both worlds —
  `manifest.mpd` *and* `master.m3u8`/`media_*.m3u8` reference the same
  `init-stream*.m4s` + `chunk-stream*.m4s` files
- `--format fmp4`: one single fragmented mp4 file (`output.mp4`). Keyframes
  are forced on segment boundaries, so it carries one CMAF-style fragment
  per `--seg-duration` — ready for byte-range packaging, and playable
  before the encode finishes (no trailing moov to wait for)
- `--format mp4`: single progressive (faststart) file

```bash
# everything in one fragmented mp4 file
python3 -m pqloop encode -p sports -i input/match.ts -o output/sports_fmp4 \
    --format fmp4 --duration 60
```

HEVC output in mp4/fMP4 is tagged `hvc1` (Apple players reject the ffmpeg
default `hev1`). Audio is re-encoded to stereo AAC (`--audio-bitrate`,
default 128k; `--no-audio` drops it). Live inputs record first
(`--capture-duration` bounds the recording; `--program` selects from an MPTS):

```bash
python3 -m pqloop encode -p channel4 -i "udp://239.1.1.1:5000" --program 1010 \
    --capture-duration 60 -o output/channel4_hls
```

## A note on VMAF vs. eyeballs

Maximizing a metric is the point of this tool — but know that VMAF mildly
rewards turning off psycho-visual optimizations (`psy-rd`, grain retention)
that human viewers often prefer. pqloop will find those wins honestly. If you
want the metric gains *without* giving up psy: `--freeze psy-rd=1.0` (x264)
and let the rest of the space do the work. Golden-eye check the
`work/<preset>/best_trial.mp4` before shipping a ladder.

## Development

```bash
python3 -m unittest discover -s tests            # full suite, no ffmpeg needed
python3 -m unittest tests.test_optimizer         # one module
```

Tests are hermetic (fake ffmpeg wrappers, no encoding) and run in CI on
Python 3.9 and 3.12 (`.github/workflows/ci.yml`).

```
pqloop/            the package (stdlib only)
presets/           saved presets (JSON, resumable)            --presets-dir
stats/             per-run JSONL + CSV                        --stats-dir
work/<preset>/     capture, mezzanine, best trial artifacts   --work-dir
work/<ladder>/     shared capture + mezzanine, per-rung dirs  --work-dir
tools/             optional: static ffmpeg with libvmaf for measurement
```

Each runtime directory can be relocated with the flag listed next to it.

`input/`, `output/`, `work/`, `stats/`, and `tools/` are gitignored local
cache/artifact directories and can grow large (captures, mezzanines, trial
encodes). Everything in them is recreated on demand, so they are safe to
prune when disk space matters — deleting `work/<preset>/` only costs a
mezzanine rebuild on the next run.
