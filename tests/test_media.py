import os
import tempfile
import unittest
from pathlib import Path

from pqloop import media
from pqloop.media import SourceInfo


def _source(path, field_order="tt"):
    return SourceInfo(path=str(path), width=1920, height=1080, fps=25.0,
                      fps_str="25/1", field_order=field_order, duration=10.0,
                      has_audio=True, video_codec="h264", pix_fmt="yuv420p")


class LiveUrlTest(unittest.TestCase):
    def test_schemes(self):
        for url in ("udp://@239.0.0.1:1234", "udp://10.0.0.1:1234",
                    "rtp://@239.0.0.1:5000", "srt://host:9000",
                    "rist://host:9000", "rtp:@239.0.0.1:5000"):
            self.assertTrue(media.is_live_url(url), url)
        for url in ("input/clip.ts", "/abs/clip.mp4", "http://host/x.m3u8",
                    "file://x.ts"):
            self.assertFalse(media.is_live_url(url), url)


class OutputFpsTest(unittest.TestCase):
    def test_field_mode_doubles_rational(self):
        self.assertEqual(media.output_fps(25.0, "25/1", True, "field"),
                         (50.0, "50/1"))
        self.assertEqual(media.output_fps(30000 / 1001, "30000/1001", True, "field"),
                         (2 * 30000 / 1001, "60000/1001"))

    def test_frame_mode_and_no_deint_keep_rate(self):
        self.assertEqual(media.output_fps(25.0, "25/1", True, "frame"),
                         (25.0, "25/1"))
        self.assertEqual(media.output_fps(25.0, "25/1", False, "field"),
                         (25.0, "25/1"))


class DeinterlaceDecisionTest(unittest.TestCase):
    def test_modes(self):
        interlaced = _source("x.ts", field_order="tt")
        progressive = _source("x.ts", field_order="progressive")
        self.assertTrue(media.deinterlace_decision(interlaced, "auto"))
        self.assertFalse(media.deinterlace_decision(progressive, "auto"))
        self.assertTrue(media.deinterlace_decision(progressive, "on"))
        self.assertFalse(media.deinterlace_decision(interlaced, "off"))


