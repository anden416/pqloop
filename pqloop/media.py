"""Input handling: probing, live capture, and mezzanine (reference) creation.

The optimization loop never touches the original input per trial. Instead a
short clip is extracted once into a lossless *mezzanine* (x264 qp=0, yuv420p,
CFR, deinterlaced if requested/detected, and — for HDR/wide-gamut/oversized
masters — normalized to SDR bt709 at the delivery resolution via
`normalization_filters`). Every trial encodes the mezzanine and VMAF compares
against it, so all trials see byte-identical reference frames and the
deinterlace/normalization cost is paid once, not per iteration.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .util import parse_fps, fingerprint_file, atomic_write_json, load_json, now_iso

LIVE_SCHEMES = ("udp", "rtp", "srt", "rist")
INTERLACED_ORDERS = ("tt", "bb", "tb", "bt")
HDR_TRCS = ("smpte2084", "arib-std-b67")   # PQ, HLG
TONEMAP_MODES = ("auto", "off", "hable", "mobius", "reinhard",
                 "clip", "gamma", "linear")


def is_live_url(url) -> bool:
    s = str(url)
    return any(s.startswith(scheme + "://") or s.startswith(scheme + ":@")
               for scheme in LIVE_SCHEMES)


def _localname(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_root_localname(path) -> str:
    try:
        for _event, elem in ET.iterparse(str(path), events=("start",)):
            return _localname(elem.tag)
    except (ET.ParseError, OSError):
        pass
    return ""


def resolve_input(path) -> str:
    """IMF package directory -> the composition playlist inside it (ffmpeg's
    imf demuxer takes the CPL XML as its input and pulls the video/audio MXFs
    from the same directory). Anything that is not a directory passes through
    untouched, so callers can resolve unconditionally."""
    if is_live_url(path) or not Path(path).is_dir():
        return str(path)
    d = Path(path)
    candidates = []
    assetmap = d / "ASSETMAP.xml"
    if assetmap.exists():
        try:
            root = ET.parse(str(assetmap)).getroot()
        except (ET.ParseError, OSError) as exc:
            raise ValueError(f"{d}: cannot parse ASSETMAP.xml: {exc}")
        for asset in root.iter():
            if _localname(asset.tag) != "Asset":
                continue
            # the packing list is itself an .xml asset; it is the one asset
            # type ASSETMAP marks explicitly, so it can be skipped cheaply
            if any(_localname(c.tag) == "PackingList"
                   and (c.text or "").strip().lower() == "true" for c in asset):
                continue
            for el in asset.iter():
                if _localname(el.tag) == "Path":
                    rel = (el.text or "").strip()
                    if rel.lower().endswith(".xml") and (d / rel).exists():
                        candidates.append(d / rel)
    if not candidates:
        candidates = sorted(d.glob("CPL_*.xml"))
    cpls = [c for c in candidates
            if _xml_root_localname(c) == "CompositionPlaylist"]
    if len(cpls) == 1:
        return str(cpls[0])
    if not cpls:
        raise ValueError(
            f"{d} is a directory with no composition playlist — not an IMF "
            f"package? Pass a media file (or the CPL XML) directly")
    raise ValueError(
        f"{d} contains multiple CPLs ({', '.join(c.name for c in cpls)}) — "
        f"pass the one to use as the input")


@dataclass
class SourceInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_str: str
    field_order: str
    duration: float
    has_audio: bool
    video_codec: str
    pix_fmt: str
    program: int = None        # MPTS program the streams below were picked from
    video_index: int = None    # absolute input stream index of the probed video
    audio_index: int = None    # absolute input stream index of the probed audio
    color_primaries: str = ""  # "" = absent/unknown in the container
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    bit_depth: int = 8
    is_rgb: bool = False
    audio_streams: list = field(default_factory=list)
    # [{"index", "channels", "layout", "codec"}, ...] in stream order

    @property
    def interlaced(self) -> bool:
        return self.field_order in INTERLACED_ORDERS

    def video_map(self) -> list:
        """ffmpeg -map arguments selecting the probed video stream, so encodes
        use the same stream the probe (and any --program selection) reported."""
        if self.video_index is None:
            return ["-map", "0:v:0"]
        return ["-map", f"0:{self.video_index}"]

    def audio_map(self) -> list:
        if self.audio_index is None:
            return ["-map", "0:a:0?"]
        return ["-map", f"0:{self.audio_index}"]


def probe_file(ff, path, program=None, audio_stream=None) -> SourceInfo:
    return parse_probe(ff.probe(path), path, program, audio_stream)


def _color_tag(value) -> str:
    v = str(value or "").strip()
    return "" if v in ("", "unknown", "unspecified") else v


def _pix_fmt_bits(pix_fmt) -> int:
    name = str(pix_fmt or "")
    for marker, bits in (("p16", 16), ("p14", 14), ("p12", 12), ("p10", 10),
                         ("p9", 9), ("48", 16), ("64", 16)):
        if marker in name:
            return bits
    return 8


def parse_probe(data, path, program=None, audio_stream=None) -> SourceInfo:
    streams = data.get("streams", [])
    if program is not None:
        for prog in data.get("programs", []):
            if prog.get("program_id") == program:
                streams = prog.get("streams") or streams
                break
        else:
            available = [p.get("program_id") for p in data.get("programs", [])]
            raise RuntimeError(f"program {program} not found in {path} "
                               f"(available: {available or 'none'})")
    video = None
    for stream in streams:
        if stream.get("codec_type") == "video" and stream.get("width"):
            video = stream
            break
    if video is None:
        raise RuntimeError(f"no video stream found in {path}")
    fps_str = video.get("avg_frame_rate") or video.get("r_frame_rate") or "25/1"
    if parse_fps(fps_str) <= 0:
        fps_str = video.get("r_frame_rate") or "25/1"
    duration = float(video.get("duration")
                     or data.get("format", {}).get("duration") or 0.0)
    audio_streams = [
        {"index": None if s.get("index") is None else int(s["index"]),
         "channels": int(s.get("channels") or 0),
         "layout": s.get("channel_layout", "") or "",
         "codec": s.get("codec_name", "") or ""}
        for s in streams if s.get("codec_type") == "audio"]
    if audio_stream is None:
        audio = audio_streams[0] if audio_streams else None
    else:
        idx = int(audio_stream)
        if idx < 0 or idx >= len(audio_streams):
            listing = ", ".join(
                f"#{i} {a['channels']}ch {a['layout'] or a['codec']}"
                for i, a in enumerate(audio_streams))
            raise RuntimeError(f"audio stream {idx} not found in {path} "
                               f"(available: {listing or 'none'})")
        audio = audio_streams[idx]
    pix_fmt = video.get("pix_fmt", "?")
    try:
        bit_depth = int(video.get("bits_per_raw_sample") or 0)
    except (TypeError, ValueError):
        bit_depth = 0
    return SourceInfo(
        path=str(path),
        width=int(video["width"]), height=int(video["height"]),
        fps=parse_fps(fps_str), fps_str=fps_str,
        field_order=video.get("field_order", "progressive") or "progressive",
        duration=duration, has_audio=audio is not None,
        video_codec=video.get("codec_name", "?"),
        pix_fmt=pix_fmt,
        program=program,
        video_index=None if video.get("index") is None else int(video["index"]),
        audio_index=None if audio is None or audio.get("index") is None
                    else int(audio["index"]),
        color_primaries=_color_tag(video.get("color_primaries")),
        color_transfer=_color_tag(video.get("color_transfer")),
        color_space=_color_tag(video.get("color_space")),
        color_range=_color_tag(video.get("color_range")),
        bit_depth=bit_depth or _pix_fmt_bits(pix_fmt),
        is_rgb=any(m in str(pix_fmt) for m in ("rgb", "bgr", "gbr")),
        audio_streams=audio_streams,
    )


def capture_live(ff, url, seconds, out_path, extra_input_args=(), program=None,
                 log=None) -> str:
    """Record a live (multicast/unicast UDP, RTP, SRT, RIST) stream to a local
    MPEG-TS file via stream copy. All trials then work from this recording.
    `program` selects one program out of a multi-program transport stream."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"capturing {seconds:.0f}s from {url} ...")
    ff.run(["-y", "-fflags", "+genpts", *extra_input_args, "-i", url,
            "-t", f"{seconds:.3f}",
            "-map", f"0:p:{program}" if program is not None else "0",
            "-c", "copy", "-f", "mpegts", str(out_path)],
           timeout=seconds * 4 + 120)
    return str(out_path)


