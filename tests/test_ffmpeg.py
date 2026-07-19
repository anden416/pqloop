import io
import unittest
from unittest import mock

from pqloop.ffmpeg import FF, FFmpegError


class _Process:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


class FFmpegProgressTest(unittest.TestCase):
    def test_run_progress_parses_records_and_keeps_stderr(self):
        process = _Process(
            stdout=(
                "frame=12\n"
                "out_time_us=500000\n"
                "speed=2.00x\n"
                "progress=continue\n"
                "frame=25\n"
                "out_time_us=1000000\n"
                "speed=2.10x\n"
                "progress=end\n"
            ),
            stderr="input banner\nencoder details\n",
        )
        records = []
        with mock.patch("pqloop.ffmpeg.subprocess.Popen",
                        return_value=process) as popen:
            completed = FF("custom-ffmpeg").run_progress(
                ["-i", "input.mkv", "output.mp4"], records.append,
                timeout=10)

        command = popen.call_args.args[0]
        self.assertEqual(command[:7], [
            "custom-ffmpeg", "-hide_banner", "-nostdin", "-nostats",
            "-progress", "pipe:1", "-i",
        ])
        self.assertEqual([record["progress"] for record in records],
                         ["continue", "end"])
        self.assertEqual(records[0]["out_time_us"], "500000")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("encoder details", completed.stderr)

    def test_run_progress_reports_buffered_failure_details(self):
        process = _Process(stderr="initializing\nencoder exploded\n",
                           returncode=7)
        with mock.patch("pqloop.ffmpeg.subprocess.Popen",
                        return_value=process):
            with self.assertRaises(FFmpegError) as raised:
                FF("custom-ffmpeg").run_progress([], lambda record: None)

        self.assertIn("rc=7", str(raised.exception))
        self.assertIn("encoder exploded", raised.exception.stderr)


class FFmpegCapabilityTest(unittest.TestCase):
    def test_encoder_help_is_cached(self):
        completed = mock.Mock(
            returncode=0,
            stdout="Encoder hevc_nvenc\n  -lookahead_level <int>\n")
        with mock.patch("pqloop.ffmpeg.subprocess.run",
                        return_value=completed) as run:
            ff = FF("custom-ffmpeg")
            first = ff.encoder_help("hevc_nvenc")
            second = ff.encoder_help("hevc_nvenc")

        self.assertEqual(first, second)
        self.assertIn("lookahead_level", first)
        run.assert_called_once_with(
            ["custom-ffmpeg", "-hide_banner", "-h", "encoder=hevc_nvenc"],
            stdout=mock.ANY, stderr=mock.ANY, text=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
