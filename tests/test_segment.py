import tempfile
import unittest

from pqloop import segment
from pqloop.encoders import get_space
from pqloop.media import SourceInfo


class FakeFF:
    def __init__(self):
        self.calls = []

    def run(self, args, timeout=None):
        self.calls.append(([str(a) for a in args], timeout))


def _source():
    return SourceInfo(path="in.mp4", width=1920, height=1080, fps=25.0,
                      fps_str="25/1", field_order="progressive", duration=60.0,
                      has_audio=False, video_codec="h264", pix_fmt="yuv420p")


def _encode(encoder, fmt, seg_type="fmp4"):
    ff = FakeFF()
    space = get_space(encoder)
    with tempfile.TemporaryDirectory() as td:
        segment.final_encode(ff, space, space.defaults(),
                             {"target_bitrate_kbps": 3000}, _source(), td,
                             fmt=fmt, hls_segment_type=seg_type)
    return ff.calls[0]


class HvcTagTest(unittest.TestCase):
    def test_hevc_tagged_hvc1_for_mp4_dash_cmaf_and_fmp4_hls(self):
        for fmt, seg_type in (("mp4", "fmp4"), ("fmp4", "fmp4"),
                              ("dash", "fmp4"), ("cmaf", "fmp4"),
                              ("hls", "fmp4")):
            args, _ = _encode("libx265", fmt, seg_type)
            self.assertEqual(args[args.index("-tag:v") + 1], "hvc1",
                             f"{fmt}/{seg_type}")

    def test_no_tag_for_mpegts_hls_or_h264(self):
        args, _ = _encode("libx265", "hls", "mpegts")
        self.assertNotIn("-tag:v", args)
        for fmt in ("mp4", "hls", "dash"):
            args, _ = _encode("libx264", fmt)
            self.assertNotIn("-tag:v", args, fmt)


class CmafTest(unittest.TestCase):
    def test_cmaf_is_dash_muxer_plus_hls_playlist(self):
        args, _ = _encode("libx264", "cmaf")
        self.assertEqual(args[args.index("-f") + 1], "dash")
        self.assertEqual(args[args.index("-hls_playlist") + 1], "1")
        self.assertEqual(args[args.index("-hls_master_name") + 1],
                         "master.m3u8")
        self.assertTrue(args[-1].endswith("manifest.mpd"))

    def test_plain_dash_has_no_hls_playlist(self):
        args, _ = _encode("libx264", "dash")
        self.assertNotIn("-hls_playlist", args)


class Mp4Test(unittest.TestCase):
    def test_fmp4_is_single_fragmented_file(self):
        args, _ = _encode("libx264", "fmp4")
        movflags = args[args.index("-movflags") + 1]
        for flag in ("frag_keyframe", "empty_moov", "default_base_moof"):
            self.assertIn(flag, movflags)
        self.assertNotIn("faststart", movflags)
        self.assertEqual(args[args.index("-f") + 1], "mp4")
        self.assertTrue(args[-1].endswith("output.mp4"))

    def test_plain_mp4_is_faststart_not_fragmented(self):
        args, _ = _encode("libx264", "mp4")
        movflags = args[args.index("-movflags") + 1]
        self.assertIn("faststart", movflags)
        self.assertNotIn("frag_keyframe", movflags)
        self.assertTrue(args[-1].endswith("output.mp4"))

    def test_intermediates_can_skip_faststart(self):
        # ABR packaging encodes local intermediates where the faststart
        # whole-file rewrite buys nothing
        args, _, _ = segment.build_encode_args(
            get_space("libx264"), get_space("libx264").defaults(),
            {"target_bitrate_kbps": 3000}, _source(), "out", fmt="mp4",
            want_audio=False, faststart=False)
        self.assertNotIn("-movflags", args)
        self.assertIn("-an", args)

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            _encode("libx264", "webm")


