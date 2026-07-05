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


if __name__ == "__main__":
    unittest.main()
