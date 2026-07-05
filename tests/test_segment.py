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

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            _encode("libx264", "webm")


class TimeoutTest(unittest.TestCase):
    def test_run_timeout_is_bounded(self):
        _, timeout = _encode("libx264", "mp4")
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(timeout, 1800.0)


if __name__ == "__main__":
    unittest.main()
