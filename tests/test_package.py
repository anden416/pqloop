import tempfile
import unittest
from pathlib import Path

from pqloop import package
from pqloop.cli import preset_params
from pqloop.encoders import get_space
from pqloop.ffmpeg import FFmpegError
from pqloop.media import SourceInfo


class FakeFF:
    def __init__(self, probe_result=None, fail_probe=False, entries=None):
        self.calls = []
        self._probe = probe_result if probe_result is not None else \
            {"streams": [{"codec_type": "video", "codec_name": "h264",
                          "profile": "High", "level": 40}]}
        self._fail_probe = fail_probe
        self._entries = entries or {}

    def run(self, args, timeout=None):
        self.calls.append(([str(a) for a in args], timeout))

    def probe(self, url):
        if self._fail_probe:
            raise FFmpegError("probe failed")
        return self._probe

    def probe_entries(self, url, section, entries, select=None,
                      read_intervals=None, timeout=600):
        return self._entries.get((Path(url).name, section), [])

    def version(self):
        return "fake-ffmpeg 1.0"


def _rung(name="a", encoder="libx264", kbps=3000, seg=4.0, height=1080,
          fps=50.0, gop=None, pix_fmt="yuv420p", ff=None):
    cfg = {"encoder": encoder, "target_bitrate_kbps": kbps, "seg_duration": seg,
           "gop_duration": gop, "pix_fmt": pix_fmt, "scale": "",
           "deinterlace": "off", "deint_mode": "field",
           "maxrate_ratio": 1.10, "bufsize_ratio": 2.0, "extra_video_args": []}
    r = package.Rung(preset_name=name, cfg=cfg, space=get_space(encoder),
                     params=get_space(encoder).defaults(), ff=ff or FakeFF())
    r.height, r.width, r.fps = height, round(height * 16 / 9), fps
    return r


def _source(path="in.mp4", has_audio=True):
    return SourceInfo(path=path, width=1920, height=1080, fps=50.0,
                      fps_str="50/1", field_order="progressive", duration=60.0,
                      has_audio=has_audio, video_codec="h264", pix_fmt="yuv420p")


class MuxArgsTest(unittest.TestCase):
    def _hls(self, audio=True, seg_type="fmp4"):
        args, out = package.mux_args(
            "hls", "out", ["v0.mp4", "v1.mp4"], ["720p", "1080p"],
            audio_path="audio.mp4" if audio else None,
            seg_duration=4.0, hls_segment_type=seg_type)
        return [str(a) for a in args], str(out)

    def test_hls_fmp4_with_audio_group(self):
        args, out = self._hls()
        vsm = args[args.index("-var_stream_map") + 1]
        self.assertEqual(vsm, "v:0,agroup:aud,name:720p "
                              "v:1,agroup:aud,name:1080p "
                              "a:0,agroup:aud,name:audio,default:yes")
        self.assertEqual(args[args.index("-c") + 1], "copy")
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "1:v:0", "2:a:0"])
        self.assertEqual(args[args.index("-hls_fmp4_init_filename") + 1],
                         "init_%v.mp4")
        self.assertIn("%v", args[args.index("-hls_segment_filename") + 1])
        self.assertTrue(args[-1].endswith("%v/index.m3u8"))
        self.assertTrue(out.endswith("master.m3u8"))
        self.assertEqual(args[args.index("-hls_list_size") + 1], "0")

    def test_hls_without_audio_has_no_agroup(self):
        args, _ = self._hls(audio=False)
        vsm = args[args.index("-var_stream_map") + 1]
        self.assertEqual(vsm, "v:0,name:720p v:1,name:1080p")
        self.assertNotIn("agroup", vsm)
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "1:v:0"])

    def test_hls_mpegts_segments(self):
        args, _ = self._hls(seg_type="mpegts")
        self.assertNotIn("-hls_fmp4_init_filename", args)
        self.assertTrue(args[args.index("-hls_segment_filename") + 1]
                        .endswith("seg_%05d.ts"))

    def test_dash_adaptation_sets(self):
        args, out = package.mux_args("dash", "out", ["v0.mp4"], ["1080p"],
                                     audio_path="audio.mp4")
        args = [str(a) for a in args]
        self.assertEqual(args[args.index("-adaptation_sets") + 1],
                         "id=0,streams=v id=1,streams=a")
        self.assertNotIn("-hls_playlist", args)
        self.assertTrue(str(out).endswith("manifest.mpd"))
        args, _ = package.mux_args("dash", "out", ["v0.mp4"], ["1080p"])
        self.assertEqual([str(a) for a in args][
            [str(a) for a in args].index("-adaptation_sets") + 1],
            "id=0,streams=v")

    def test_cmaf_adds_hls_master(self):
        args, _ = package.mux_args("cmaf", "out", ["v0.mp4"], ["1080p"],
                                   audio_path="audio.mp4")
        args = [str(a) for a in args]
        self.assertEqual(args[args.index("-hls_playlist") + 1], "1")
        self.assertEqual(args[args.index("-hls_master_name") + 1], "master.m3u8")

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            package.mux_args("webm", "out", ["v0.mp4"], ["1080p"])


