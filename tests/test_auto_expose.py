"""设备接入后自动暴露参数行（双向映射表落地）回归测试。

口径：设备连接后模块应自动向核心暴露其全部可写/可读参数——
输入表补「核心输入参数 ← 头像参数」行（可读参数），输出表补
「核心输出参数 → 头像参数」行（可写参数）；用户删除的行不复活。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from dglab.state import EngineState, Slot
from modules.osc_bridge.bridge import OscBridge, OscConfig, default_input_rows
from modules.osc_bridge.plugin import (OSC_CONFIG_DEFAULTS, OscModule,
                                       materialize)

CONFIG = {"prefix": "DGLab",
          "device_prefixes": {"COYOTE": "DGLab", "OVC": "DGLabOvc",
                              "BMTR": "DGLabBmtr"}}


def _state(*slots: tuple[str, str]) -> EngineState:
    state = EngineState(backend="v4", paired=True, connected=True)
    for sid, dtype in slots:
        state.slots[sid] = Slot(slot_id=sid, type=dtype)
    return state


class DefaultInputRowsTests(unittest.TestCase):
    def test_rows_follow_connected_families(self):
        rows = default_input_rows(CONFIG, _state(("c1", "COYOTE_030"),
                                                 ("b1", "BMTR_1")))
        keys = [row["param"] for row in rows]
        self.assertIn("in_strength_a", keys)
        self.assertIn("in_fire_b", keys)
        self.assertIn("in_emergency", keys)          # 全局参数始终保留
        self.assertFalse(any(k.startswith("in_ovc_") for k in keys))

    def test_no_state_keeps_all_families(self):
        # 引擎空表兜底口径（effective_rows）：不区分接入设备
        keys = [row["param"] for row in default_input_rows(CONFIG)]
        self.assertIn("in_ovc_strength_a", keys)
        self.assertIn("in_emergency", keys)


class MaterializeTests(unittest.TestCase):
    def test_exposes_all_device_params(self):
        settings = dict(CONFIG)
        self.assertTrue(materialize(
            settings, _state(("c1", "COYOTE_030"), ("b1", "BMTR_1"))))
        in_rows = {row["param"]: row for row in settings["mappings"]}
        self.assertIn("in_strength_a", in_rows)
        self.assertEqual(in_rows["in_strength_a"]["name"], "DGLabStrengthA")
        self.assertEqual(in_rows["in_strength_a"]["expr"],
                         "{DGLabStrengthA}")
        out_names = {row["name"] for row in settings["outputs"]}
        self.assertIn("DGLabStrengthA", out_names)      # 郊狼强度
        self.assertIn("DGLabBmtrPressure", out_names)   # 灵猫气压
        self.assertIn("DGLabAction", out_names)         # 全局
        self.assertEqual(sorted(settings["auto_exposed"]),
                         sorted([row["param"] for row in settings["mappings"]]
                                + [row["param"] for row in settings["outputs"]]))

    def test_legacy_migration_does_not_seed_fresh_configs(self):
        # 全新配置装载不预填任何行：缺省行只随设备接入记账落地
        from modules.osc_bridge.plugin import migrate_legacy

        settings = dict(CONFIG)
        self.assertFalse(migrate_legacy(settings))
        self.assertNotIn("mappings", settings)

    def test_user_rows_and_tombstones_survive(self):
        settings = dict(CONFIG, mappings=[
            {"param": "in_strength_a", "name": "MyStr", "expr": "{MyStr}*2"}],
            auto_exposed=["COYOTE.Battery", "in_zap_a"])  # 用户已删除的行
        self.assertTrue(materialize(settings, _state(("c1", "COYOTE_030"))))
        in_rows = {row["param"]: row for row in settings["mappings"]}
        # 已有行原样保留，缺省行补齐
        self.assertEqual(in_rows["in_strength_a"]["expr"], "{MyStr}*2")
        self.assertIn("in_wave_a", in_rows)
        # 记账本内的 id 不复活
        self.assertNotIn("in_zap_a", in_rows)
        out_params = {row["param"] for row in settings["outputs"]}
        self.assertNotIn("COYOTE.Battery", out_params)
        self.assertIn("COYOTE.StrengthA", out_params)

    def test_second_materialize_is_noop(self):
        settings = dict(CONFIG)
        materialize(settings, _state(("c1", "COYOTE_030")))
        self.assertFalse(materialize(settings, _state(("c1", "COYOTE_030"))))


class _Settings(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save(self):
        self.saved += 1


class _Events:
    def __init__(self):
        self.emitted: list[tuple] = []

    def emit(self, event, *args):
        self.emitted.append((event, *args))


class _Engine:
    def __init__(self, state):
        self._state = state

    def get_state(self):
        return self._state


class _Ctx:
    def __init__(self, settings, state):
        self.settings = settings
        self.engine = _Engine(state)
        self.events = _Events()
        self.log = lambda msg: None


class PersistAutoRowsTests(unittest.TestCase):
    def setUp(self):
        self.mod = OscModule()
        self.mod.ctx = _Ctx(_Settings(dict(CONFIG)), None)
        self.mod.bridge = OscBridge(
            OscConfig(dict(CONFIG), defaults=OSC_CONFIG_DEFAULTS),
            self.mod.ctx.engine.get_state, None)

    def tearDown(self):
        self.mod.bridge.close()

    def test_device_change_lands_rows_and_reloads_engine(self):
        rows_in = default_input_rows(CONFIG, _state(("c1", "COYOTE_030")))
        rows_out = [{"param": "COYOTE.StrengthA", "name": "DGLabStrengthA",
                     "expr": "{COYOTE.StrengthA}", "type": "Int"}]
        self.mod._persist_auto_rows(rows_in, rows_out)

        settings = self.mod.ctx.settings
        self.assertIn("in_strength_a",
                      [row["param"] for row in settings["mappings"]])
        # 落地即同步桥接引擎（无需重启桥接）
        self.assertIn("in_strength_a", self.mod.bridge.engine.mappings)
        self.assertEqual(self.mod.ctx.events.emitted,
                         [("modules_changed", "osc_bridge")])

        # 再次推送相同内容：不重复落盘、不重复通知
        self.mod._persist_auto_rows(rows_in, rows_out)
        self.assertEqual(len(self.mod.ctx.events.emitted), 1)
        self.assertGreaterEqual(settings.saved, 1)

    def test_new_device_params_appended_on_later_connect(self):
        strength_out = [{"param": "COYOTE.StrengthA", "name": "DGLabStrengthA",
                         "expr": "{COYOTE.StrengthA}", "type": "Int"}]
        self.mod._persist_auto_rows(
            default_input_rows(CONFIG, _state(("c1", "COYOTE_030"))),
            strength_out)
        self.mod._persist_auto_rows(
            default_input_rows(CONFIG, _state(("c1", "COYOTE_030"),
                                              ("b1", "BMTR_1"))),
            strength_out + [{"param": "BMTR.Pressure",
                             "name": "DGLabBmtrPressure",
                             "expr": "{BMTR.Pressure}", "type": "Float"}])

        settings = self.mod.ctx.settings
        out_params = {row["param"] for row in settings["outputs"]}
        self.assertIn("COYOTE.StrengthA", out_params)
        self.assertIn("BMTR.Pressure", out_params)
        self.assertEqual(len(self.mod.ctx.events.emitted), 2)

    def test_link_params_include_default_names(self):
        mod = OscModule()
        mod.ctx = _Ctx(_Settings(dict(CONFIG)), _state(("c1", "COYOTE_030")))
        mod.bridge = OscBridge(
            OscConfig(dict(CONFIG), defaults=OSC_CONFIG_DEFAULTS),
            mod.ctx.engine.get_state, None)
        try:
            names = dict(mod.link_params())
            self.assertIn("DGLabStrengthA", names)
            self.assertIn("DGLabEmergency", names)
        finally:
            mod.bridge.close()


class BridgeNotifyTests(unittest.TestCase):
    def test_bridge_notifies_on_device_sig_change(self):
        events: list[tuple[list, list]] = []
        bridge = OscBridge(OscConfig(dict(CONFIG)),
                           lambda: None, None,
                           on_auto_rows=lambda i, o: events.append((i, o)))
        try:
            coyote = _state(("c1", "COYOTE_030"))
            bridge._refresh_rows(coyote)
            self.assertEqual(len(events), 1)
            self.assertIn("in_strength_a",
                          [row["param"] for row in events[0][0]])
            self.assertIn("COYOTE.StrengthA",
                          [row["param"] for row in events[0][1]])
            bridge._refresh_rows(coyote)             # 设备集未变 → 不重发
            self.assertEqual(len(events), 1)
            bridge._refresh_rows(_state(("c1", "COYOTE_030"),
                                        ("b1", "BMTR_1")))
            self.assertEqual(len(events), 2)
            self.assertIn("BMTR.Pressure",
                          [row["param"] for row in events[1][1]])
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
