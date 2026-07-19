"""Multi-preset ABR packaging (`pqloop package`).

Two phases. Phase A encodes one video-only plain-MP4 *intermediate* per rung
with that preset's tuned parameters (audio is encoded once, separately), each
with a sidecar recording exactly what produced it — re-runs skip intermediates
whose sidecar still matches, so re-packaging to another format is free and a
failed ladder resumes where it stopped. Phase B remuxes all intermediates in
a single `-c copy` ffmpeg run into HLS (variant streams + audio rendition
group), DASH (video/audio adaptation sets) or CMAF. Segments align across
rungs because every rung forces IDRs on the same segment cadence and the
remux applies one global timestamp offset per output (verified: heterogeneous
B-frame delays across rungs still yield identical segment start times).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import __version__, media, segment
from .encoders import codec_family
from .ffmpeg import FFmpegError
from .util import atomic_write_json, atomic_write_text, fingerprint_file, load_json

# phase A encodes audio with ffmpeg's native AAC-LC encoder
AAC_CODEC = "mp4a.40.2"


def _tool_identity(ff):
    identity = getattr(ff, "identity", None)
    return identity() if identity else {"version": ff.version()}


@dataclass
class Rung:
    """One ladder rung: a preset's tuned parameters bound to its own encode
    binary, plus the resolved output geometry (set by resolve_output)."""
    preset_name: str
    cfg: dict
    space: object            # EncoderSpace
    params: dict
    ff: object               # FF instance for this rung's encodes
    name: str = ""           # unique variant name; becomes the %v directory
    width: int = 0
    height: int = 0
    fps: float = 0.0

    @property
    def target_kbps(self) -> int:
        return int(self.cfg["target_bitrate_kbps"])


def resolve_output(rung: Rung, source) -> None:
    deint = media.deinterlace_decision(source, rung.cfg.get("deinterlace") or "auto")
    rung.fps, _ = media.output_fps(source.fps, source.fps_str, deint,
                                   rung.cfg.get("deint_mode") or "field")
    scale = rung.cfg.get("scale") or ""
    if scale:
        w, h = str(scale).lower().split("x", 1)
        rung.width, rung.height = int(w), int(h)
    else:
        # a scale-less rung outputs the *normalized* resolution, not the raw
        # source's (a UHD master normalized to 1080p yields a 1080p rung)
        rung.width, rung.height = media.norm_dims(source, rung.cfg)


def assign_names(rungs) -> None:
    """Variant names from output height (`720p`); same-height rungs get a
    bitrate suffix. Names end up in var_stream_map and as directory names."""
    counts = Counter(r.height for r in rungs)
    for r in rungs:
        r.name = (f"{r.height}p" if counts[r.height] == 1
                  else f"{r.height}p-{r.target_kbps}k")


def validate_rungs(rungs) -> list:
    """Raise on anything that breaks cross-rung alignment or manifest
    validity; return a list of warnings for the merely inadvisable."""
    if not rungs:
        raise ValueError("no rungs to package")

    def per_rung(fn):
        return ", ".join(f"{r.preset_name}={fn(r)}" for r in rungs)

    if len({float(r.cfg["seg_duration"]) for r in rungs}) > 1:
        raise ValueError(
            f"seg_duration differs across presets ({per_rung(lambda r: r.cfg['seg_duration'])}) "
            "— segments would not align; override with --seg-duration")
    if len({codec_family(r.space.codec) or r.space.codec for r in rungs}) > 1:
        raise ValueError(
            f"codec family differs across presets ({per_rung(lambda r: r.cfg['encoder'])}) "
            "— all rungs of one ladder must share a codec")
    if len({round(r.fps, 3) for r in rungs}) > 1:
        raise ValueError(
            f"output frame rate differs across rungs ({per_rung(lambda r: f'{r.fps:g}')}) "
            "— check the presets' deinterlace settings")
    bitrates = [r.target_kbps for r in rungs]
    if len(set(bitrates)) != len(bitrates):
        raise ValueError(f"duplicate target bitrates ({per_rung(lambda r: r.target_kbps)})")
    names = [r.name for r in rungs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate variant names ({', '.join(names)})")

    warnings = []
    gops = {float(r.cfg.get("gop_duration") or r.cfg["seg_duration"]) for r in rungs}
    if len(gops) > 1:
        warnings.append(f"GOP duration differs across rungs "
                        f"({per_rung(lambda r: r.cfg.get('gop_duration') or r.cfg['seg_duration'])}) "
                        "— spec-legal, but Apple's guidelines recommend matching")
    if len({r.cfg["pix_fmt"] for r in rungs}) > 1:
        warnings.append(f"pixel format differs across rungs "
                        f"({per_rung(lambda r: r.cfg['pix_fmt'])}) — codec profiles will differ")
    by_rate = sorted(rungs, key=lambda r: r.target_kbps)
    heights = [r.height for r in by_rate]
    if heights != sorted(heights):
        warnings.append("bitrate order does not follow resolution order "
                        f"({', '.join(f'{r.name}@{r.target_kbps}k' for r in by_rate)})")
    return warnings


def validate_source_selections(rungs, live=False) -> None:
    """Validate shared stream/input choices before any live capture is spent."""
    if not rungs:
        return

    def values(key):
        return {json.dumps(r.cfg.get(key), sort_keys=True, default=str)
                for r in rungs}

    for key in ("program", "audio_stream"):
        if len(values(key)) > 1:
            raise ValueError(
                f"{key} differs across presets — every rung must read the same "
                f"source streams (override --{key.replace('_', '-')} uniformly)")
    if live and len(values("extra_input_args")) > 1:
        raise ValueError(
            "extra_input_args differs across presets — one shared live capture "
            "cannot use multiple input configurations")


def validate_source_pipelines(rungs, source, live=False) -> None:
    """Reject cross-rung source choices that would produce unlike pictures.

    The ladder command already enforces raw config equality during preflight;
    direct ``package`` needs the equivalent protection, expressed in terms of
    the effective pipeline so harmless aliases such as auto/off on a
    progressive source remain compatible.
    """
    if not rungs:
        return
    validate_source_selections(rungs, live=live)

    pipeline_keys = []
    for r in rungs:
        deint = media.deinterlace_decision(
            source, r.cfg.get("deinterlace") or "auto")
        deint_mode = r.cfg.get("deint_mode") or "field"
        filters = media.build_filters(deint, deint_mode)
        filters += media.normalization_filters(source, r.cfg)
        _, fps_str = media.output_fps(source.fps, source.fps_str,
                                      deint, deint_mode)
        pipeline_keys.append((tuple(filters), fps_str))
    if len(set(pipeline_keys)) > 1:
        raise ValueError(
            "source normalization/deinterlace differs across presets — every "
            "rung must be derived from the same effective source pipeline")


# --------------------------------------------------------------------------- #
# phase A: intermediates
# --------------------------------------------------------------------------- #

def audio_args(source, out_path, audio_kbps, start=None, duration=None) -> list:
    """Audio-only AAC intermediate; -ss/-t placement mirrors the video rungs
    so the shared rendition stays in sync with every variant."""
    args = ["-y"]
    if start:
        args += ["-ss", f"{float(start):.3f}"]
    args += ["-i", source.path]
    if duration:
        args += ["-t", f"{float(duration):.3f}"]
    args += source.audio_map()
    args += ["-vn", "-sn", "-dn",
             "-c:a", "aac", "-b:a", f"{int(audio_kbps)}k", "-ac", "2",
             "-f", "mp4", str(out_path)]
    return args


def _intermediate_ready(ff, out_path, sidecar, identity) -> bool:
    """An intermediate counts as done only if its sidecar matches what we
    would encode now AND the file still probes (the moov sits at the end of
    these non-faststart files, so an interrupted encode is unreadable)."""
    if not (Path(out_path).exists() and Path(sidecar).exists()):
        return False
    try:
        if load_json(sidecar) != identity:
            return False
    except (json.JSONDecodeError, OSError):
        return False
    try:
        return bool(ff.probe(out_path).get("streams"))
    except FFmpegError:
        return False


def build_intermediates(rungs, source, work_dir, want_audio=True, audio_kbps=128,
                        start=None, duration=None, reuse=True, ff_audio=None,
                        log=None, progress=None) -> dict:
    """Encode (or reuse) per-rung video intermediates plus the shared audio
    intermediate. Returns {"video": [path, ...] in rung order, "audio": path|None}."""
    log = log or (lambda m: None)
    progress = progress or log
    work_dir = Path(work_dir)
    src_fp = fingerprint_file(source.path)
    videos = []
    for r in rungs:
        plan = segment.build_encode_plan(
            r.space, r.params, r.cfg, source, work_dir / r.name, fmt="mp4",
            want_audio=False, start=start, duration=duration, faststart=False)
        out_path, meta = plan.main_output, plan.meta
        sidecar = work_dir / f"{r.name}.json"
        identity = {"pqloop": __version__, "tools": _tool_identity(r.ff),
                    "source_fp": src_fp,
                    "commands": [[str(a) for a in args]
                                 for args in plan.commands]}
        if reuse and _intermediate_ready(r.ff, out_path, sidecar, identity):
            log(f"rung {r.name}: reusing intermediate ({out_path})")
        else:
            log(f"rung {r.name}: encoding {r.cfg['encoder']} @ {r.target_kbps}k "
                f"{r.width}x{r.height} (GOP {meta['gop']} @ {meta['fps']:g}fps, "
                f"{meta['passes']} pass{'es' if meta['passes'] != 1 else ''})")
            if meta["two_pass_requested"] and not meta["two_pass"]:
                log(f"note: --two-pass not supported for {r.space.name}; "
                    f"using single pass")
            (work_dir / r.name).mkdir(parents=True, exist_ok=True)
            segment.run_encode_plan(r.ff, plan, progress=progress)
            atomic_write_json(sidecar, identity)
        videos.append(out_path)

    audio_path = None
    if want_audio and source.has_audio:
        ff = ff_audio or rungs[0].ff
        audio_path = work_dir / "audio.mp4"
        args = audio_args(source, audio_path, audio_kbps, start, duration)
        sidecar = work_dir / "audio.json"
        identity = {"pqloop": __version__, "tools": _tool_identity(ff),
                    "source_fp": src_fp, "args": [str(a) for a in args]}
        if reuse and _intermediate_ready(ff, audio_path, sidecar, identity):
            log("audio: reusing intermediate")
        else:
            log(f"audio: encoding aac {int(audio_kbps)}k stereo")
            work_dir.mkdir(parents=True, exist_ok=True)
            enc_dur = float(duration) if duration else max(
                0.0, (source.duration or 0.0) - float(start or 0.0))
            ff.run(args, timeout=max(900.0, enc_dur * 30 + 300) if enc_dur > 0 else 7200.0)
            atomic_write_json(sidecar, identity)
    return {"video": videos, "audio": audio_path}


# --------------------------------------------------------------------------- #
# phase B: one -c copy remux into the ABR package
# --------------------------------------------------------------------------- #

def mux_args(fmt, out_dir, video_paths, names, audio_path=None,
             seg_duration=4.0, hls_segment_type="fmp4"):
    """The packaging command (validated against the pinned ffmpeg build; see
    module docstring). Returns (args, main_output). Note hlsenc quirks relied
    on: var_stream_map attributes use colon syntax (agroup:aud), %v in the
    last directory component fans variants into per-name subdirectories and
    puts the master playlist above them, and init_%v.mp4 keeps init segment
    names stable (without %v hlsenc appends _<index> itself)."""
    out_dir = Path(out_dir)
    args = ["-y"]
    for p in video_paths:
        args += ["-i", str(p)]
    if audio_path:
        args += ["-i", str(audio_path)]
    for i in range(len(video_paths)):
        args += ["-map", f"{i}:v:0"]
    if audio_path:
        args += ["-map", f"{len(video_paths)}:a:0"]
    args += ["-c", "copy"]

    if fmt == "hls":
        agroup = ",agroup:aud" if audio_path else ""
        vsm = [f"v:{i}{agroup},name:{name}" for i, name in enumerate(names)]
        if audio_path:
            vsm.append("a:0,agroup:aud,name:audio,default:yes")
        args += ["-f", "hls", "-hls_time", f"{seg_duration:g}",
                 "-hls_playlist_type", "vod",
                 "-hls_flags", "independent_segments",
                 "-hls_list_size", "0"]
        if hls_segment_type == "fmp4":
            args += ["-hls_segment_type", "fmp4",
                     "-hls_fmp4_init_filename", "init_%v.mp4"]
            ext = "m4s"
        else:
            args += ["-hls_segment_type", "mpegts"]
            ext = "ts"
        args += ["-var_stream_map", " ".join(vsm),
                 "-master_pl_name", "master.m3u8",
                 "-hls_segment_filename", str(out_dir / "%v" / f"seg_%05d.{ext}"),
                 str(out_dir / "%v" / "index.m3u8")]
        main_output = out_dir / "master.m3u8"
    elif fmt in ("dash", "cmaf"):
        # without -adaptation_sets the dash muxer makes one AdaptationSet per
        # stream, which breaks ABR switching
        sets = "id=0,streams=v id=1,streams=a" if audio_path else "id=0,streams=v"
        args += ["-f", "dash", "-seg_duration", f"{seg_duration:g}",
                 "-adaptation_sets", sets]
        if fmt == "cmaf":
            args += ["-hls_playlist", "1", "-hls_master_name", "master.m3u8"]
        args += [str(out_dir / "manifest.mpd")]
        main_output = out_dir / "manifest.mpd"
    else:
        raise ValueError(f"unknown package format {fmt!r} (hls, dash, cmaf)")
    return args, main_output


def mux_timeout(duration_s, n_variants) -> float:
    if not duration_s or duration_s <= 0:
        return 21600.0
    return max(1800.0, float(duration_s) * 5 * max(1, int(n_variants)) + 600)


# --------------------------------------------------------------------------- #
# master playlist / manifest fixup
# --------------------------------------------------------------------------- #

def rfc6381_hevc(profile, level, tier=None) -> str:
    """RFC 6381 codec string for the HEVC we produce (progressive, frame-only,
    non-packed — constraint byte 0xB0, which is what x265 signals). None for
    profiles we don't emit; callers then leave the manifest untouched."""
    key = str(profile or "").strip().lower().replace(" ", "")
    table = {"main": ("1", "6"), "main10": ("2", "4")}
    if key not in table or not level:
        return None
    idc, compat = table[key]
    t = "H" if str(tier or "").strip().lower() == "high" else "L"
    return f"hvc1.{idc}.{compat}.{t}{int(level)}.B0"