def get_or_capture_live(ff, url, seconds, out_path, extra_input_args=(),
                        program=None, reuse=False, log=None) -> str:
    """Capture the live url, or — when `reuse` is set — keep an existing capture
    whose sidecar meta matches url/program/input-args and covers `seconds`.
    Reuse is what lets a live optimization resume on identical reference frames."""
    out_path = Path(out_path)
    meta_path = out_path.with_suffix(out_path.suffix + ".json")
    wanted = {"url": str(url), "program": program,
              "extra_input_args": list(extra_input_args)}
    if reuse and out_path.exists():
        meta = None
        if meta_path.exists():
            try:
                meta = load_json(meta_path)
            except (json.JSONDecodeError, OSError):
                meta = None
        if meta is None:
            if log:
                log(f"reusing capture {out_path} (no capture meta; assuming it matches)")
            return str(out_path)
        stale = [k for k, v in wanted.items() if meta.get(k) != v]
        if float(meta.get("seconds") or 0.0) < float(seconds):
            stale.append("seconds")
        if not stale:
            if log:
                log(f"reusing capture {out_path} ({meta.get('seconds'):g}s of {url})")
            return str(out_path)
        if log:
            log(f"recapturing ({', '.join(stale)} changed since last capture)")
    captured = capture_live(ff, url, seconds, out_path, extra_input_args,
                            program, log)
    atomic_write_json(meta_path, {**wanted, "seconds": float(seconds),
                                  "captured_at": now_iso()})
    return captured


