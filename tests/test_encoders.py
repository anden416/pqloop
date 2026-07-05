import unittest

from pqloop.encoders import get_space, RateControl


class X264ArgsTest(unittest.TestCase):
    def setUp(self):
        self.space = get_space("libx264")
        self.rc = RateControl(6000, 6600, 12000)

    def test_defaults_emit_single_kv_flag_with_gop(self):
        args = self.space.video_args(self.space.defaults(),
                                     gop_len=200, seg_duration=4.0, rc=self.rc)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertIn("-preset", args)
        self.assertEqual(args.count("-x264-params"), 1)
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "6000k")
        self.assertEqual(args[args.index("-g") + 1], "200")
        self.assertIn("-sc_threshold", args)
        self.assertEqual(args[args.index("-force_key_frames") + 1],
                         "expr:gte(t,n_forced*4)")
        kv = args[args.index("-x264-params") + 1]
        self.assertIn("aq-mode=1", kv)
        self.assertIn("psy-rd=1.0", kv)

    def test_tune_none_is_omitted(self):
        args = self.space.video_args(self.space.defaults())
        self.assertNotIn("-tune", args)
        cfg = dict(self.space.defaults(), tune="film")
        args = self.space.video_args(cfg)
        self.assertEqual(args[args.index("-tune") + 1], "film")

    def test_inactive_param_not_emitted(self):
        cfg = dict(self.space.defaults(), me="hex", merange=48)
        kv = self._kv(self.space.video_args(cfg))
        self.assertNotIn("merange", kv)
        cfg["me"] = "umh"
        kv = self._kv(self.space.video_args(cfg))
        self.assertIn("merange=48", kv)

    def test_effective_drops_inactive_and_none(self):
        cfg = dict(self.space.defaults(), me="hex", merange=48, tune=None)
        eff = self.space.effective(cfg)
        self.assertNotIn("merange", eff)
        self.assertNotIn("tune", eff)

    def _kv(self, args):
        return args[args.index("-x264-params") + 1] if "-x264-params" in args else ""


class X265ArgsTest(unittest.TestCase):
    def test_scenecut_merged_into_single_x265_params(self):
        space = get_space("libx265")
        args = space.video_args(space.defaults(), gop_len=100, seg_duration=4.0,
                                rc=RateControl(4000, 4400, 8000))
        self.assertEqual(args.count("-x265-params"), 1)
        kv = args[args.index("-x265-params") + 1]
        self.assertIn("scenecut=0", kv)
        self.assertIn("aq-mode=2", kv)


class NvencArgsTest(unittest.TestCase):
    def test_flag_style_emission(self):
        space = get_space("h264_nvenc")
        cfg = dict(space.defaults(), **{"spatial-aq": 1, "aq-strength": 12})
        args = space.video_args(cfg, gop_len=200, seg_duration=4.0,
                                rc=RateControl(6000, 6600, 12000))
        self.assertIn("-rc", args)
        self.assertEqual(args[args.index("-rc") + 1], "vbr")
        self.assertIn("-no-scenecut", args)
        self.assertIn("-forced-idr", args)
        self.assertEqual(args[args.index("-spatial-aq") + 1], "1")
        self.assertEqual(args[args.index("-aq-strength") + 1], "12")
        self.assertNotIn("-x264-params", args)

    def test_aq_strength_inactive_without_spatial_aq(self):
        space = get_space("h264_nvenc")
        cfg = dict(space.defaults(), **{"spatial-aq": 0, "aq-strength": 12})
        args = space.video_args(cfg)
        self.assertNotIn("-aq-strength", args)


class GenericSpaceTest(unittest.TestCase):
    def test_unknown_encoder_still_encodes(self):
        space = get_space("h264_ni_quadra_enc")
        args = space.video_args({}, gop_len=100, seg_duration=2.0,
                                rc=RateControl(3000, 3300, 6000))
        self.assertEqual(args[args.index("-c:v") + 1], "h264_ni_quadra_enc")
        self.assertIn("-b:v", args)
        self.assertEqual(space.tunable(), [])


class TunableSelectionTest(unittest.TestCase):
    def test_include_exclude_frozen(self):
        space = get_space("libx264")
        names = [s.name for s in space.tunable()]
        self.assertEqual(names[0], "preset")
        only = [s.name for s in space.tunable(include=["aq-mode", "preset"])]
        self.assertEqual(set(only), {"aq-mode", "preset"})
        without = [s.name for s in space.tunable(exclude=["preset"],
                                                 frozen=["psy-rd"])]
        self.assertNotIn("preset", without)
        self.assertNotIn("psy-rd", without)
        with self.assertRaises(ValueError):
            space.tunable(include=["not-a-param"])


if __name__ == "__main__":
    unittest.main()
