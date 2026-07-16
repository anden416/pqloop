import os
import tempfile
import unittest
from pathlib import Path

from pqloop.encoders import get_space
from pqloop.ffmpeg import FF
from pqloop.media import probe_file
from pqloop.package import AAC_CODEC, finalize_manifests
from pqloop.segment import final_encode


RUN_INTEGRATION = os.environ.get("PQLOOP_FFMPEG_INTEGRATION") == "1"


@unittest.skipUnless(RUN_INTEGRATION,
                     "set PQLOOP_FFMPEG_INTEGRATION=1 for real ffmpeg tests")
class FFmpegIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ff = FF("ffmpeg")
        required_encoders = ("libx264", "libx265", "aac")
        missing = [name for name in required_encoders
                   if not cls.ff.has_encoder(name)]
        missing += [f"muxer:{name}" for name in ("dash", "mp4")
                    if not cls.ff.has_muxer(name)]
        if missing:
            raise AssertionError("integration ffmpeg lacks: " + ", ".join(missing))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source.mp4"
        self.ff.run([
            "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(self.source_path),
        ], timeout=120)
        self.source = probe_file(self.ff, self.source_path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _cfg(encoder, two_pass=False):
        return {
            "encoder": encoder, "target_bitrate_kbps": 300,
            "maxrate_ratio": 1.1, "bufsize_ratio": 2.0,
            "seg_duration": 1.0, "gop_duration": None,
            "pix_fmt": "yuv420p", "scale": "",
            "deinterlace": "off", "deint_mode": "field",
            "extra_video_args": [], "two_pass": two_pass,
        }

    def test_direct_cmaf_manifests_are_strict_for_h264_and_hevc(self):
        for encoder in ("libx264", "libx265"):
            with self.subTest(encoder=encoder):
                out = self.root / encoder
                space = get_space(encoder)
                result = final_encode(
                    self.ff, space, space.defaults(), self._cfg(encoder),
                    self.source, out, fmt="cmaf", duration=2)
                finalize_manifests(
                    "cmaf", out, fps=result["fps"], audio_codec=AAC_CODEC,
                    ff=self.ff, probe_path=result["output"])
                probe = self.ff.probe(result["output"])
                self.assertTrue(any(s.get("codec_type") == "video"
                                    for s in probe.get("streams", [])))
                master = (out / "master.m3u8").read_text()
                self.assertIn("#EXT-X-INDEPENDENT-SEGMENTS", master)
                self.assertIn("AVERAGE-BANDWIDTH=", master)
                self.assertIn("FRAME-RATE=25.000", master)
                if encoder == "libx265":
                    mpd = (out / "manifest.mpd").read_text()
                    self.assertNotIn('codecs="hvc1"', mpd)
                    self.assertNotIn('CODECS="hvc1,', master)
                    self.assertIn("hvc1.1.", mpd)

    def test_two_pass_presets_run_two_passes_and_clean_logs(self):
        for encoder in ("libx264", "libx265"):
            with self.subTest(encoder=encoder):
                # The colon also verifies private x265 stats-path escaping.
                out = self.root / f"two:pass-{encoder}"
                space = get_space(encoder)
                result = final_encode(
                    self.ff, space, space.defaults(),
                    self._cfg(encoder, True), self.source, out,
                    fmt="mp4", duration=2)
                self.assertEqual(result["passes"], 2)
                self.assertTrue((out / "output.mp4").exists())
                self.assertTrue(self.ff.probe(out / "output.mp4").get("streams"))
                self.assertEqual(list(out.glob(".pqloop-passlog*")), [])

    def test_probe_entries_streams_rows(self):
        rows = list(self.ff.iter_probe_entries(
            self.source_path, "packet", "pts_time,flags", select="v:0",
            read_intervals="%+0.5", timeout=30))
        self.assertGreater(len(rows), 0)
        self.assertTrue(all("pts_time" in row for row in rows))
        self.assertTrue(all(not value.endswith("\n")
                            for row in rows for value in row.values()))


if __name__ == "__main__":
    unittest.main()