def deinterlace_decision(source: SourceInfo, mode) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return source.interlaced


def build_filters(deinterlace: bool, deint_mode: str) -> list:
    filters = []
    if deinterlace:
        bwdif_mode = "send_field" if deint_mode == "field" else "send_frame"
        filters.append(f"bwdif=mode={bwdif_mode}")
    return filters


def _cfg_get(cfg, key, default=None):
    value = cfg.get(key, default) if isinstance(cfg, dict) else \
        getattr(cfg, key, default)
    return default if value is None else value


def effective_color(source: SourceInfo, cfg):
    """(primaries, transfer) the pipeline should treat the source as — the
    operator's --src-primaries/--src-trc assertion wins over container tags
    (masters like IMF J2K often carry no color metadata at all)."""
    prims = _cfg_get(cfg, "src_primaries") or source.color_primaries
    trc = _cfg_get(cfg, "src_trc") or source.color_transfer
    return prims, trc


def norm_engaged(source: SourceInfo, cfg) -> bool:
    """True when the HDR->SDR tonemap chain applies to this source."""
    if _cfg_get(cfg, "tonemap", "auto") == "off":
        return False
    return effective_color(source, cfg)[1] in HDR_TRCS


def norm_dims(source: SourceInfo, cfg):
    """(width, height) after normalization — the delivery/reference
    resolution when --norm-scale is set, else the source's own."""
    ns = _cfg_get(cfg, "norm_scale") or ""
    if ns:
        w, _, h = str(ns).lower().partition("x")
        return int(w), int(h)
    return source.width, source.height


def normalization_filters(source: SourceInfo, cfg) -> list:
    """Source-normalization pre-chain: assert color tags, tone-map HDR to SDR
    BT.709, and resize to the delivery resolution. Applied wherever the
    original source is decoded (mezzanine build and final encode) so trials
    and deliverables see identical frames. Returns [] when nothing applies —
    existing SDR sources keep byte-identical commands."""
    filters = []
    prims, trc = effective_color(source, cfg)
    engaged = norm_engaged(source, cfg)
    if engaged and not prims:
        raise ValueError("HDR normalization needs the source primaries; "
                         "pass --src-primaries (e.g. bt2020)")
    # zscale refuses untagged input ("no path between colorspaces"), so the
    # asserted color is stamped on the frames first; RGB masters are full
    # range by definition, which swscale/zscale cannot infer either.
    if _cfg_get(cfg, "src_primaries") or _cfg_get(cfg, "src_trc") or engaged:
        params = []
        if prims:
            params.append(f"color_primaries={prims}")
        if trc:
            params.append(f"color_trc={trc}")
        if source.is_rgb or source.color_range == "pc":
            params.append("range=pc")
        if params:
            filters.append("setparams=" + ":".join(params))
    if engaged:
        op = _cfg_get(cfg, "tonemap", "auto")
        op = "hable" if op == "auto" else op
        # linearize (PQ absolute: 100 nits -> 1.0) -> float RGB -> bt709
        # primaries -> compress highlights -> re-encode as SDR bt709 video
        filters += ["zscale=t=linear:npl=100",
                    "format=gbrpf32le",
                    "zscale=p=bt709",
                    f"tonemap={op}:desat=0",
                    "zscale=t=bt709:m=bt709:r=tv"]
    if _cfg_get(cfg, "norm_scale"):
        w, h = norm_dims(source, cfg)
        # resize at high bit depth, before any 8-bit quantization downstream
        if (w, h) != (source.width, source.height) or engaged:
            filters.append(f"scale={w}:{h}:flags=lanczos")
    return filters