class NamesAndValidationTest(unittest.TestCase):
    def test_names_from_height_with_bitrate_suffix_on_duplicates(self):
        rungs = [_rung("a", kbps=2800, height=720), _rung("b", kbps=6000, height=1080)]
        package.assign_names(rungs)
        self.assertEqual([r.name for r in rungs], ["720p", "1080p"])
        rungs = [_rung("a", kbps=2800, height=720), _rung("b", kbps=1800, height=720)]
        package.assign_names(rungs)
        self.assertEqual([r.name for r in rungs], ["720p-2800k", "720p-1800k"])

    def _valid_pair(self):
        rungs = [_rung("lo", kbps=2800, height=720), _rung("hi", kbps=6000, height=1080)]
        package.assign_names(rungs)
        return rungs

    def test_valid_ladder_passes_without_warnings(self):
        self.assertEqual(package.validate_rungs(self._valid_pair()), [])

    def test_seg_duration_mismatch_raises(self):
        rungs = self._valid_pair()
        rungs[0].cfg["seg_duration"] = 6.0
        with self.assertRaisesRegex(ValueError, "seg_duration"):
            package.validate_rungs(rungs)

    def test_codec_family_mismatch_raises(self):
        rungs = [_rung("lo", kbps=2800, height=720),
                 _rung("hi", encoder="libx265", kbps=6000, height=1080)]
        package.assign_names(rungs)
        with self.assertRaisesRegex(ValueError, "codec family"):
            package.validate_rungs(rungs)

    def test_fps_mismatch_raises(self):
        rungs = self._valid_pair()
        rungs[0].fps = 25.0
        with self.assertRaisesRegex(ValueError, "frame rate"):
            package.validate_rungs(rungs)

    def test_duplicate_bitrate_raises(self):
        rungs = [_rung("a", kbps=2800, height=720), _rung("b", kbps=2800, height=1080)]
        package.assign_names(rungs)
        with self.assertRaisesRegex(ValueError, "duplicate target bitrates"):
            package.validate_rungs(rungs)

    def test_gop_and_pix_fmt_mismatch_warn(self):
        rungs = self._valid_pair()
        rungs[0].cfg["gop_duration"] = 2.0
        rungs[1].cfg["pix_fmt"] = "yuv420p10le"
        warnings = package.validate_rungs(rungs)
        self.assertTrue(any("GOP duration" in w for w in warnings))
        self.assertTrue(any("pixel format" in w for w in warnings))

    def test_inverted_ladder_warns(self):
        rungs = [_rung("a", kbps=2800, height=1080), _rung("b", kbps=6000, height=720)]
        package.assign_names(rungs)
        warnings = package.validate_rungs(rungs)
        self.assertTrue(any("resolution order" in w for w in warnings))