class MezzKeyTest(unittest.TestCase):
    def test_key_survives_mtime_change_but_not_content_change(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "capture.ts"
            f.write_bytes(b"same content")
            src = _source(f)
            key1 = media._mezz_inputs_key(src, 0, 20, True, "field")
            os.utime(f, (1, 1))
            self.assertEqual(media._mezz_inputs_key(src, 0, 20, True, "field"),
                             key1)
            f.write_bytes(b"different content")
            self.assertNotEqual(media._mezz_inputs_key(src, 0, 20, True, "field"),
                                key1)

    def test_key_tracks_clip_window_and_deint(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "capture.ts"
            f.write_bytes(b"content")
            src = _source(f)
            base = media._mezz_inputs_key(src, 0, 20, True, "field")
            self.assertNotEqual(media._mezz_inputs_key(src, 5, 20, True, "field"), base)
            self.assertNotEqual(media._mezz_inputs_key(src, 0, 30, True, "field"), base)
            self.assertNotEqual(media._mezz_inputs_key(src, 0, 20, False, "field"), base)

    def test_key_tracks_program_selection(self):
        # two programs of one MPTS share the file fingerprint, so the program
        # must change the key; program=None keeps the pre-existing key shape
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "mpts.ts"
            f.write_bytes(b"content")
            base = media._mezz_inputs_key(_source(f), 0, 20, True, "field")
            src_p1 = _source(f)
            src_p1.program = 1
            src_p2 = _source(f)
            src_p2.program = 2
            key_p1 = media._mezz_inputs_key(src_p1, 0, 20, True, "field")
            key_p2 = media._mezz_inputs_key(src_p2, 0, 20, True, "field")
            self.assertNotEqual(key_p1, base)
            self.assertNotEqual(key_p2, base)
            self.assertNotEqual(key_p1, key_p2)
            self.assertNotIn("program", base)


class FakeCaptureFF:
    def __init__(self):
        self.calls = []

    def run(self, args, timeout=None):
        args = [str(a) for a in args]
        self.calls.append(args)
        Path(args[-1]).write_bytes(b"ts")


class CaptureTest(unittest.TestCase):
    URL = "udp://@239.0.0.1:1234"

    def test_program_and_genpts_emission(self):
        with tempfile.TemporaryDirectory() as td:
            ff = FakeCaptureFF()
            media.capture_live(ff, self.URL, 10, Path(td) / "c.ts", program=3)
            args = ff.calls[0]
            self.assertEqual(args[args.index("-map") + 1], "0:p:3")
            self.assertIn("+genpts", args[args.index("-fflags") + 1])
            media.capture_live(ff, self.URL, 10, Path(td) / "c.ts")
            self.assertEqual(ff.calls[1][ff.calls[1].index("-map") + 1], "0")

    def test_reuse_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            ff = FakeCaptureFF()
            out = Path(td) / "c.ts"
            media.get_or_capture_live(ff, self.URL, 10, out, reuse=True)
            self.assertEqual(len(ff.calls), 1)
            # same request -> reused
            media.get_or_capture_live(ff, self.URL, 10, out, reuse=True)
            self.assertEqual(len(ff.calls), 1)
            # shorter need is covered by the existing capture
            media.get_or_capture_live(ff, self.URL, 8, out, reuse=True)
            self.assertEqual(len(ff.calls), 1)
            # longer need -> recapture
            media.get_or_capture_live(ff, self.URL, 20, out, reuse=True)
            self.assertEqual(len(ff.calls), 2)
            # program change -> recapture
            media.get_or_capture_live(ff, self.URL, 10, out, program=2, reuse=True)
            self.assertEqual(len(ff.calls), 3)
            # reuse off -> always recapture
            media.get_or_capture_live(ff, self.URL, 10, out, program=2, reuse=False)
            self.assertEqual(len(ff.calls), 4)

    def test_reuse_without_meta_trusts_existing_capture(self):
        with tempfile.TemporaryDirectory() as td:
            ff = FakeCaptureFF()
            out = Path(td) / "c.ts"
            out.write_bytes(b"pre-upgrade capture")
            media.get_or_capture_live(ff, self.URL, 10, out, reuse=True)
            self.assertEqual(len(ff.calls), 0)


class FakeProbeFF:
    def __init__(self, data):
        self.data = data

    def probe(self, path):
        return self.data


class ProbeProgramTest(unittest.TestCase):
    def _data(self):
        vid1 = {"index": 0, "codec_type": "video", "width": 1280, "height": 720,
                "avg_frame_rate": "25/1", "codec_name": "h264",
                "pix_fmt": "yuv420p", "field_order": "progressive"}
        vid2 = {"index": 2, "codec_type": "video", "width": 1920, "height": 1080,
                "avg_frame_rate": "50/1", "codec_name": "h264",
                "pix_fmt": "yuv420p", "field_order": "progressive"}
        aud = {"index": 3, "codec_type": "audio"}
        return {
            "programs": [
                {"program_id": 1, "streams": [vid1]},
                {"program_id": 2, "streams": [vid2, aud]},
            ],
            "streams": [vid1, vid2, aud],
            "format": {"duration": "10.0"},
        }

    def test_program_selects_its_video_stream(self):
        src = media.probe_file(FakeProbeFF(self._data()), "mpts.ts", program=2)
        self.assertEqual((src.width, src.height), (1920, 1080))
        self.assertTrue(src.has_audio)
        self.assertEqual(src.program, 2)
        self.assertEqual(src.video_index, 2)
        self.assertEqual(src.audio_index, 3)
        self.assertEqual(src.video_map(), ["-map", "0:2"])
        self.assertEqual(src.audio_map(), ["-map", "0:3"])

    def test_default_is_first_video_stream(self):
        src = media.probe_file(FakeProbeFF(self._data()), "mpts.ts")
        self.assertEqual((src.width, src.height), (1280, 720))
        self.assertIsNone(src.program)
        self.assertEqual(src.video_index, 0)

    def test_map_falls_back_when_indexes_unknown(self):
        src = _source("x.ts")
        self.assertEqual(src.video_map(), ["-map", "0:v:0"])
        self.assertEqual(src.audio_map(), ["-map", "0:a:0?"])

    def test_missing_program_raises(self):
        with self.assertRaises(RuntimeError):
            media.probe_file(FakeProbeFF(self._data()), "mpts.ts", program=7)


class FakeMezzFF:
    """run() writes the mezzanine file; probe() describes the result."""

    def __init__(self):
        self.calls = []

    def run(self, args, timeout=None):
        args = [str(a) for a in args]
        self.calls.append(args)
        Path(args[-1]).write_bytes(b"mezz")

    def probe(self, path):
        return {"streams": [{"index": 0, "codec_type": "video", "width": 1920,
                             "height": 1080, "avg_frame_rate": "50/1"}],
                "format": {"duration": "20.0"}}


class MezzanineMapTest(unittest.TestCase):
    def test_build_maps_the_selected_program_stream(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "mpts.ts"
            f.write_bytes(b"mpts content")
            src = _source(f)
            src.program, src.video_index = 2, 4
            ff = FakeMezzFF()
            media.get_or_build_mezzanine(ff, src, 0, 20, "off", "field",
                                         Path(td) / "mezz.mkv")
            args = ff.calls[0]
            self.assertEqual(args[args.index("-map") + 1], "0:4")

    def test_norm_filters_slot_between_deint_and_yuv420p(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "src.mxf"
            f.write_bytes(b"master content")
            src = _source(f, field_order="progressive")
            ff = FakeMezzFF()
            media.get_or_build_mezzanine(
                ff, src, 0, 20, "off", "field", Path(td) / "mezz.mkv",
                norm_filters=["setparams=color_trc=smpte2084",
                              "scale=1920:1080:flags=lanczos"])
            args = ff.calls[0]
            self.assertEqual(args[args.index("-vf") + 1],
                             "setparams=color_trc=smpte2084,"
                             "scale=1920:1080:flags=lanczos,format=yuv420p")


ASSETMAP_NS = "http://www.smpte-ra.org/schemas/429-9/2007/AM"


def _write_imf(d, cpls=("CPL_a.xml",), with_assetmap=True):
    d = Path(d)
    assets = []
    for name in cpls:
        (d / name).write_text(
            '<?xml version="1.0"?>'
            '<CompositionPlaylist xmlns="http://www.smpte-ra.org/schemas/'
            '2067-3/2013"><Id>urn:uuid:1</Id></CompositionPlaylist>')
        assets.append(f"<Asset><Id>urn:uuid:{name}</Id><ChunkList><Chunk>"
                      f"<Path>{name}</Path></Chunk></ChunkList></Asset>")
    (d / "OPL_x.xml").write_text(
        '<?xml version="1.0"?><OutputProfileList/>')
    (d / "PKL_x.xml").write_text(
        '<?xml version="1.0"?><PackingList/>')
    (d / "video.mxf").write_bytes(b"mxf")
    assets.append("<Asset><Id>urn:uuid:opl</Id><ChunkList><Chunk>"
                  "<Path>OPL_x.xml</Path></Chunk></ChunkList></Asset>")
    assets.append("<Asset><Id>urn:uuid:pkl</Id><PackingList>true</PackingList>"
                  "<ChunkList><Chunk><Path>PKL_x.xml</Path></Chunk>"
                  "</ChunkList></Asset>")
    assets.append("<Asset><Id>urn:uuid:mxf</Id><ChunkList><Chunk>"
                  "<Path>video.mxf</Path></Chunk></ChunkList></Asset>")
    if with_assetmap:
        (d / "ASSETMAP.xml").write_text(
            f'<?xml version="1.0"?><AssetMap xmlns="{ASSETMAP_NS}">'
            f'<AssetList>{"".join(assets)}</AssetList></AssetMap>')


class ResolveInputTest(unittest.TestCase):
    def test_directory_resolves_to_the_cpl_via_assetmap(self):
        with tempfile.TemporaryDirectory() as td:
            _write_imf(td)
            self.assertEqual(media.resolve_input(td),
                             str(Path(td) / "CPL_a.xml"))

    def test_glob_fallback_without_assetmap(self):
        with tempfile.TemporaryDirectory() as td:
            _write_imf(td, with_assetmap=False)
            self.assertEqual(media.resolve_input(td),
                             str(Path(td) / "CPL_a.xml"))

    def test_files_and_live_urls_pass_through(self):
        self.assertEqual(media.resolve_input("input/clip.ts"), "input/clip.ts")
        self.assertEqual(media.resolve_input("udp://@239.0.0.1:1234"),
                         "udp://@239.0.0.1:1234")

    def test_multiple_cpls_and_no_cpl_raise(self):
        with tempfile.TemporaryDirectory() as td:
            _write_imf(td, cpls=("CPL_a.xml", "CPL_b.xml"))
            with self.assertRaisesRegex(ValueError, "multiple CPLs"):
                media.resolve_input(td)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "notes.txt").write_text("not a package")
            with self.assertRaisesRegex(ValueError, "no composition playlist"):
                media.resolve_input(td)


class ProbeColorTest(unittest.TestCase):
    def _data(self, **video_over):
        video = {"index": 0, "codec_type": "video", "width": 3840,
                 "height": 2160, "avg_frame_rate": "60000/1001",
                 "codec_name": "jpeg2000", "pix_fmt": "rgb48le",
                 "field_order": "progressive"}
        video.update(video_over)
        return {"streams": [video], "format": {"duration": "719.0"}}

    def test_captures_color_tags_and_bit_depth(self):
        src = media.parse_probe(self._data(
            color_primaries="bt2020", color_transfer="smpte2084",
            color_space="unknown", color_range="pc",
            bits_per_raw_sample="12"), "m.mxf")
        self.assertEqual(src.color_primaries, "bt2020")
        self.assertEqual(src.color_transfer, "smpte2084")
        self.assertEqual(src.color_space, "")       # unknown normalizes away
        self.assertEqual(src.color_range, "pc")
        self.assertEqual(src.bit_depth, 12)
        self.assertTrue(src.is_rgb)

    def test_bit_depth_falls_back_to_pix_fmt(self):
        self.assertEqual(media.parse_probe(self._data(), "m.mxf").bit_depth, 16)
        self.assertEqual(media.parse_probe(
            self._data(pix_fmt="yuv420p10le"), "m.mxf").bit_depth, 10)
        src = media.parse_probe(self._data(pix_fmt="yuv420p"), "m.mxf")
        self.assertEqual(src.bit_depth, 8)
        self.assertFalse(src.is_rgb)


class AudioStreamsTest(unittest.TestCase):
    def _data(self):
        return {"streams": [
            {"index": 0, "codec_type": "video", "width": 3840, "height": 2160,
             "avg_frame_rate": "60000/1001", "codec_name": "jpeg2000",
             "pix_fmt": "rgb48le"},
            {"index": 1, "codec_type": "audio", "channels": 6,
             "channel_layout": "5.1(side)", "codec_name": "pcm_s24le"},
            {"index": 2, "codec_type": "audio", "channels": 2,
             "channel_layout": "stereo", "codec_name": "pcm_s24le"},
        ], "format": {"duration": "719.0"}}

    def test_all_streams_listed_default_first(self):
        src = media.parse_probe(self._data(), "m.mxf")
        self.assertEqual([a["channels"] for a in src.audio_streams], [6, 2])
        self.assertEqual(src.audio_index, 1)
        self.assertEqual(src.audio_map(), ["-map", "0:1"])

    def test_audio_stream_selects_by_ordinal(self):
        src = media.parse_probe(self._data(), "m.mxf", audio_stream=1)
        self.assertEqual(src.audio_index, 2)
        self.assertEqual(src.audio_map(), ["-map", "0:2"])
        self.assertTrue(src.has_audio)

    def test_out_of_range_raises_with_listing(self):
        with self.assertRaisesRegex(RuntimeError, r"#0 6ch 5.1\(side\)"):
            media.parse_probe(self._data(), "m.mxf", audio_stream=2)


def _hdr_source(**over):
    src = SourceInfo(path="m.mxf", width=3840, height=2160, fps=59.94,
                     fps_str="60000/1001", field_order="progressive",
                     duration=719.0, has_audio=True, video_codec="jpeg2000",
                     pix_fmt="rgb48le", bit_depth=12, is_rgb=True)
    for key, value in over.items():
        setattr(src, key, value)
    return src


MERIDIAN_CFG = {"src_primaries": "bt2020", "src_trc": "smpte2084",
                "norm_scale": "1920x1080"}


class NormalizationFiltersTest(unittest.TestCase):
    def test_sdr_source_with_defaults_is_a_noop(self):
        src = _source("in.ts", field_order="progressive")
        self.assertEqual(media.normalization_filters(src, {}), [])
        self.assertFalse(media.norm_engaged(src, {}))

    def test_asserted_hdr_master_gets_the_full_chain(self):
        self.assertEqual(
            media.normalization_filters(_hdr_source(), MERIDIAN_CFG),
            ["setparams=color_primaries=bt2020:color_trc=smpte2084:range=pc",
             "zscale=t=linear:npl=100",
             "format=gbrpf32le",
             "zscale=p=bt709",
             "tonemap=hable:desat=0",
             "zscale=t=bt709:m=bt709:r=tv",
             "scale=1920:1080:flags=lanczos"])

    def test_probed_pq_tags_engage_automatically(self):
        src = _hdr_source(is_rgb=False, pix_fmt="yuv420p10le", bit_depth=10,
                          color_primaries="bt2020", color_transfer="smpte2084")
        filters = media.normalization_filters(src, {})
        self.assertTrue(media.norm_engaged(src, {}))
        self.assertIn("setparams=color_primaries=bt2020:color_trc=smpte2084",
                      filters)
        self.assertIn("tonemap=hable:desat=0", filters)

    def test_tonemap_off_disables_the_chain_but_keeps_asserts_and_scale(self):
        cfg = dict(MERIDIAN_CFG, tonemap="off")
        filters = media.normalization_filters(_hdr_source(), cfg)
        self.assertEqual(filters, [
            "setparams=color_primaries=bt2020:color_trc=smpte2084:range=pc",
            "scale=1920:1080:flags=lanczos"])

    def test_explicit_operator_replaces_hable(self):
        cfg = dict(MERIDIAN_CFG, tonemap="mobius")
        self.assertIn("tonemap=mobius:desat=0",
                      media.normalization_filters(_hdr_source(), cfg))

    def test_engaged_without_primaries_raises(self):
        with self.assertRaisesRegex(ValueError, "src-primaries"):
            media.normalization_filters(_hdr_source(),
                                        {"src_trc": "smpte2084"})

    def test_norm_scale_alone_downscales_sdr_sources(self):
        src = _source("in.ts", field_order="progressive")
        src.width, src.height = 3840, 2160
        self.assertEqual(media.normalization_filters(
            src, {"norm_scale": "1920x1080"}),
            ["scale=1920:1080:flags=lanczos"])
        # already at the target and not tonemapping -> nothing to do
        src.width, src.height = 1920, 1080
        self.assertEqual(media.normalization_filters(
            src, {"norm_scale": "1920x1080"}), [])

    def test_norm_dims(self):
        src = _hdr_source()
        self.assertEqual(media.norm_dims(src, MERIDIAN_CFG), (1920, 1080))
        self.assertEqual(media.norm_dims(src, {}), (3840, 2160))


class MezzKeyNormTest(unittest.TestCase):
    def test_norm_filters_change_the_key_only_when_set(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "src.mxf"
            f.write_bytes(b"content")
            src = _source(f)
            base = media._mezz_inputs_key(src, 0, 20, False, "field")
            self.assertNotIn("norm", base)   # legacy keys keep their shape
            normed = media._mezz_inputs_key(
                src, 0, 20, False, "field",
                norm_filters=["tonemap=hable:desat=0"])
            self.assertNotEqual(normed, base)
            other = media._mezz_inputs_key(
                src, 0, 20, False, "field",
                norm_filters=["tonemap=mobius:desat=0"])
            self.assertNotEqual(other, normed)


if __name__ == "__main__":
    unittest.main()