def output_fps(source_fps: float, source_fps_str: str,
               deinterlace: bool, deint_mode: str):
    """(fps float, fps rational string) after optional field-rate deinterlace."""
    if deinterlace and deint_mode == "field":
        if "/" in source_fps_str:
            num, den = source_fps_str.split("/", 1)
            return source_fps * 2, f"{int(num) * 2}/{den}"
        return source_fps * 2, str(source_fps * 2)
    return source_fps, source_fps_str


@dataclass
class MezzInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_str: str
    duration: float
    fingerprint: str
    deinterlaced: bool
    filters: str
    inputs_key: str


def _mezz_inputs_key(source: SourceInfo, start, duration, deinterlaced,
                     deint_mode, norm_filters=()) -> str:
    # Content-based (not mtime-based): a recaptured-but-identical live clip or a
    # file copied between servers keeps its key, so the mezzanine is reused.
    src = Path(source.path)
    ident = {
        "src": str(src), "src_fp": fingerprint_file(src),
        "start": round(float(start), 3), "duration": round(float(duration), 3),
        "deint": bool(deinterlaced), "deint_mode": deint_mode if deinterlaced else "",
    }
    # The content fingerprint covers the whole file, so two programs of one
    # MPTS share it — the selected program must be part of the identity. Only
    # added when set, so pre-existing single-program mezzanines keep their key.
    if source.program is not None:
        ident["program"] = int(source.program)
    # Normalization changes what the reference *is* (tonemap operator, asserted
    # color, delivery resolution) — same only-when-set rule as program.
    if norm_filters:
        ident["norm"] = ",".join(norm_filters)
    return json.dumps(ident, sort_keys=True)


def get_or_build_mezzanine(ff, source: SourceInfo, start, duration,
                           deint, deint_mode, out_path, norm_filters=(),
                           log=None) -> MezzInfo:
    out_path = Path(out_path)
    meta_path = out_path.with_suffix(out_path.suffix + ".json")
    deinterlaced = deinterlace_decision(source, deint)
    inputs_key = _mezz_inputs_key(source, start, duration, deinterlaced,
                                  deint_mode, norm_filters)

    if out_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if (meta.get("inputs_key") == inputs_key
                    and meta.get("fingerprint") == fingerprint_file(out_path)):
                if log:
                    log(f"reusing mezzanine {out_path}")
                return MezzInfo(**meta)
        except (json.JSONDecodeError, TypeError, OSError):
            pass

    filters = build_filters(deinterlaced, deint_mode)
    filters += list(norm_filters)
    filters.append("format=yuv420p")
    fps, fps_str = output_fps(source.fps, source.fps_str, deinterlaced, deint_mode)
    if log:
        what = f"bwdif {deint_mode} -> {fps:g}fps, " if deinterlaced else ""
        log(f"building lossless mezzanine ({what}{duration:g}s @ {start:g}s) ...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # bitexact muxing: no random SegmentUID/date, so rebuilding from unchanged
    # inputs reproduces the same bytes and the fingerprint (and with it the
    # optimizer's trial cache) survives a rebuild.
    ff.run(["-y", "-ss", f"{float(start):.3f}", "-t", f"{float(duration):.3f}",
            "-i", source.path,
            *source.video_map(),
            "-vf", ",".join(filters),
            "-an", "-sn", "-dn",
            "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
            "-r", fps_str, "-fflags", "+bitexact", "-f", "matroska",
            str(out_path)],
           timeout=max(600.0, duration * 30 + 300))

    probed = probe_file(ff, out_path)
    info = MezzInfo(
        path=str(out_path), width=probed.width, height=probed.height,
        fps=probed.fps, fps_str=probed.fps_str,
        duration=probed.duration or float(duration),
        fingerprint=fingerprint_file(out_path),
        deinterlaced=deinterlaced,
        filters=",".join(filters), inputs_key=inputs_key,
    )
    atomic_write_json(meta_path, asdict(info))
    return info