def video_codec_strings(rungs, video_paths) -> list:
    """Per-rung RFC 6381 video codec string (rung order), None where nothing
    needs fixing. ffmpeg's dash muxer writes a bare 'hvc1' into both manifests
    (invalid — Safari and strict DASH players reject it); the real profile and
    level come from probing the intermediates we just encoded."""
    return [video_codec_string(r.ff, path) for r, path in zip(rungs, video_paths)]


def video_codec_string(ff, path):
    """Probe one encoded output and return the HEVC RFC 6381 string to fix."""
    try:
        streams = ff.probe(path).get("streams", [])
        st = next((s for s in streams if s.get("codec_type") == "video"), {})
        if st.get("codec_name") == "hevc":
            return rfc6381_hevc(st.get("profile"), st.get("level"),
                                st.get("tier"))
    except FFmpegError:
        pass
    return None


_BARE_HEVC = re.compile(r"^(hvc1|hev1)$")


def _fix_codecs_attr(codecs, video_codec):
    """Replace a bare hvc1/hev1 entry in an EXT-X CODECS value (unquoted)."""
    parts = [p.strip() for p in codecs.split(",") if p.strip()]
    fixed = False
    for i, part in enumerate(parts):
        if _BARE_HEVC.match(part):
            parts[i] = video_codec
            fixed = True
    return ",".join(parts), fixed


