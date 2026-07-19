"""Final deliverable encode: segmented output for a packager/origin service."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .encoders import RateControl, codec_family
from . import media


@dataclass
class EncodePlan:
    """One logical encode, represented by one or two ffmpeg commands."""

    commands: list
    main_output: Path
    meta: dict
    passlog: Path = None


def build_encode_args(space, params, cfg, source, out_dir, fmt="hls",
                      hls_segment_type="fmp4", audio_kbps=128, want_audio=True,
                      start=None, duration=None, faststart=True,
                      pass_num=0, passlog=None):
    """Build the deliverable-encode command without running it. Returns
    (args, main_output, meta); meta carries what final_encode needs for
    logging/running and what package.py records in intermediate sidecars."""
    out_dir = Path(out_dir)

    deinterlaced = media.deinterlace_decision(source, cfg_get(cfg, "deinterlace", "auto"))
    deint_mode = cfg_get(cfg, "deint_mode", "field")
    filters = media.build_filters(deinterlaced, deint_mode)
    norm = media.normalization_filters(source, cfg)
    if norm:
        # pin the exact pipeline the trials saw: the mezzanine quantized to
        # 8-bit yuv420p after normalization, so the rung scale below must too
        filters += norm + ["format=yuv420p"]
    scale = cfg_get(cfg, "scale", "")
    if scale:
        w, h = str(scale).lower().split("x", 1)
        filters.append(f"scale={int(w)}:{int(h)}:flags=lanczos")
    fps, fps_str = media.output_fps(source.fps, source.fps_str, deinterlaced, deint_mode)

    seg = float(cfg_get(cfg, "seg_duration", 4.0))
    target = int(cfg_get(cfg, "target_bitrate_kbps"))
    rc = RateControl(
        bitrate_kbps=target,
        maxrate_kbps=int(round(target * float(cfg_get(cfg, "maxrate_ratio", 1.10)))),
        bufsize_kbps=int(round(target * float(cfg_get(cfg, "bufsize_ratio", 2.0)))),
    )
    gop_dur = cfg_get(cfg, "gop_duration", None) or seg
    gop_len = max(1, int(round(gop_dur * fps)))

    args = ["-y"]
    if start:
        args += ["-ss", f"{float(start):.3f}"]
    args += ["-i", source.path]
    if duration:
        args += ["-t", f"{float(duration):.3f}"]
    args += source.video_map()
    # Pass one only analyzes video. Audio is encoded/muxed exactly once in the
    # final pass, matching TrialRunner's two-pass behavior.
    use_audio = pass_num != 1 and want_audio and source.has_audio
    if use_audio:
        args += source.audio_map()
    if filters:
        args += ["-vf", ",".join(filters)]
    if deinterlaced:
        args += ["-r", fps_str]
    args += space.video_args(params, gop_len=gop_len, seg_duration=seg, rc=rc,
                             pass_num=pass_num, passlog=passlog)
    # Apple players require the hvc1 sample entry for HEVC in mp4/fMP4
    # (ffmpeg defaults to hev1); mpegts segments carry no such tag.
    if pass_num != 1 and codec_family(space.codec) == "hevc" and (
            fmt in ("mp4", "fmp4", "dash", "cmaf")
            or (fmt == "hls" and hls_segment_type == "fmp4")):
        args += ["-tag:v", "hvc1"]
    args += ["-pix_fmt", cfg_get(cfg, "pix_fmt", "yuv420p")]
    if media.norm_engaged(source, cfg):
        # the tonemap chain outputs SDR bt709; say so in the bitstream VUI
        # (players must not inherit the source's HDR interpretation)
        args += ["-color_primaries", "bt709", "-color_trc", "bt709",
                 "-colorspace", "bt709", "-color_range", "tv"]
    args += list(cfg_get(cfg, "extra_video_args", []) or [])
    if use_audio:
        args += ["-c:a", "aac", "-b:a", f"{int(audio_kbps)}k", "-ac", "2"]
    else:
        args += ["-an"]

    if pass_num == 1:
        args += ["-sn", "-dn", "-f", "null", "-"]
        main_output = Path("-")
    elif fmt == "hls":
        playlist = out_dir / "index.m3u8"
        args += ["-f", "hls", "-hls_time", f"{seg:g}",
                 "-hls_playlist_type", "vod",
                 "-hls_flags", "independent_segments",
                 "-hls_list_size", "0",
                 "-master_pl_name", "master.m3u8"]
        if hls_segment_type == "fmp4":
            args += ["-hls_segment_type", "fmp4",
                     "-hls_fmp4_init_filename", "init.mp4",
                     "-hls_segment_filename", str(out_dir / "seg_%05d.m4s")]
        else:
            args += ["-hls_segment_type", "mpegts",
                     "-hls_segment_filename", str(out_dir / "seg_%05d.ts")]
        args += [str(playlist)]
        main_output = playlist
    elif fmt in ("dash", "cmaf"):
        manifest = out_dir / "manifest.mpd"
        args += ["-f", "dash", "-seg_duration", f"{seg:g}",
                 "-use_template", "1", "-use_timeline", "1"]
        if fmt == "cmaf":
            # CMAF: one set of fMP4 segments referenced by both manifests —
            # the dash muxer also writes master.m3u8 + per-stream playlists.
            args += ["-hls_playlist", "1", "-hls_master_name", "master.m3u8"]
        args += [str(manifest)]
        main_output = manifest
    elif fmt == "fmp4":
        # Single self-contained fragmented mp4: keyframes are already forced
        # on segment boundaries (scene-cut off), so frag_keyframe cuts one
        # fragment per --seg-duration — same cadence a packager would ship.
        outfile = out_dir / "output.mp4"
        args += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                 "-f", "mp4", str(outfile)]
        main_output = outfile
    elif fmt == "mp4":
        outfile = out_dir / "output.mp4"
        # faststart rewrites the whole file to front-load the moov; skip it for
        # local intermediates (ABR packaging) where that pass buys nothing.
        if faststart:
            args += ["-movflags", "+faststart"]
        args += ["-f", "mp4", str(outfile)]
        main_output = outfile
    else:
        raise ValueError(f"unknown output format {fmt!r} "
                         "(hls, dash, cmaf, fmp4, mp4)")

    enc_dur = float(duration) if duration else max(
        0.0, (source.duration or 0.0) - float(start or 0.0))
    timeout = max(1800.0, enc_dur * 240 + 900) if enc_dur > 0 else 21600.0
    meta = {"deinterlaced": deinterlaced, "fps": fps, "gop": gop_len,
            "seg": seg, "target": target, "duration": enc_dur,
            "timeout": timeout}
    return args, main_output, meta


def build_encode_plan(space, params, cfg, source, out_dir, fmt="hls",
                      hls_segment_type="fmp4", audio_kbps=128, want_audio=True,
                      start=None, duration=None, faststart=True,
                      passlog=None) -> EncodePlan:
    """Build the complete command plan for a final/intermediate encode."""
    out_dir = Path(out_dir)
    requested = bool(cfg_get(cfg, "two_pass", False))
    two_pass = requested and bool(space.two_pass)
    passlog = Path(passlog) if passlog else out_dir / ".pqloop-passlog"
    final_pass = 2 if two_pass else 0
    final_args, main_output, meta = build_encode_args(
        space, params, cfg, source, out_dir, fmt=fmt,
        hls_segment_type=hls_segment_type, audio_kbps=audio_kbps,
        want_audio=want_audio, start=start, duration=duration,
        faststart=faststart, pass_num=final_pass,
        passlog=passlog if two_pass else None)
    commands = []
    if two_pass:
        first_args, _, _ = build_encode_args(
            space, params, cfg, source, out_dir, fmt=fmt,
            hls_segment_type=hls_segment_type, audio_kbps=audio_kbps,
            want_audio=False, start=start, duration=duration,
            faststart=faststart, pass_num=1, passlog=passlog)
        commands.append(first_args)
    commands.append(final_args)
    meta.update({"passes": len(commands), "two_pass": two_pass,
                 "two_pass_requested": requested})
    return EncodePlan(commands=commands, main_output=Path(main_output), meta=meta,
                      passlog=passlog if two_pass else None)


def cleanup_passlog(passlog) -> None:
    if not passlog:
        return
    path = Path(passlog)
    for leftover in path.parent.glob(path.name + "*"):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass


def _progress_seconds(record) -> float:
    """Extract encoded media time from an ffmpeg ``-progress`` record."""
    for key in ("out_time_us", "out_time_ms"):
        try:
            return max(0.0, float(record[key]) / 1_000_000.0)
        except (KeyError, TypeError, ValueError):
            pass
    try:
        hours, minutes, seconds = str(record["out_time"]).split(":", 2)
        return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _clock(seconds) -> str:
    total = max(0, int(float(seconds) + 0.5))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class _EncodeProgress:
    """Turn ffmpeg progress records into compact, rate-limited log lines."""

    def __init__(self, log, pass_number, passes, duration):
        self.log = log
        self.label = f"encode pass {pass_number}/{passes}"
        self.duration = float(duration or 0.0)
        self.bar_width = max(1, int(getattr(log, "bar_width", 24)))
        self.in_place = bool(getattr(log, "in_place", False))
        self.last_log_at = None
        self.last_percent_bucket = -1
        self.latest = {}
        self.done = False

    def __call__(self, record):
        self.latest = dict(record)
        position = _progress_seconds(record)
        percent = (min(100.0, position * 100.0 / self.duration)
                   if self.duration > 0 else None)
        percent_bucket = int(percent // 5) if percent is not None else -1
        now = time.monotonic()
        finished = record.get("progress") == "end"
        if not finished and not self.in_place and self.last_log_at is not None:
            new_bucket = percent_bucket > self.last_percent_bucket
            if not new_bucket and now - self.last_log_at < 10.0:
                return

        shown_percent = 100.0 if finished and percent is not None else percent
        if shown_percent is not None:
            filled = int(shown_percent * self.bar_width / 100.0)
            if shown_percent > 0 and filled == 0:
                filled = 1
            if shown_percent < 100.0:
                filled = min(self.bar_width - 1, filled)
            else:
                filled = self.bar_width
            bar = "█" * filled + "░" * (self.bar_width - filled)
            message = f"{self.label} [{bar}] {shown_percent:5.1f}%"
        else:
            message = f"{self.label}: {_clock(position)} encoded"

        if self.duration > 0:
            message += f" {_clock(position)}/{_clock(self.duration)}"
        details = []
        fps = str(record.get("fps", "")).strip()
        speed = str(record.get("speed", "")).strip()
        if fps and fps != "0.00":
            details.append(f"{fps}fps")
        if speed and speed != "N/A":
            details.append(speed)
        if details:
            message += " | " + " | ".join(details)
        if finished:
            message += " | done"
        self.log(message)
        if finished:
            complete = getattr(self.log, "complete", None)
            if callable(complete):
                complete()
        self.last_log_at = now
        self.last_percent_bucket = max(self.last_percent_bucket, percent_bucket)
        self.done = finished

    def finish(self):
        """Some ffmpeg builds omit the final marker; still close the display."""
        if not self.done:
            record = dict(self.latest)
            record["progress"] = "end"
            self(record)


def run_encode_plan(ff, plan: EncodePlan, progress=None) -> None:
    """Execute an EncodePlan and always remove two-pass scratch files."""
    try:
        passes = len(plan.commands)
        progress_runner = getattr(ff, "run_progress", None)
        for index, args in enumerate(plan.commands, 1):
            if progress is not None and callable(progress_runner):
                reporter = _EncodeProgress(
                    progress, index, passes, plan.meta.get("duration", 0.0))
                progress_runner(args, reporter, timeout=plan.meta["timeout"])
                reporter.finish()
            else:
                ff.run(args, timeout=plan.meta["timeout"])
    finally:
        complete = getattr(progress, "complete", None)
        if callable(complete):
            complete()
        cleanup_passlog(plan.passlog)


def final_encode(ff, space, params, cfg, source, out_dir, fmt="hls",
                 hls_segment_type="fmp4", audio_kbps=128, want_audio=True,
                 start=None, duration=None, log=None, progress=None) -> dict:
    """Encode `source` (a local file; live inputs are captured first by the CLI)
    with the tuned parameters into segmented HLS/DASH/CMAF, a single
    fragmented MP4 (fmp4), or a plain progressive MP4."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = log or (lambda m: None)
    plan = build_encode_plan(
        space, params, cfg, source, out_dir, fmt=fmt,
        hls_segment_type=hls_segment_type, audio_kbps=audio_kbps,
        want_audio=want_audio, start=start, duration=duration)
    main_output, meta = plan.main_output, plan.meta
    gop_len, fps = meta["gop"], meta["fps"]
    if meta["two_pass_requested"] and not meta["two_pass"]:
        log(f"note: --two-pass not supported for {space.name}; using single pass")
    log(f"encoding deliverable -> {main_output} "
        f"({'deinterlaced ' if meta['deinterlaced'] else ''}{fmt}, "
        f"{meta['target']}k, GOP {gop_len} @ {fps:g}fps, seg {meta['seg']:g}s)")
    run_encode_plan(ff, plan, progress=progress or log)

    segments = sorted([p.name for p in out_dir.iterdir()
                       if p.suffix in (".m4s", ".ts", ".mp4", ".m4a")
                       and p.name != "output.mp4"])
    return {"output": str(main_output), "segments": len(segments),
            "gop": gop_len, "fps": fps, "deinterlaced": meta["deinterlaced"],
            "passes": meta["passes"]}


def cfg_get(cfg, key, default=None):
    if isinstance(cfg, dict):
        value = cfg.get(key, default)
    else:
        value = getattr(cfg, key, default)
    return default if value is None else value
