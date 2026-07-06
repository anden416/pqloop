"""Final deliverable encode: segmented output for a packager/origin service."""

from __future__ import annotations

from pathlib import Path

from .encoders import RateControl, codec_family
from . import media


def build_encode_args(space, params, cfg, source, out_dir, fmt="hls",
                      hls_segment_type="fmp4", audio_kbps=128, want_audio=True,
                      start=None, duration=None, faststart=True):
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
    use_audio = want_audio and source.has_audio
    if use_audio:
        args += source.audio_map()
    if filters:
        args += ["-vf", ",".join(filters)]
    if deinterlaced:
        args += ["-r", fps_str]
    args += space.video_args(params, gop_len=gop_len, seg_duration=seg, rc=rc)
    # Apple players require the hvc1 sample entry for HEVC in mp4/fMP4
    # (ffmpeg defaults to hev1); mpegts segments carry no such tag.
    if codec_family(space.codec) == "hevc" and (
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

    if fmt == "hls":
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
            "seg": seg, "target": target, "timeout": timeout}
    return args, main_output, meta


def final_encode(ff, space, params, cfg, source, out_dir, fmt="hls",
                 hls_segment_type="fmp4", audio_kbps=128, want_audio=True,
                 start=None, duration=None, log=None) -> dict:
    """Encode `source` (a local file; live inputs are captured first by the CLI)
    with the tuned parameters into segmented HLS/DASH/CMAF, a single
    fragmented MP4 (fmp4), or a plain progressive MP4."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = log or (lambda m: None)
    args, main_output, meta = build_encode_args(
        space, params, cfg, source, out_dir, fmt=fmt,
        hls_segment_type=hls_segment_type, audio_kbps=audio_kbps,
        want_audio=want_audio, start=start, duration=duration)
    gop_len, fps = meta["gop"], meta["fps"]
    log(f"encoding deliverable -> {main_output} "
        f"({'deinterlaced ' if meta['deinterlaced'] else ''}{fmt}, "
        f"{meta['target']}k, GOP {gop_len} @ {fps:g}fps, seg {meta['seg']:g}s)")
    ff.run(args, timeout=meta["timeout"])

    segments = sorted([p.name for p in out_dir.iterdir()
                       if p.suffix in (".m4s", ".ts", ".mp4", ".m4a")
                       and p.name != "output.mp4"])
    return {"output": str(main_output), "segments": len(segments),
            "gop": gop_len, "fps": fps, "deinterlaced": meta["deinterlaced"]}


def cfg_get(cfg, key, default=None):
    if isinstance(cfg, dict):
        value = cfg.get(key, default)
    else:
        value = getattr(cfg, key, default)
    return default if value is None else value