def fixup_mpd(mpd_path, video_codecs) -> list:
    """Rewrite bare codecs="hvc1|hev1" Representation attributes in ffmpeg's
    MPD with the full per-rung strings (document order matches mux stream
    order, which is rung order). Idempotent via a trailing marker comment."""
    mpd_path = Path(mpd_path)
    codecs = [c for c in (video_codecs or []) if c]
    if not codecs:
        return []
    try:
        text = mpd_path.read_text()
    except OSError:
        return []
    if "pqloop-fixup" in text:
        return []
    bare = re.findall(r'codecs="(?:hvc1|hev1)"', text)
    if len(bare) != len(codecs):
        return []   # unexpected manifest shape — leave it alone
    it = iter(codecs)
    fixed_text = re.sub(r'codecs="(?:hvc1|hev1)"',
                        lambda _m: f'codecs="{next(it)}"', text)
    fixed_text += f"<!-- pqloop-fixup ({__version__}): hevc codec strings -->\n"
    atomic_write_text(mpd_path, fixed_text)
    return ["hevc codec strings completed"]


def _parse_attrs(text) -> dict:
    """EXT-X attribute list -> ordered dict; commas inside quotes are literal."""
    attrs, parts, cur, quoted = {}, [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur += ch
        elif ch == "," and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    for part in parts:
        key, _, value = part.partition("=")
        attrs[key] = value
    return attrs


def _int_attr(value) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return 0


def _playlist_bitrates(playlist_path):
    """(peak_bps, avg_bps) measured from a media playlist's EXTINF durations
    and segment file sizes (init segments excluded, matching hlsenc's own
    accounting; sub-0.5s tail segments are excluded from the peak)."""
    playlist_path = Path(playlist_path)
    try:
        text = playlist_path.read_text()
    except OSError:
        return None
    base = playlist_path.parent
    total_bytes, total_dur, peak, dur = 0, 0.0, 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[len("#EXTINF:"):].split(",")[0])
            except ValueError:
                dur = None
        elif line and not line.startswith("#") and dur is not None:
            try:
                size = (base / line).stat().st_size
            except OSError:
                return None
            total_bytes += size
            total_dur += dur
            if dur >= 0.5:
                peak = max(peak, size * 8 / dur)
            dur = None
    if total_dur <= 0:
        return None
    avg = total_bytes * 8 / total_dur
    return int(round(peak or avg)), int(round(avg))


