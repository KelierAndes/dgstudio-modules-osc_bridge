"""OSC 默认参数名生成与输入行 name 补齐回归测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from modules.osc_bridge.bridge import default_input_name, default_output_name
from modules.osc_bridge.plugin import materialize_names

CONFIG = {"prefix": "DGLab",
          "device_prefixes": {"COYOTE": "DGLab", "OVC": "DGLabOvc",
                              "BMTR": "DGLabBmtr"}}


class DefaultNameTests(unittest.TestCase):
    def test_input_names_follow_prefix_templates(self):
        self.assertEqual(default_input_name(CONFIG, "in_strength_a"),
                         "DGLabStrengthA")
        self.assertEqual(default_input_name(CONFIG, "in_ovc_fire"),
                         "DGLabOvcInFire")
        self.assertEqual(default_input_name(CONFIG, "in_fire_a"),
                         "DGLabFireA")
        self.assertEqual(default_input_name(CONFIG, "in_fire_b"),
                         "DGLabFireB")
        self.assertEqual(default_input_name(CONFIG, "in_ovc_fire_a"),
                         "DGLabOvcInFireA")
        self.assertEqual(default_input_name(CONFIG, "in_ovc_fire_b"),
                         "DGLabOvcInFireB")
        self.assertEqual(default_input_name(CONFIG, "in_ovc_wave_step_b"),
                         "DGLabOvcInWaveStepB")
        self.assertEqual(default_input_name(CONFIG, "in_emergency"),
                         "DGLabEmergency")

    def test_output_names_follow_device_prefixes(self):
        self.assertEqual(default_output_name(CONFIG, "COYOTE.StrengthA"),
                         "DGLabStrengthA")
        self.assertEqual(default_output_name(CONFIG, "COYOTE.Battery"),
                         "DGLabBattery")
        self.assertEqual(default_output_name(CONFIG, "COYOTE.2.Battery"),
                         "DGLab2Battery")
        self.assertEqual(default_output_name(CONFIG, "BMTR.Pressure"),
                         "DGLabBmtrPressure")
        self.assertEqual(default_output_name(CONFIG, "Action"), "DGLabAction")

    def test_unknown_key_falls_back(self):
        self.assertEqual(default_input_name(CONFIG, "nope"), "nope")
        self.assertEqual(default_output_name(CONFIG, "nope"), "nope")


class MaterializeNamesTests(unittest.TestCase):
    def test_missing_names_filled(self):
        settings = dict(CONFIG, mappings=[
            {"param": "in_strength_a", "expr": "{DGLabStrengthA}"},
            {"param": "in_emergency", "name": "MyStop", "expr": "{MyStop}"},
            {"param": "in_fire", "expr": ""},
        ])
        self.assertTrue(materialize_names(settings))
        rows = settings["mappings"]
        self.assertEqual(rows[0]["name"], "DGLabStrengthA")
        self.assertEqual(rows[1]["name"], "MyStop")
        self.assertEqual(rows[2]["name"], "DGLabFire")

    def test_no_change_when_all_named(self):
        settings = dict(CONFIG, mappings=[
            {"param": "in_strength_a", "name": "Custom", "expr": "{Custom}"}])
        self.assertFalse(materialize_names(settings))


if __name__ == "__main__":
    unittest.main()