class StreamMapTest(unittest.TestCase):
    def _encode_source(self, source):
        ff = FakeFF()
        space = get_space("libx264")
        with tempfile.TemporaryDirectory() as td:
            segment.final_encode(ff, space, space.defaults(),
                                 {"target_bitrate_kbps": 3000}, source, td)
        return ff.calls[0][0]

    def test_default_source_keeps_first_stream_maps(self):
        src = _source()
        src.has_audio = True
        args = self._encode_source(src)
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "0:a:0?"])

    def test_program_selected_streams_are_mapped(self):
        src = _source()
        src.has_audio = True
        src.program, src.video_index, src.audio_index = 2, 4, 5
        args = self._encode_source(src)
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:4", "0:5"])


class GopDurationTest(unittest.TestCase):
    def _encode(self, cfg):
        ff = FakeFF()
        space = get_space("libx264")
        with tempfile.TemporaryDirectory() as td:
            segment.final_encode(ff, space, space.defaults(), cfg, _source(),
                                 td, fmt="mp4")
        return ff.calls[0][0]

    def test_gop_defaults_to_segment(self):
        # 4s segment at 25fps -> 100-frame GOP, keyframes forced every 4s
        args = self._encode({"target_bitrate_kbps": 3000, "seg_duration": 4.0})
        self.assertEqual(args[args.index("-g") + 1], "100")
        self.assertEqual(args[args.index("-force_key_frames") + 1],
                         "expr:gte(t,n_forced*4)")

    def test_gop_duration_decouples_from_segment(self):
        # 2s GOP inside 4s segments: -g halves, segment cadence unchanged
        args = self._encode({"target_bitrate_kbps": 3000, "seg_duration": 4.0,
                             "gop_duration": 2.0})
        self.assertEqual(args[args.index("-g") + 1], "50")
        self.assertEqual(args[args.index("-force_key_frames") + 1],
                         "expr:gte(t,n_forced*4)")


class TimeoutTest(unittest.TestCase):
    def test_run_timeout_is_bounded(self):
        _, timeout = _encode("libx264", "mp4")
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(timeout, 1800.0)


def _hdr_source():
    return SourceInfo(path="m.mxf", width=3840, height=2160, fps=59.94,
                      fps_str="60000/1001", field_order="progressive",
                      duration=719.0, has_audio=False, video_codec="jpeg2000",
                      pix_fmt="rgb48le", bit_depth=12, is_rgb=True)


class NormalizationTest(unittest.TestCase):
    CFG = {"target_bitrate_kbps": 4500, "src_primaries": "bt2020",
           "src_trc": "smpte2084", "norm_scale": "1920x1080"}

    def _encode(self, cfg, source, encoder="libx265"):
        ff = FakeFF()
        space = get_space(encoder)
        with tempfile.TemporaryDirectory() as td:
            segment.final_encode(ff, space, space.defaults(), cfg, source, td,
                                 fmt="cmaf")
        return ff.calls[0][0]

    def test_norm_chain_precedes_quantization_and_rung_scale(self):
        cfg = dict(self.CFG, scale="1280x720")
        args = self._encode(cfg, _hdr_source())
        vf = args[args.index("-vf") + 1]
        self.assertEqual(vf.split(","), [
            "setparams=color_primaries=bt2020:color_trc=smpte2084:range=pc",
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "scale=1920:1080:flags=lanczos",
            "format=yuv420p",                       # trials scaled 8-bit frames
            "scale=1280:720:flags=lanczos"])        # rung scale comes last

    def test_tonemapped_output_is_tagged_sdr_bt709(self):
        args = self._encode(dict(self.CFG), _hdr_source())
        for flag, value in (("-color_primaries", "bt709"),
                            ("-color_trc", "bt709"),
                            ("-colorspace", "bt709"),
                            ("-color_range", "tv")):
            self.assertEqual(args[args.index(flag) + 1], value, flag)

    def test_sdr_source_with_unset_keys_is_unchanged(self):
        args = self._encode({"target_bitrate_kbps": 3000}, _source(),
                            encoder="libx264")
        self.assertNotIn("-vf", args)               # no filters at all
        self.assertNotIn("-color_primaries", args)


if __name__ == "__main__":
    unittest.main()