def fixup_master(master_path, fps=None, audio_codec=None, video_codecs=None) -> list:
    """Post-process ffmpeg's master playlist into strict spec shape. What
    ffmpeg gets wrong (verified against the pinned build): variant BANDWIDTH
    excludes the audio rendition the variant will actually play; FRAME-RATE
    is never written; EXT-X-INDEPENDENT-SEGMENTS only reaches the media
    playlists. Older builds and the dash muxer's CMAF master additionally
    carry average-based BANDWIDTH, no AVERAGE-BANDWIDTH, and a bare 'hvc1'
    CODECS entry — bandwidths are recomputed from real segment sizes and the
    codec entry is completed from `video_codecs` (rung order, matching the
    variant order). Returns the list of fixes applied (idempotent: a marker
    comment prevents double application)."""
    master_path = Path(master_path)
    lines = master_path.read_text().splitlines()
    if any("pqloop-fixup" in line for line in lines):
        return []
    fixed = set()

    audio_uri = None
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            audio_uri = _parse_attrs(line[len("#EXT-X-MEDIA:"):]).get("URI", "").strip('"')
            break
    audio_rates = _playlist_bitrates(master_path.parent / audio_uri) if audio_uri else None

    out = []
    variant_no = -1
    for idx, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            out.append(line)
            continue
        attrs = _parse_attrs(line[len("#EXT-X-STREAM-INF:"):])
        variant_no += 1
        uri = next((l for l in lines[idx + 1:] if l and not l.startswith("#")), None)
        if uri and (not _int_attr(attrs.get("BANDWIDTH"))
                    or "AVERAGE-BANDWIDTH" not in attrs):
            rates = _playlist_bitrates(master_path.parent / uri)
            if rates:
                attrs["BANDWIDTH"] = str(rates[0])
                attrs["AVERAGE-BANDWIDTH"] = str(rates[1])
                fixed.add("bandwidth measured from segments")
        video_codec = (video_codecs[variant_no]
                       if video_codecs and variant_no < len(video_codecs) else None)
        if video_codec and "CODECS" in attrs:
            codecs, changed = _fix_codecs_attr(attrs["CODECS"].strip('"'),
                                               video_codec)
            if changed:
                attrs["CODECS"] = f'"{codecs}"'
                fixed.add("hevc codec strings completed")
        if audio_rates and attrs.get("AUDIO"):
            attrs["BANDWIDTH"] = str(_int_attr(attrs.get("BANDWIDTH")) + audio_rates[0])
            if "AVERAGE-BANDWIDTH" in attrs:
                attrs["AVERAGE-BANDWIDTH"] = str(
                    _int_attr(attrs["AVERAGE-BANDWIDTH"]) + audio_rates[1])
            fixed.add("audio rendition included in bandwidth")
        if audio_codec and attrs.get("AUDIO") and audio_codec not in attrs.get("CODECS", ""):
            codecs = attrs.get("CODECS", "").strip('"')
            attrs["CODECS"] = f'"{codecs},{audio_codec}"' if codecs else f'"{audio_codec}"'
            fixed.add("audio codec appended to CODECS")
        if fps and "FRAME-RATE" not in attrs:
            attrs["FRAME-RATE"] = f"{float(fps):.3f}"
            fixed.add("FRAME-RATE added")
        out.append("#EXT-X-STREAM-INF:"
                   + ",".join(f"{k}={v}" for k, v in attrs.items()))

    if not any(l.strip() == "#EXT-X-INDEPENDENT-SEGMENTS" for l in out):
        pos = next((i + 1 for i, l in enumerate(out)
                    if l.startswith("#EXT-X-VERSION")), 1)
        out.insert(pos, "#EXT-X-INDEPENDENT-SEGMENTS")
        fixed.add("EXT-X-INDEPENDENT-SEGMENTS added to master")

    if fixed:
        out.append(f"# pqloop-fixup ({__version__}): " + "; ".join(sorted(fixed)))
        atomic_write_text(master_path, "\n".join(out) + "\n")
    return sorted(fixed)


