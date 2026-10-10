from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from dglab.naming import default_input_name, default_output_name
from modules.osc_bridge.plugin import drop_retired_rows

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


class _Settings(dict):
    """模拟核心的 JsonDict：带 save()，落盘次数可查。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


class DropRetiredRowsTests(unittest.TestCase):
    """表达式映射时代的配置行在加载时清掉，设置页不再出现映射表。"""

    def test_retired_rows_are_dropped_and_saved(self):
        settings = _Settings(
            CONFIG,
            mappings=[{"param": "in_strength_a", "expr": "{DGLabStrengthA}"}],
            outputs=[{"param": "COYOTE.Battery", "name": "Bat",
                      "expr": "{COYOTE.Battery}"}],
            temps=[{"name": "avatar/parameters/X", "expr": "{Y}"}],
            events=[{"name": "OSC 强度（自动）"}],
            input_expr={"in_strength_a": "{blood}"},
            custom_inputs=[{"target": "in_strength_a", "param": "blood"}],
            auto_wired=["DGLabStrengthA"],
            in_strength_a="DGLabStrengthA")
        logs: list[str] = []
        self.assertTrue(drop_retired_rows(settings, logs.append))
        for key in ("mappings", "outputs", "temps", "events", "input_expr",
                    "custom_inputs", "auto_wired", "auto_exposed",
                    "in_strength_a"):
            self.assertNotIn(key, settings, key)
        self.assertEqual(settings.saved, 1)
        self.assertTrue(any("事件流" in line for line in logs))

    def test_clean_settings_untouched(self):
        settings = _Settings(CONFIG, param_names={"COYOTE.StrengthA": "MyStr"})
        self.assertFalse(drop_retired_rows(settings, None))
        self.assertEqual(settings.saved, 0)
        self.assertEqual(settings["param_names"], {"COYOTE.StrengthA": "MyStr"})

    def test_mapping_keys_gone_from_config_spec(self):
        from modules.osc_bridge.plugin import META, OSC_CONFIG_DEFAULTS
        self.assertNotIn("mappings", META["config"])
        self.assertNotIn("outputs", META["config"])
        self.assertNotIn("mappings", OSC_CONFIG_DEFAULTS)
        self.assertNotIn("outputs", OSC_CONFIG_DEFAULTS)
        groups = {str(spec.get("group") or "") for spec in META["config"].values()}
        self.assertNotIn("map", groups)


if __name__ == "__main__":
    unittest.main()
