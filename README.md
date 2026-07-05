# pqloop

Picture-quality optimization loop for ffmpeg. pqloop re-encodes a short clip of
your content over and over, measures each attempt with
[Netflix VMAF](https://github.com/Netflix/vmaf), and searches the encoder's
parameter space — always at your target bitrate — until improvements show
diminishing returns. The result is a saved, resumable preset you can use to
produce segmented streaming output (HLS/DASH) for a packager origin.

```
             ┌────────────────────────────────────────────┐
             │                                            │
input ──► mezzanine ──► encode trial ──► VMAF vs mezz ──► pick next params
(file /   (lossless      (ffmpeg,          (libvmaf)       (impact-guided
 multicast) clip, deint)  target bitrate)                   hill climbing)
```

## Requirements

- Python ≥ 3.9 (stdlib only, no pip dependencies)
- ffmpeg/ffprobe for encoding (any build: stock, nvenc, netint/quadra, ...)
- an ffmpeg with the `libvmaf` filter for measurement. This may be a
  *different* binary from the encode ffmpeg — pqloop auto-detects one from:
  `--vmaf-ffmpeg`, the encode ffmpeg, `ffmpeg` on PATH, then
  `tools/ffmpeg-static/bin/ffmpeg`. (Ubuntu's distro ffmpeg lacks libvmaf; a
  [BtbN static build](https://github.com/BtbN/FFmpeg-Builds/releases) dropped
  into `tools/ffmpeg-static/` works out of the box.)

Run from the repo (`python3 -m pqloop ...`) or install: `pip install -e .`
(gives the `pqloop` command).

## Quick start

```bash
# what does pqloop see? (resolution, fps, interlacing, audio)
python3 -m pqloop probe -i input/match.ts

# optimize a "sports" preset: 6 Mbps, 20 s clip starting 65 min in
python3 -m pqloop optimize -i input/match.ts -p sports -b 6000k \
    --clip-start 01:05:08 --clip-duration 20

# keep tweaking later — same command resumes from the preset (cached trials
# are free, the search continues where it stopped)
python3 -m pqloop optimize -i input/match.ts -p sports

# produce packager-ready CMAF/HLS segments with the tuned parameters
python3 -m pqloop encode -p sports -i input/match.ts -o output/sports_hls \
    --duration 60

# offline analysis
python3 -m pqloop report stats/<run_id>.jsonl     # summary + CSV
python3 -m pqloop presets                          # list saved presets
```

Live inputs work the same way — pqloop records the stream first (stream copy),
then loops on the recording:

```bash
python3 -m pqloop optimize -i "udp://239.1.1.1:5000" -p channel4 -b 4500k \
    --clip-duration 30 --capture-duration 40
```

## How the search works

Not random, and not exhaustive:

1. **Baseline** — encode with the encoder's defaults at the target bitrate.
2. **Screening** — each tunable parameter is probed one-at-a-time (in
   curated expected-impact order) to *measure* how many VMAF points it is
   worth on *your* content. Improvements are adopted immediately.
3. **Refinement passes** — parameters are revisited in measured-impact order;
   ordinal parameters hill-climb through their value ladder, categorical ones
   try all values. Because knobs interact (a preset step that lost at baseline
   settings often wins after AQ/psy tuning), passes repeat — but a parameter
   is only re-examined if something else changed since it was last tried.
4. **Exit** — when a full pass gains less than `--min-gain` (default 0.2 VMAF
   points): diminishing returns. Budgets (`--max-trials`, `--max-seconds`,
   `--target-score`) also stop the run; resuming continues the same walk.

Every evaluated configuration is cached in the preset by its *effective*
parameter signature, so nothing is ever encoded twice — this is also what
makes resume free and deterministic.

**The objective** is the chosen VMAF aggregate (`--metric mean|harmonic|p1|p5|min`)
minus a penalty when the measured bitrate overshoots the target beyond
tolerance (`--bitrate-tolerance`, default 5%). Undershoot is not penalized —
the rate controller already had its chance to spend those bits. Encodes are
VBV-constrained (`-maxrate`/`-bufsize` from `--maxrate-ratio`/`--bufsize-ratio`)
so trials behave like real streaming ladder encodes; `--two-pass` is available
for libx264.

### The reference (mezzanine)

The clip (`--clip-start`/`--clip-duration`, default 30 s) is extracted once
into a lossless x264 mezzanine — deinterlaced, CFR, yuv420p. All trials encode
this mezzanine and VMAF compares against it, so every trial sees byte-identical
reference frames and the deinterlace cost is paid once. VMAF uses libvmaf's
default model `vmaf_v0.6.1` (the Netflix VMAF v1 default); override with
`--vmaf-model`, e.g. `version=vmaf_4k_v0.6.1` for a 4K ladder, and speed up
long clips with `--vmaf-subsample N`.

### Interlaced sources

`--deinterlace auto` (default) checks the stream's field order.
`--deint-mode field` (default) uses `bwdif=mode=send_field` — 1080i25 becomes
1080p50, the right call for sports; `--deint-mode frame` keeps 25p.

### Encoders

Curated parameter spaces: **libx264** (16 knobs: preset, psy-rd, aq-mode,
subme, aq-strength, rc-lookahead, bframes, qcomp, refs, me, trellis, deblock,
merange, b-adapt, direct, tune), **libx265**, **h264_nvenc / hevc_nvenc**
(preset p1–p7, multipass, spatial/temporal AQ, lookahead, B-ref).
GOP/keyframe placement is *not* tuned — it is fixed to your segment duration
(`--seg-duration`, default 4 s: `-g`, scene-cut off, forced IDR at segment
boundaries) because a packager needs it that way, and trials should measure
what production will ship.

Other encoders (e.g. netint `h264_ni_quadra_enc`) run with rate control + GOP
+ `--extra-video-args` passthrough — no curated knobs yet; add a space in
`pqloop/encoders.py` (one list of `ParamSpec`s). Custom builds:
`--ffmpeg /opt/netint/ffmpeg` for encoding, `--vmaf-ffmpeg` for measurement.

Useful controls:

- `--tune-params preset,aq-mode,psy-rd` — restrict the search
- `--exclude-params tune` — drop knobs
- `--freeze psy-rd=1.0` — pin a value and keep it out of the search (see
  the psy caveat below); `--unfreeze psy-rd` reverses it
- `--no-screen` — skip screening (e.g. when resuming with known sensitivities)

## Presets

One JSON file per preset (`presets/<name>.json`) holding the full
configuration, optimizer state (current point, per-parameter sensitivities,
every trial's cached result) and the best parameters found. Re-running
`optimize` with the same preset resumes; `encode` consumes the best result.
CLI flags always override stored values; the preset remembers its last input.

If you point a preset at different content (or a different clip window), the
cached scores no longer apply: pqloop resets them but keeps the best-known
parameters as the starting point and the measured impact ordering as priors.

## Statistics

Each run writes `stats/<run_id>.jsonl` — a `meta` record (full config, source,
mezzanine, ffmpeg versions), one `trial` record per evaluation (parameters,
VMAF mean/harmonic/min/p1/p5, bitrate, encode time, penalty, objective) and a
`done` record — plus a flattened CSV next to it for spreadsheets/pandas.
`pqloop report` prints a summary of any stats file. The best trial's encode
and its per-frame VMAF log are kept as `work/<preset>/best_trial.*`.

## Segmented output

`pqloop encode` re-runs the tuned parameters on the full input (or
`--duration N` seconds) with the same deinterlace/GOP discipline and packages:

- `--format hls` (default): CMAF fMP4 — `init.mp4` + `seg_%05d.m4s` +
  `index.m3u8` (`--hls-segment-type mpegts` for TS segments)
- `--format dash`: `manifest.mpd` + templated segments
- `--format mp4`: single faststart file

Audio is passed through AAC stereo (`--audio-bitrate`, `--no-audio`).

## A note on VMAF vs. eyeballs

Maximizing a metric is the point of this tool — but know that VMAF mildly
rewards turning off psycho-visual optimizations (`psy-rd`, grain retention)
that human viewers often prefer. pqloop will find those wins honestly. If you
want the metric gains *without* giving up psy: `--freeze psy-rd=1.0` (x264)
and let the rest of the space do the work. Golden-eye check the
`work/<preset>/best_trial.mp4` before shipping a ladder.

## Layout

```
pqloop/            the package (stdlib only)
presets/           saved presets (JSON, resumable)
stats/             per-run JSONL + CSV
work/<preset>/     capture, mezzanine, best trial artifacts
tools/             optional: static ffmpeg with libvmaf for measurement
```