def finalize_manifests(fmt, out_dir, fps=None, audio_codec=None,
                       video_codecs=None, ff=None, probe_path=None) -> list:
    """Apply the same strict manifest fixups to direct and ABR outputs."""
    out_dir = Path(out_dir)
    if video_codecs is None and ff is not None and probe_path is not None:
        video_codecs = [video_codec_string(ff, probe_path)]
    video_codecs = list(video_codecs or [])
    fixed = []
    if fmt in ("hls", "cmaf"):
        fixed += fixup_master(out_dir / "master.m3u8", fps=fps,
                              audio_codec=audio_codec,
                              video_codecs=video_codecs)
    if fmt in ("dash", "cmaf"):
        fixed += fixup_mpd(out_dir / "manifest.mpd", video_codecs)
    return fixed


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def stream_report(rungs, video_paths):
    """([report line, ...], [warning, ...]) — codec/profile/level per rung,
    warning when H.264 level exceeds the common 4.1 device ceiling."""
    lines, warnings = [], []
    for r, path in zip(rungs, video_paths):
        try:
            streams = r.ff.probe(path).get("streams", [])
        except FFmpegError:
            continue
        st = next((s for s in streams if s.get("codec_type") == "video"), {})
        codec = st.get("codec_name", "?")
        level = st.get("level") or 0
        level_txt = (f"{level / 10:g}" if codec == "h264" else
                     f"{level / 30:g}" if codec == "hevc" else str(level))
        lines.append(f"  {r.name:<12} {r.width}x{r.height} @ {r.target_kbps}k  "
                     f"{codec} {st.get('profile', '?')} L{level_txt}")
        if codec == "h264" and level > 41:
            hint = ("inherent to >=50fps at this resolution — check the "
                    "target devices support it"
                    if r.fps >= 50 and r.height >= 1080 else
                    "often too many reference frames; re-optimize with "
                    "--freeze refs=4, or package with --h264-level 4.1 to clamp")
            warnings.append(f"{r.name}: H.264 level {level_txt} exceeds 4.1, "
                            f"which some devices cap at ({hint})")
    return lines, warnings


