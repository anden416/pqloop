import unittest

from pqloop.encoders import get_space, RateControl, codec_family


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

    def test_extra_kv_merged_into_params_flag(self):
        space = get_space("libx265")
        args = space.video_args(space.defaults(),
                                extra_kv={"pass": 1, "stats": "x.log"})
        self.assertEqual(args.count("-x265-params"), 1)
        kv = args[args.index("-x265-params") + 1]
        self.assertIn("pass=1", kv)
        self.assertIn("stats=x.log", kv)


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


class SvtAv1ArgsTest(unittest.TestCase):
    def setUp(self):
        self.space = get_space("libsvtav1")

    def test_single_svtav1_params_flag_with_scd_off(self):
        args = self.space.video_args(self.space.defaults(), gop_len=200,
                                     seg_duration=4.0,
                                     rc=RateControl(3000, 3300, 6000))
        self.assertEqual(args[args.index("-c:v") + 1], "libsvtav1")
        self.assertEqual(args.count("-svtav1-params"), 1)
        kv = args[args.index("-svtav1-params") + 1]
        self.assertIn("scd=0", kv)
        self.assertIn("tune=1", kv)
        self.assertEqual(args[args.index("-g") + 1], "200")
        self.assertIn("-force_key_frames", args)

    def test_preset_emitted_as_flag(self):
        args = self.space.video_args(self.space.defaults())
        self.assertEqual(args[args.index("-preset") + 1], "8")
        self.assertNotIn("preset=", args[args.index("-svtav1-params") + 1])

    def test_qm_min_inactive_without_enable_qm(self):
        cfg = dict(self.space.defaults(), **{"enable-qm": 0, "qm-min": 0})
        kv = self.space.video_args(cfg)[-1]
        self.assertNotIn("qm-min", kv)
        cfg["enable-qm"] = 1
        kv = self.space.video_args(cfg)[-1]
        self.assertIn("qm-min=0", kv)


