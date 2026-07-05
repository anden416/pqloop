import json
import tempfile
import unittest
from pathlib import Path

from pqloop import vmaf


class PercentileTest(unittest.TestCase):
    def test_interpolation(self):
        self.assertEqual(vmaf._percentile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)
        scores = sorted(float(i) for i in range(1, 101))
        self.assertAlmostEqual(vmaf._percentile(scores, 0.01), 1.99)
        self.assertAlmostEqual(vmaf._percentile(scores, 0.05), 5.95)

    def test_edges(self):
        self.assertEqual(vmaf._percentile([], 0.5), 0.0)
        self.assertEqual(vmaf._percentile([7.0], 0.01), 7.0)


class EscapeTest(unittest.TestCase):
    def test_filtergraph_specials_escaped(self):
        self.assertEqual(vmaf._fesc("a:b,c'd"), "a\\:b\\,c\\'d")
        self.assertEqual(vmaf._fesc("plain"), "plain")


class FakeVmafFF:
    """Writes a canned libvmaf JSON log where measure() expects it."""

    def __init__(self, log_path, payload):
        self.log_path = log_path
        self.payload = payload
        self.graphs = []

    def run(self, args, timeout=None):
        args = [str(a) for a in args]
        self.graphs.append(args[args.index("-lavfi") + 1])
        Path(self.log_path).write_text(json.dumps(self.payload))


def _measure(payload, td, **kwargs):
    log_path = str(Path(td) / "v.json")
    ff = FakeVmafFF(log_path, payload)
    scores = vmaf.measure(ff, "dis.mp4", "ref.mkv", 1920, 1080, log_path,
                          **kwargs)
    return scores, ff


class MeasureAggregateTest(unittest.TestCase):
    def test_per_frame_aggregation(self):
        frames = {"frames": [{"metrics": {"vmaf": s}} for s in (80.0, 90.0, 100.0)]}
        with tempfile.TemporaryDirectory() as td:
            scores, _ = _measure(frames, td)
        self.assertAlmostEqual(scores["vmaf_mean"], 90.0)
        self.assertEqual(scores["vmaf_min"], 80.0)
        self.assertEqual(scores["vmaf_frames"], 3)
        self.assertAlmostEqual(scores["vmaf_p1"], 80.2)
        self.assertAlmostEqual(scores["vmaf_p5"], 81.0)

    def test_pooled_only_fallback_degrades_p1_to_min(self):
        pooled = {"pooled_metrics": {"vmaf": {
            "mean": 95.0, "harmonic_mean": 94.5, "min": 90.0}}}
        with tempfile.TemporaryDirectory() as td:
            scores, _ = _measure(pooled, td)
        self.assertEqual(scores["vmaf_mean"], 95.0)
        self.assertEqual(scores["vmaf_harmonic"], 94.5)
        self.assertEqual(scores["vmaf_p1"], 90.0)
        self.assertEqual(scores["vmaf_p5"], 90.0)
        self.assertEqual(scores["vmaf_frames"], 0)

    def test_no_scores_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                _measure({"frames": []}, td)

    def test_graph_options(self):
        frames = {"frames": [{"metrics": {"vmaf": 90.0}}]}
        with tempfile.TemporaryDirectory() as td:
            _, ff = _measure(frames, td, subsample=5, threads=4,
                             model="version=vmaf_4k_v0.6.1")
        graph = ff.graphs[0]
        self.assertIn("n_subsample=5", graph)
        self.assertIn("n_threads=4", graph)
        self.assertIn("vmaf_4k_v0.6.1", graph)
        self.assertIn("scale=1920:1080", graph)


if __name__ == "__main__":
    unittest.main()