def _frame_quantized(t, fps) -> float:
    """The first achievable frame time at or after t — where
    `-force_key_frames expr:gte(t,n*seg)` actually lands an IDR when a
    boundary falls between frames (fractional rates: 4s at 59.94fps is
    239.76 frames, so the keyframe sits at 4.004s). Integer rates return
    t unchanged."""
    if not fps or t <= 0:
        return t
    return math.ceil(t * fps - 1e-6) / fps


def verify_package(rungs, video_paths, seg_duration, log=None) -> list:
    """Cross-rung keyframe alignment from the intermediates (demux-only over
    the whole file), plus a bounded IDR spot-check over the first two
    segments (frame-level, so it decodes). Returns problem strings."""
    log = log or (lambda m: None)
    tol = 0.5 / rungs[0].fps if rungs[0].fps else 0.02
    keyframes = []
    problems = []
    for r, path in zip(rungs, video_paths):
        read_rows = getattr(r.ff, "iter_probe_entries", r.ff.probe_entries)
        rows = read_rows(path, "packet", "pts_time,flags", select="v:0")
        kfs = [float(d["pts_time"]) for d in rows if "K" in d.get("flags", "")]
        keyframes.append(kfs)
        window = 2 * float(seg_duration)
        frames = r.ff.probe_entries(path, "frame", "pts_time,pict_type,key_frame",
                                    select="v:0", read_intervals=f"%+{window:g}")
        # AVFrame key_frame + pict_type I ~= IDR for h264/hevc: without an IDR
        # on the boundary, independent_segments would be a lie (matters for
        # hardware encoders whose keyframe packets may be plain I-frames)
        idrs = [float(d["pts_time"]) for d in frames
                if d.get("key_frame") == "1" and d.get("pict_type") == "I"]
        for boundary in (0.0, float(seg_duration)):
            expected = _frame_quantized(boundary, r.fps)
            if boundary < window and not any(abs(t - expected) <= tol for t in idrs):
                problems.append(f"{r.name}: no IDR at segment boundary {boundary:g}s")

    base = keyframes[0]
    for r, kfs in zip(rungs[1:], keyframes[1:]):
        if len(kfs) != len(base):
            problems.append(f"{r.name}: {len(kfs)} keyframes vs "
                            f"{len(base)} in {rungs[0].name}")
            continue
        worst = max((abs(a - b) for a, b in zip(base, kfs)), default=0.0)
        if worst > tol:
            problems.append(f"{r.name}: keyframes deviate up to {worst:.3f}s "
                            f"from {rungs[0].name}")
    for r, kfs in zip(rungs, keyframes):
        end = max(kfs, default=0.0)
        missing = []
        n = 0
        while n * float(seg_duration) <= end + tol:
            t = _frame_quantized(n * float(seg_duration), r.fps)
            if not any(abs(k - t) <= tol for k in kfs):
                missing.append(f"{n * float(seg_duration):g}s")
            n += 1
        if missing:
            problems.append(f"{r.name}: no keyframe at segment boundaries "
                            + ", ".join(missing[:5])
                            + (" ..." if len(missing) > 5 else ""))
    return problems