class QuadraArgsTest(unittest.TestCase):
    def setUp(self):
        self.space = get_space("h264_ni_quadra_enc")

    def kv(self, config, **kwargs):
        args = self.space.video_args(config, **kwargs)
        raw = args[args.index("-xcoder-params") + 1]
        return dict(pair.split("=", 1) for pair in raw.split(":"))

    def test_xcoder_params_colon_joined_single_flag(self):
        args = self.space.video_args(self.space.defaults(), gop_len=200,
                                     seg_duration=4.0,
                                     rc=RateControl(6000, 6600, 12000))
        self.assertEqual(args[args.index("-c:v") + 1], "h264_ni_quadra_enc")
        self.assertEqual(args.count("-xcoder-params"), 1)
        kv = args[args.index("-xcoder-params") + 1]
        self.assertIn("RcEnable=1", kv)
        self.assertIn("gopPresetIdx=-1", kv)

    def test_intra_period_tracks_gop_len(self):
        args = self.space.video_args(self.space.defaults(), gop_len=250,
                                     seg_duration=5.0)
        kv = args[args.index("-xcoder-params") + 1]
        self.assertIn("intraPeriod=250", kv)

    def test_rc_enable_always_emitted_but_never_tuned(self):
        self.assertNotIn("RcEnable", [s.name for s in self.space.tunable()])
        kv = self.space.video_args(self.space.defaults())[-1]
        self.assertIn("RcEnable=1", kv)

    def test_marked_experimental(self):
        self.assertTrue(self.space.experimental)
        self.assertTrue(get_space("h265_ni_quadra_enc").experimental)
        self.assertFalse(get_space("libx264").experimental)

    def test_vbv_goes_through_xcoder_params(self):
        # the netint wrapper ignores -maxrate/-bufsize; VBV must be in kv:
        # vbvBufferSize in msec of buffer at target rate, vbvMaxRate in bps
        args = self.space.video_args(self.space.defaults(),
                                     rc=RateControl(6000, 6600, 12000))
        kv = args[args.index("-xcoder-params") + 1]
        self.assertIn("vbvBufferSize=2000", kv)
        self.assertIn("vbvMaxRate=6600000", kv)

    def test_vbv_buffer_size_clamped_to_valid_range(self):
        kv = self.space.video_args(self.space.defaults(),
                                   rc=RateControl(1000, 1100, 10000))[-1]
        self.assertIn("vbvBufferSize=3000", kv)     # 10s of buffer -> cap 3000ms
        kv = self.space.video_args(self.space.defaults(),
                                   rc=RateControl(100000, 110000, 100))[-1]
        self.assertIn("vbvBufferSize=10", kv)       # floor 10ms

    def test_no_vbv_params_without_rate_control(self):
        kv = self.space.video_args(self.space.defaults())[-1]
        self.assertNotIn("vbvBufferSize", kv)
        self.assertNotIn("vbvMaxRate", kv)

    def test_deprecated_cbr_replaced_by_filler_enable(self):
        # "cbr" logs a deprecation warning on Quadra; fillerEnable is the
        # documented replacement (same filler-bits semantics)
        self.assertNotIn("cbr", self.space.params)
        self.assertIn("fillerEnable", self.space.params)

    def test_hvs_qp_enable_tunable(self):
        self.assertIn("hvsQPEnable", [s.name for s in self.space.tunable()])

    def test_gop_preset_idx_values_valid_per_guide(self):
        # Integration guide 8.4: supported gopPresetIdx values are
        # -1, 0, 1, 3, 4, 5, 7, 8, 9, 10 (2 and 6 do not exist on Quadra);
        # -1 (adaptive GOP) is the hardware default
        spec = self.space.params["gopPresetIdx"]
        self.assertTrue(set(spec.values) <= {-1, 0, 1, 3, 4, 5, 7, 8, 9, 10})
        self.assertEqual(spec.default, -1)

    def test_rdo_knobs_follow_codec_support(self):
        # guide 8.4: enableRdoQuant applies to H.264+H.265; rdoLevel is
        # 1..3 for H.265 but fixed at 1 for H.264 (nothing to tune)
        h265 = get_space("h265_ni_quadra_enc")
        self.assertIn("enableRdoQuant", self.space.params)
        self.assertIn("enableRdoQuant", h265.params)
        self.assertNotIn("rdoLevel", self.space.params)
        self.assertEqual(h265.params["rdoLevel"].values, (1, 2, 3))

    def test_rc_mode_searched_but_never_emitted(self):
        self.assertIn("rcMode", [s.name for s in self.space.tunable()])
        kv = self.kv(self.space.defaults(), rc=RateControl(6000, 6600, 12000))
        self.assertNotIn("rcMode", kv)

    def test_rc_modes_map_to_vbv_pattern(self):
        # guide 12.4: the zero/non-zero pattern of vbvBufferSize/vbvMaxRate
        # selects constrained-VBR vs CBR vs ABR
        rc = RateControl(6000, 6600, 12000)
        for mode, buf, maxrate in (("cvbr", "2000", "6600000"),
                                   ("cbr", "2000", "0"),
                                   ("abr", "0", "0")):
            kv = self.kv({**self.space.defaults(), "rcMode": mode}, rc=rc)
            self.assertEqual(kv["vbvBufferSize"], buf, mode)
            self.assertEqual(kv["vbvMaxRate"], maxrate, mode)
            self.assertEqual(kv["RcEnable"], "1", mode)

    def test_capped_crf_mode_drops_rc_enable(self):
        # guide 8.4: "When CRF mode is enabled, rcEnable must be 0" — the
        # hardware default is 0, so RcEnable must simply not be emitted
        kv = self.kv({**self.space.defaults(), "rcMode": "cappedcrf"},
                     rc=RateControl(6000, 6600, 12000))
        self.assertNotIn("RcEnable", kv)
        self.assertEqual(kv["crf"], "23")
        self.assertEqual(kv["vbvBufferSize"], "2000")   # bitrate caps apply
        self.assertEqual(kv["vbvMaxRate"], "6600000")

    def test_mode_dependent_params_gated(self):
        eff = self.space.effective(self.space.defaults())
        # inert at defaults: crf (not cappedcrf), bitrateMode (needs
        # cuLevelRCEnable=1), lookahead refinements (lookAheadDepth=0)
        for name in ("crf", "bitrateMode", "noMbTree", "enable2PassGop",
                     "tuneBframeVisual"):
            self.assertNotIn(name, eff, name)
        # HRD-only knobs vanish in ABR (fillerEnable would silently force
        # vbvBufferSize=3000 back on), RC-only knobs vanish in capped CRF
        abr = self.space.effective({**self.space.defaults(), "rcMode": "abr"})
        self.assertNotIn("fillerEnable", abr)
        self.assertNotIn("enableipRatio", abr)
        crf = self.space.effective({**self.space.defaults(),
                                    "rcMode": "cappedcrf"})
        self.assertNotIn("intraQPDelta", crf)
        self.assertIn("crf", crf)
        # lookahead unlocks its refinements
        la = self.space.effective({**self.space.defaults(),
                                   "lookAheadDepth": 20, "noMbTree": 1})
        self.assertIn("noMbTree", la)


class CodecFamilyTest(unittest.TestCase):
    def test_families(self):
        for name, family in (("libx264", "h264"), ("h264_nvenc", "h264"),
                             ("h264_ni_quadra_enc", "h264"),
                             ("libx265", "hevc"), ("hevc_nvenc", "hevc"),
                             ("h265_ni_quadra_enc", "hevc"),
                             ("libsvtav1", "av1"), ("libaom-av1", "av1"),
                             ("av1_nvenc", "av1"), ("mpeg2video", "")):
            self.assertEqual(codec_family(name), family, name)


class GenericSpaceTest(unittest.TestCase):
    def test_unknown_encoder_still_encodes(self):
        space = get_space("some_future_encoder")
        args = space.video_args({}, gop_len=100, seg_duration=2.0,
                                rc=RateControl(3000, 3300, 6000))
        self.assertEqual(args[args.index("-c:v") + 1], "some_future_encoder")
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