class AudioArgsTest(unittest.TestCase):
    def test_audio_args_mirror_video_clip_window(self):
        args = package.audio_args(_source(), "out/audio.mp4", 128,
                                  start=10.0, duration=30.0)
        self.assertEqual(args[args.index("-ss") + 1], "10.000")
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertEqual(args[args.index("-t") + 1], "30.000")
        self.assertIn("-vn", args)
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-ac") + 1], "2")


class SidecarTest(unittest.TestCase):
    def test_build_encodes_then_reuses(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src_file = td / "in.mp4"
            src_file.write_bytes(b"x" * 4096)
            source = _source(path=str(src_file), has_audio=False)
            ff = FakeFF()
            rung = _rung("hi", kbps=6000, height=1080, ff=ff)
            rung.name = "1080p"
            work = td / "_work"

            out = package.build_intermediates([rung], source, work, log=lambda m: None)
            self.assertEqual(len(ff.calls), 1)
            self.assertTrue((work / "1080p.json").exists())
            self.assertIsNone(out["audio"])

            # FakeFF wrote nothing: without the output file there is no reuse
            package.build_intermediates([rung], source, work, log=lambda m: None)
            self.assertEqual(len(ff.calls), 2)

            # with the file present and the sidecar matching, the encode is skipped
            Path(out["video"][0]).parent.mkdir(parents=True, exist_ok=True)
            Path(out["video"][0]).write_bytes(b"mp4")
            package.build_intermediates([rung], source, work, log=lambda m: None)
            self.assertEqual(len(ff.calls), 2)

            # any parameter change invalidates the sidecar
            rung.params["subme"] = 11
            package.build_intermediates([rung], source, work, log=lambda m: None)
            self.assertEqual(len(ff.calls), 3)

    def test_unreadable_intermediate_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src_file = td / "in.mp4"
            src_file.write_bytes(b"x" * 4096)
            source = _source(path=str(src_file), has_audio=False)
            ff = FakeFF(fail_probe=True)
            rung = _rung("hi", kbps=6000, height=1080, ff=ff)
            rung.name = "1080p"
            work = td / "_work"
            out = package.build_intermediates([rung], source, work, log=lambda m: None)
            Path(out["video"][0]).parent.mkdir(parents=True, exist_ok=True)
            Path(out["video"][0]).write_bytes(b"truncated")
            package.build_intermediates([rung], source, work, log=lambda m: None)
            self.assertEqual(len(ff.calls), 2)


MASTER_FFMPEG = """\
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_aud",NAME="audio_2",DEFAULT=YES,CHANNELS="2",URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=10000,AVERAGE-BANDWIDTH=8000,RESOLUTION=640x360,CODECS="avc1.64001f",AUDIO="group_aud"
hi/index.m3u8
"""

MASTER_CMAF_STYLE = """\
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_A1",NAME="audio_2",DEFAULT=YES,URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=9999,RESOLUTION=640x360,CODECS="avc1.64001f",AUDIO="group_A1"
hi/index.m3u8
"""

MEDIA_PLAYLIST = """\
#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.000000,
seg_00000.m4s
#EXTINF:4.000000,
seg_00001.m4s
#EXT-X-ENDLIST
"""

AUDIO_PLAYLIST = """\
#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.000000,
seg_00000.m4s
#EXT-X-ENDLIST
"""


class FixupMasterTest(unittest.TestCase):
    def _package_dir(self, master_text):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        (base / "hi").mkdir()
        (base / "audio").mkdir()
        (base / "master.m3u8").write_text(master_text)
        (base / "hi" / "index.m3u8").write_text(MEDIA_PLAYLIST)
        # 4s segments: 5000 and 3000 bytes -> peak 10000 bps, avg 8000 bps
        (base / "hi" / "seg_00000.m4s").write_bytes(b"x" * 5000)
        (base / "hi" / "seg_00001.m4s").write_bytes(b"x" * 3000)
        # one 4s audio segment of 500 bytes -> 1000 bps peak and avg
        (base / "audio" / "index.m3u8").write_text(AUDIO_PLAYLIST)
        (base / "audio" / "seg_00000.m4s").write_bytes(b"x" * 500)
        self.addCleanup(td.cleanup)
        return base

    def _stream_inf(self, base):
        line = next(l for l in (base / "master.m3u8").read_text().splitlines()
                    if l.startswith("#EXT-X-STREAM-INF:"))
        return package._parse_attrs(line[len("#EXT-X-STREAM-INF:"):])

    def test_fixup_measured_master_adds_audio_fps_and_tag(self):
        base = self._package_dir(MASTER_FFMPEG)
        fixed = package.fixup_master(base / "master.m3u8", fps=50.0,
                                     audio_codec="mp4a.40.2")
        attrs = self._stream_inf(base)
        self.assertEqual(attrs["BANDWIDTH"], "11000")           # 10000 + 1000 audio
        self.assertEqual(attrs["AVERAGE-BANDWIDTH"], "9000")    # 8000 + 1000 audio
        self.assertEqual(attrs["CODECS"], '"avc1.64001f,mp4a.40.2"')
        self.assertEqual(attrs["FRAME-RATE"], "50.000")
        text = (base / "master.m3u8").read_text()
        self.assertIn("#EXT-X-INDEPENDENT-SEGMENTS\n", text)
        self.assertTrue(fixed)

    def test_fixup_recomputes_average_less_master_from_segments(self):
        base = self._package_dir(MASTER_CMAF_STYLE)
        package.fixup_master(base / "master.m3u8", fps=50.0,
                             audio_codec="mp4a.40.2")
        attrs = self._stream_inf(base)
        self.assertEqual(attrs["BANDWIDTH"], "11000")
        self.assertEqual(attrs["AVERAGE-BANDWIDTH"], "9000")

    def test_fixup_is_idempotent(self):
        base = self._package_dir(MASTER_FFMPEG)
        package.fixup_master(base / "master.m3u8", fps=50.0, audio_codec="mp4a.40.2")
        first = (base / "master.m3u8").read_text()
        self.assertEqual(package.fixup_master(base / "master.m3u8", fps=50.0,
                                              audio_codec="mp4a.40.2"), [])
        self.assertEqual((base / "master.m3u8").read_text(), first)

    def test_attr_parser_keeps_quoted_commas(self):
        attrs = package._parse_attrs('BANDWIDTH=1,CODECS="a,b",AUDIO="g"')
        self.assertEqual(attrs, {"BANDWIDTH": "1", "CODECS": '"a,b"',
                                 "AUDIO": '"g"'})

    def test_bare_hevc_codecs_completed_valid_strings_untouched(self):
        # the dash muxer's CMAF master carries a bare (spec-invalid) hvc1
        base = self._package_dir(MASTER_FFMPEG.replace(
            'CODECS="avc1.64001f"', 'CODECS="hvc1"'))
        fixed = package.fixup_master(base / "master.m3u8", fps=50.0,
                                     audio_codec="mp4a.40.2",
                                     video_codecs=["hvc1.1.6.L123.B0"])
        self.assertIn("hevc codec strings completed", fixed)
        self.assertEqual(self._stream_inf(base)["CODECS"],
                         '"hvc1.1.6.L123.B0,mp4a.40.2"')
        # already-valid strings (hlsenc's avc1) are left alone
        base = self._package_dir(MASTER_FFMPEG)
        fixed = package.fixup_master(base / "master.m3u8", fps=50.0,
                                     audio_codec="mp4a.40.2",
                                     video_codecs=["hvc1.1.6.L123.B0"])
        self.assertNotIn("hevc codec strings completed", fixed)
        self.assertEqual(self._stream_inf(base)["CODECS"],
                         '"avc1.64001f,mp4a.40.2"')


class Rfc6381HevcTest(unittest.TestCase):
    def test_profiles_and_tiers(self):
        self.assertEqual(package.rfc6381_hevc("Main", 123), "hvc1.1.6.L123.B0")
        self.assertEqual(package.rfc6381_hevc("Main 10", 120, "High"),
                         "hvc1.2.4.H120.B0")

    def test_unknown_profile_or_level_yields_none(self):
        self.assertIsNone(package.rfc6381_hevc("Rext", 123))
        self.assertIsNone(package.rfc6381_hevc("Main", 0))

    def test_strings_built_from_probed_intermediates(self):
        hevc_ff = FakeFF(probe_result={"streams": [
            {"codec_type": "video", "codec_name": "hevc",
             "profile": "Main", "level": 123}]})
        h264_ff = FakeFF()   # default probe: h264 High L40
        rungs = [_rung(name="a", encoder="libx265", ff=hevc_ff),
                 _rung(name="b", kbps=1000, ff=h264_ff)]
        self.assertEqual(
            package.video_codec_strings(rungs, ["a.mp4", "b.mp4"]),
            ["hvc1.1.6.L123.B0", None])


MPD_BARE = """\
<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
<AdaptationSet id="0" contentType="video">
<Representation id="0" codecs="hvc1" bandwidth="4500000"/>
<Representation id="1" codecs="hvc1" bandwidth="2400000"/>
</AdaptationSet>
<AdaptationSet id="1" contentType="audio">
<Representation id="2" codecs="mp4a.40.2" bandwidth="128000"/>
</AdaptationSet>
</MPD>
"""


class FixupMpdTest(unittest.TestCase):
    def _mpd(self, text=MPD_BARE):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "manifest.mpd"
        path.write_text(text)
        return path

    def test_bare_codecs_rewritten_in_rung_order(self):
        path = self._mpd()
        fixed = package.fixup_mpd(path, ["hvc1.1.6.L123.B0",
                                         "hvc1.1.6.L120.B0"])
        self.assertEqual(fixed, ["hevc codec strings completed"])
        text = path.read_text()
        self.assertIn('codecs="hvc1.1.6.L123.B0" bandwidth="4500000"', text)
        self.assertIn('codecs="hvc1.1.6.L120.B0" bandwidth="2400000"', text)
        self.assertIn('codecs="mp4a.40.2"', text)   # audio untouched

    def test_idempotent_and_shape_guarded(self):
        path = self._mpd()
        package.fixup_mpd(path, ["hvc1.1.6.L123.B0", "hvc1.1.6.L120.B0"])
        first = path.read_text()
        self.assertEqual(package.fixup_mpd(
            path, ["hvc1.1.6.L123.B0", "hvc1.1.6.L120.B0"]), [])
        self.assertEqual(path.read_text(), first)
        # count mismatch (or an avc1 manifest) -> untouched
        path = self._mpd()
        self.assertEqual(package.fixup_mpd(path, ["hvc1.1.6.L123.B0"]), [])
        self.assertEqual(path.read_text(), MPD_BARE)
        self.assertEqual(package.fixup_mpd(self._mpd(), [None, None]), [])


class ResolveOutputNormTest(unittest.TestCase):
    def test_scaleless_rung_takes_the_normalized_dimensions(self):
        rung = _rung()
        rung.cfg["norm_scale"] = "1920x1080"
        src = _source()
        src.width, src.height = 3840, 2160
        package.resolve_output(rung, src)
        self.assertEqual((rung.width, rung.height), (1920, 1080))
        package.assign_names([rung])
        self.assertEqual(rung.name, "1080p")

    def test_explicit_rung_scale_still_wins(self):
        rung = _rung()
        rung.cfg["norm_scale"] = "1920x1080"
        rung.cfg["scale"] = "1280x720"
        package.resolve_output(rung, _source())
        self.assertEqual((rung.width, rung.height), (1280, 720))


class VerifyTest(unittest.TestCase):
    def _entries(self, kfs, idrs):
        packets = [{"pts_time": f"{t:.6f}",
                    "flags": "K__" if t in kfs else "___"}
                   for t in sorted(set(kfs) | {0.02, 1.5, 5.5})]
        frames = [{"pts_time": f"{t:.6f}", "pict_type": "I", "key_frame": "1"}
                  for t in idrs]
        return packets, frames

    def _rung_with(self, name, kfs, idrs):
        packets, frames = self._entries(kfs, idrs)
        ff = FakeFF(entries={(f"{name}.mp4", "packet"): packets,
                             (f"{name}.mp4", "frame"): frames})
        r = _rung(name, ff=ff)
        r.name = name
        return r

    def test_aligned_rungs_pass(self):
        a = self._rung_with("a", [0.0, 4.0, 8.0], [0.0, 4.0])
        b = self._rung_with("b", [0.0, 4.0, 8.0], [0.0, 4.0])
        problems = package.verify_package([a, b], ["a.mp4", "b.mp4"], 4.0)
        self.assertEqual(problems, [])

    def test_misaligned_keyframes_reported(self):
        a = self._rung_with("a", [0.0, 4.0, 8.0], [0.0, 4.0])
        b = self._rung_with("b", [0.0, 4.2, 8.0], [0.0, 4.2])
        problems = package.verify_package([a, b], ["a.mp4", "b.mp4"], 4.0)
        self.assertTrue(any("deviate" in p for p in problems))

    def test_missing_boundary_idr_reported(self):
        a = self._rung_with("a", [0.0, 4.0, 8.0], [0.0])
        problems = package.verify_package([a], ["a.mp4"], 4.0)
        self.assertTrue(any("no IDR at segment boundary 4s" in p for p in problems))

    def test_ntsc_rate_boundaries_compare_frame_quantized(self):
        # at 59.94fps a 4s boundary has no frame on it: -force_key_frames
        # gte(t,n*4) lands the IDR on the first frame at/after the boundary
        # (4.004, 8.008, 12.012, ...) — that IS the aligned position, not a
        # miss. Times below are the real x265 output pattern.
        fps = 60000 / 1001
        kfs = [package._frame_quantized(4.0 * n, fps) for n in range(8)]
        a = self._rung_with("a", kfs, kfs[:2])
        b = self._rung_with("b", kfs, kfs[:2])
        for r in (a, b):
            r.fps = fps
        problems = package.verify_package([a, b], ["a.mp4", "b.mp4"], 4.0)
        self.assertEqual(problems, [])

    def test_frame_quantized_is_identity_for_integer_rates(self):
        for n in range(10):
            self.assertEqual(package._frame_quantized(4.0 * n, 50.0), 4.0 * n)
        self.assertAlmostEqual(
            package._frame_quantized(12.0, 60000 / 1001), 720 * 1001 / 60000)
        self.assertAlmostEqual(
            package._frame_quantized(20.0, 60000 / 1001), 1199 * 1001 / 60000)


class PresetParamsTest(unittest.TestCase):
    def test_best_params_win(self):
        space = get_space("libx264")
        data = {"best": {"params": {"subme": 9}},
                "optimizer": {"current": {"subme": 7}}}
        self.assertEqual(preset_params(data, space, log_fn=lambda m: None),
                         {"subme": 9})

    def test_falls_back_to_current_then_defaults(self):
        space = get_space("libx264")
        notes = []
        params = preset_params({"optimizer": {"current": {"subme": 8, "merange": 32}}},
                               space, log_fn=notes.append)
        self.assertEqual(params.get("subme"), 8)
        self.assertNotIn("merange", params)   # inert while me != umh
        self.assertTrue(any("current search point" in n for n in notes))
        params = preset_params({}, space, log_fn=notes.append)
        self.assertEqual(params["preset"], "medium")
        self.assertTrue(any("encoder defaults" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
