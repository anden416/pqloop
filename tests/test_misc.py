import json
import tempfile
import unittest
from pathlib import Path

from pqloop import presets, stats
from pqloop.util import (parse_bitrate_kbps, parse_time_seconds, parse_fps,
                         coerce_value)


class ParseTest(unittest.TestCase):
    def test_bitrate(self):
        self.assertEqual(parse_bitrate_kbps("6000k"), 6000)
        self.assertEqual(parse_bitrate_kbps("6M"), 6000)
        self.assertEqual(parse_bitrate_kbps("6.5m"), 6500)
        self.assertEqual(parse_bitrate_kbps("4500"), 4500)
        self.assertEqual(parse_bitrate_kbps(3000), 3000)
        with self.assertRaises(ValueError):
            parse_bitrate_kbps("fast")

    def test_time(self):
        self.assertEqual(parse_time_seconds("95.5"), 95.5)
        self.assertEqual(parse_time_seconds("01:05:08"), 3908.0)
        self.assertEqual(parse_time_seconds("2:30"), 150.0)

    def test_fps(self):
        self.assertEqual(parse_fps("25/1"), 25.0)
        self.assertEqual(parse_fps("30000/1001"), 30000 / 1001)
        self.assertEqual(parse_fps("0/0"), 0.0)

    def test_coerce(self):
        self.assertEqual(coerce_value("3"), 3)
        self.assertEqual(coerce_value("0.7"), 0.7)
        self.assertEqual(coerce_value("slow"), "slow")
        self.assertIsNone(coerce_value("none"))


class PresetTest(unittest.TestCase):
    def test_roundtrip_and_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            path = presets.resolve("sports", td)
            self.assertEqual(path, Path(td) / "sports.json")
            data = presets.load(path)
            self.assertEqual(data["name"], "sports")
            data["config"]["encoder"] = "libx264"
            data["optimizer"] = {"encodes": 7}
            presets.save(path, data)
            again = presets.load(path)
            self.assertEqual(again["config"]["encoder"], "libx264")
            self.assertEqual(again["optimizer"]["encodes"], 7)
            listed = presets.list_presets(td)
            self.assertEqual(listed[0]["name"], "sports")
            self.assertEqual(listed[0]["encodes"], 7)

    def test_resolve_path_passthrough(self):
        self.assertEqual(presets.resolve("x/y.json", "presets"), Path("x/y.json"))


class StatsTest(unittest.TestCase):
    def test_run_id_and_schema_on_events(self):
        with tempfile.TemporaryDirectory() as td:
            w = stats.StatsWriter(td, "run7")
            w.event("meta", schema=stats.SCHEMA, **stats.host_meta())
            w.event("trial", n=1, ok=True)
            w.close()
            events = stats.read_events(w.path)
            self.assertTrue(all(e["run_id"] == "run7" for e in events))
            meta = events[0]
            self.assertEqual(meta["schema"], stats.SCHEMA)
            self.assertTrue(meta["hostname"])
            self.assertTrue(meta["platform"])
            csv_header = Path(stats.to_csv(w.path)).read_text().splitlines()[0]
            self.assertIn("run_id", csv_header)

    def test_jsonl_and_csv(self):
        with tempfile.TemporaryDirectory() as td:
            w = stats.StatsWriter(td, "run1")
            w.event("meta", config={"encoder": "libx264"})
            w.event("trial", n=1, phase="baseline", label="baseline", cached=False,
                    ok=True, objective=90.0,
                    params={"preset": "medium", "aq-mode": 1},
                    metrics={"vmaf_mean": 90.0, "bitrate_kbps": 6000})
            w.event("trial", n=2, phase="screen", label="preset=slow", cached=False,
                    ok=True, objective=91.5,
                    params={"preset": "slow", "aq-mode": 1},
                    metrics={"vmaf_mean": 91.5, "bitrate_kbps": 6010})
            w.event("done", stop_reason="diminishing_returns", elapsed_s=12)
            w.close()
            csv_path = stats.to_csv(w.path)
            text = Path(csv_path).read_text()
            header = text.splitlines()[0]
            self.assertIn("param.preset", header)
            self.assertIn("vmaf_mean", header)
            self.assertEqual(len(text.strip().splitlines()), 3)
            summary = stats.summarize(w.path)
            self.assertIn("diminishing_returns", summary)
            self.assertIn("91.5", summary)


if __name__ == "__main__":
    unittest.main()
