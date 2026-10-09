"""OSC 参数改名回归测试：变量表登记的行落在「可改名参数」栏，改名即改 OSC 地址。

口径：temp_specs 的每一行都带 renamable=True（核心据此把它们放进可改名栏），
改名走 rename_var(旧变量名, 新变量名) → 覆盖表 settings["param_names"]，
下一轮登记（temp_specs / link_params）、收发地址（default_input_name /
default_output_name 与桥接维护回传）全部跟着走新名字。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from dglab.naming import default_input_name, default_output_name
from dglab.state import EngineState, Slot
from modules.osc_bridge.bridge import OscBridge, OscConfig, TEMP_PATH_PREFIX
from modules.osc_bridge.plugin import (OSC_CONFIG_DEFAULTS, OscModule,
                                       materialize_names)

CONFIG = {"prefix": "DGLab", "in_port": 19001,
          "device_prefixes": {"COYOTE": "DGLab", "OVC": "DGLabOvc",
                              "BMTR": "DGLabBmtr"}}


def _path(name: str) -> str:
    return TEMP_PATH_PREFIX + name


def _addr(name: str) -> str:
    return "/" + TEMP_PATH_PREFIX + name


def _state(*slots: tuple[str, str]) -> EngineState:
    state = EngineState(backend="v4", paired=True, connected=True)
    for sid, dtype in slots:
        state.slots[sid] = Slot(slot_id=sid, type=dtype)
    return state


class _Settings(dict):
    def save(self):
        self.saved = getattr(self, "saved", 0) + 1


class _Events:
    def on(self, *args):
        pass

    def off(self, *args):
        pass

    def emit(self, event, *args):
        pass


class _Ctx:
    def __init__(self, state=None):
        self.settings = _Settings(dict(CONFIG))
        self.engine = self._Engine(state)
        self.engine.modules = None
        self.events = _Events()
        self.temps: dict[str, float] = {}
        self.logs: list[str] = []
        self.log = self.logs.append

    def set_temp(self, key, value):
        self.temps[str(key)] = value

    class _Engine:
        def __init__(self, state):
            self._state = state

        def get_state(self):
            return self._state


def _make_module(state=None) -> tuple[OscModule, _Ctx]:
    mod = OscModule()
    ctx = _Ctx(state)
    mod.on_load(ctx)
    return mod, ctx


def _attach_bridge(mod: OscModule, ctx: _Ctx) -> OscBridge:
    written: dict[str, object] = {}
    bridge = OscBridge(OscConfig(dict(ctx.settings), defaults=OSC_CONFIG_DEFAULTS),
                       ctx.engine.get_state, None,
                       set_temp=lambda k, v: written.__setitem__(k, v))
    bridge.log = lambda msg: None
    mod.bridge = bridge
    bridge._written = written
    return bridge


class RenamableRowsTests(unittest.TestCase):
    """登记行必须标 renamable，方向 / 值类型口径与 v1.13 保持一致。"""

    def test_every_row_is_renamable(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"), ("b1", "BMTR_1")))
        rows = mod.temp_specs()
        self.assertTrue(rows)
        for row in rows:
            self.assertIs(row["renamable"], True, row)

    def test_row_fields_and_directions_unchanged(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"), ("b1", "BMTR_1")))
        by = {row["key"]: row for row in mod.temp_specs()}
        self.assertEqual(sorted(by[_path("DGLabStrengthA")]),
                         ["desc", "dir", "key", "label", "renamable", "type"])
        # 方向按参数语义：核心输入参数可写、设备回传读数可读、两边都有则读写
        self.assertEqual(by[_path("DGLabStrengthA")]["dir"], "inout")
        self.assertEqual(by[_path("DGLabWaveA")]["dir"], "out")
        self.assertEqual(by[_path("DGLabEmergency")]["dir"], "out")
        self.assertEqual(by["DGLab/Action"]["dir"], "in")
        self.assertEqual(by[_path("DGLabBmtrPressure")]["dir"], "in")
        self.assertEqual(by[_path("DGLabBmtrPressure")]["type"], "Float")

    def test_unconnected_device_still_registers_nothing(self):
        """可改名栏归可改名栏，「设备没连上就不预登记」的闸门不动。"""
        for state in (None, EngineState(backend="v4"),
                      EngineState(backend="v4", connected=True),
                      EngineState(backend="v4", paired=True)):
            mod, ctx = _make_module(state)
            self.assertEqual(mod.temp_specs(), [])


class RenameVarTests(unittest.TestCase):
    def test_rename_input_moves_registered_path(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        self.assertEqual(mod.rename_var(_path("DGLabWaveA"), _path("MyWaveA")), "")
        keys = {row["key"] for row in mod.temp_specs()}
        self.assertIn(_path("MyWaveA"), keys)
        self.assertNotIn(_path("DGLabWaveA"), keys)
        self.assertEqual(ctx.settings["param_names"], {"in_wave_a": "MyWaveA"})
        self.assertEqual(default_input_name(ctx.settings, "in_wave_a"), "MyWaveA")
        self.assertEqual(ctx.settings.saved, 1)
        self.assertIn("OSC 参数改名", ctx.logs[-1])

    def test_rename_readwrite_row_moves_both_directions(self):
        """通道强度同名双向：一行改名，输入键与输出键一起换。"""
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        self.assertEqual(mod.rename_var(_path("DGLabStrengthA"), _path("MyStrA")), "")
        table = ctx.settings["param_names"]
        self.assertEqual(table["in_strength_a"], "MyStrA")
        self.assertEqual(table["COYOTE.StrengthA"], "MyStrA")
        by = {row["key"]: row for row in mod.temp_specs()}
        self.assertEqual(by[_path("MyStrA")]["dir"], "inout")
        self.assertNotIn(_path("DGLabStrengthA"), by)
        self.assertEqual(default_output_name(ctx.settings, "COYOTE.StrengthA"),
                         "MyStrA")

    def test_rename_global_action_keeps_prefix_path(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        self.assertEqual(mod.rename_var("DGLab/Action", "DGLab/Btn"), "")
        self.assertEqual(ctx.settings["param_names"], {"Action": "Btn"})
        keys = {row["key"] for row in mod.temp_specs()}
        self.assertIn("DGLab/Btn", keys)
        self.assertNotIn("DGLab/Action", keys)

    def test_link_params_follow_rename(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        bridge = _attach_bridge(mod, ctx)
        try:
            self.assertEqual(mod.rename_var(_path("DGLabWaveA"), _path("MyWaveA")), "")
            self.assertEqual(mod.rename_var(_path("DGLabStrengthA"), _path("MyStrA")), "")
            names = {row["name"] for row in mod.link_params()}
            self.assertIn(_path("MyWaveA"), names)
            self.assertIn(_path("MyStrA"), names)
            self.assertNotIn(_path("DGLabWaveA"), names)
        finally:
            bridge.close()

    def test_send_and_receive_paths_follow_rename(self):
        state = _state(("c1", "COYOTE_030"))
        state.slots["c1"].strength = {"A": 55, "B": 0}
        mod, ctx = _make_module(state)
        bridge = _attach_bridge(mod, ctx)
        try:
            self.assertEqual(mod.rename_var(_path("DGLabStrengthA"), _path("MyStrA")), "")
            self.assertEqual(mod.rename_var(_path("DGLabWaveA"), _path("MyWaveA")), "")
            self.assertEqual(mod.rename_var("DGLab/Action", "DGLab/Btn"), "")

            sent: list[tuple[str, object]] = []
            bridge.send_value = lambda addr, value: sent.append((addr, value))
            bridge._push_maintained(state)
            addrs = dict(sent)
            self.assertEqual(addrs[_addr("MyStrA")], 55)
            self.assertNotIn(_addr("DGLabStrengthA"), addrs)
            self.assertIn("/DGLab/Btn", addrs)
            self.assertNotIn("/DGLab/Action", addrs)

            written = bridge._written
            bridge._refresh_input_names(state)
            bridge._track_input("/avatar/parameters/MyWaveA", 3)
            self.assertEqual(written.get(_path("MyWaveA")), 3)
        finally:
            bridge.close()

    def test_rename_works_before_device_list_cached(self):
        """设备清单还没缓存：靠核心参数目录也能把改名落到正确的键上。"""
        mod, ctx = _make_module(None)
        self.assertEqual(mod.temp_specs(), [])
        self.assertEqual(
            mod.rename_var(_path("DGLabOvcInStrengthA"), _path("MyOvcA")), "")
        self.assertEqual(ctx.settings["param_names"],
                         {"in_ovc_strength_a": "MyOvcA"})
        self.assertEqual(default_input_name(ctx.settings, "in_ovc_strength_a"),
                         "MyOvcA")

    def test_rename_input_mapping_expression(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        ctx.settings["mappings"] = [{"param": "in_wave_a",
                                     "expr": "{DGLabWaveA} * 2"}]
        self.assertEqual(mod.rename_var(_path("DGLabWaveA"), _path("MyWaveA")), "")
        self.assertEqual(ctx.settings["mappings"][0]["expr"], "{MyWaveA} * 2")

    def test_rename_to_same_name_is_noop(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        self.assertEqual(mod.rename_var(_path("DGLabWaveA"), _path("DGLabWaveA")), "")
        self.assertNotIn("param_names", ctx.settings)


class RenameRejectionTests(unittest.TestCase):
    def test_unknown_parameter_returns_chinese_error(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        before = mod.temp_specs()
        note = mod.rename_var(_path("NothingHere"), _path("Whatever"))
        self.assertEqual(note, "找不到要改名的 OSC 参数")
        self.assertEqual(mod.temp_specs(), before)
        self.assertNotIn("param_names", ctx.settings)

    def test_collision_returns_chinese_error(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        before = mod.temp_specs()
        note = mod.rename_var(_path("DGLabWaveA"), _path("DGLabWaveStepA"))
        self.assertIn("已被其它 OSC 参数占用", note)
        self.assertEqual(mod.temp_specs(), before)
        self.assertNotIn("param_names", ctx.settings)

    def test_invalid_new_name_rejected(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        for bad in ("", "   ", "avatar/parameters/", "avatar/parameters/My/Wave/",
                    "1abc", "my wave", "avatar/parameters/我的参数"):
            note = mod.rename_var(_path("DGLabWaveA"), bad)
            self.assertTrue(note, bad)
            self.assertIn("变量名", note)
        self.assertNotIn("param_names", ctx.settings)
        self.assertIn(_path("DGLabWaveA"),
                      {row["key"] for row in mod.temp_specs()})

    def test_reload_config_pushes_overrides_into_bridge(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        bridge = _attach_bridge(mod, ctx)
        try:
            ctx.settings["param_names"] = {"in_wave_a": "MyWaveA"}
            asyncio.run(mod.reload_config())
            self.assertEqual(bridge.config["param_names"],
                             {"in_wave_a": "MyWaveA"})
        finally:
            bridge.close()


class OverrideSurvivalTests(unittest.TestCase):
    def test_on_load_keeps_param_names(self):
        mod = OscModule()
        ctx = _Ctx(_state(("c1", "COYOTE_030")))
        ctx.settings["param_names"] = {"in_wave_a": "MyWaveA"}
        ctx.settings["auto_wired"] = ["COYOTE.StrengthA"]
        mod.on_load(ctx)
        self.assertEqual(ctx.settings["param_names"], {"in_wave_a": "MyWaveA"})
        self.assertNotIn("auto_wired", ctx.settings)
        self.assertIn(_path("MyWaveA"),
                      {row["key"] for row in mod.temp_specs()})

    def test_on_load_normalizes_dirty_param_names(self):
        mod = OscModule()
        ctx = _Ctx(_state(("c1", "COYOTE_030")))
        ctx.settings["param_names"] = ["junk"]
        mod.on_load(ctx)
        self.assertEqual(ctx.settings["param_names"], {})

    def test_materialize_names_uses_override(self):
        ctx = _Ctx(_state(("c1", "COYOTE_030")))
        ctx.settings["mappings"] = [{"param": "in_wave_a", "expr": "{MyWaveA}"}]
        ctx.settings["param_names"] = {"in_wave_a": "MyWaveA"}
        self.assertTrue(materialize_names(ctx.settings))
        self.assertEqual(ctx.settings["mappings"][0]["name"], "MyWaveA")

    def test_migrate_legacy_keeps_param_names(self):
        from modules.osc_bridge.plugin import migrate_legacy

        ctx = _Ctx(_state(("c1", "COYOTE_030")))
        ctx.settings["param_names"] = {"in_wave_a": "MyWaveA"}
        ctx.settings["input_expr"] = {"in_wave_a": "{DGLabWaveA}"}
        self.assertTrue(migrate_legacy(ctx.settings))
        self.assertEqual(ctx.settings["param_names"], {"in_wave_a": "MyWaveA"})


if __name__ == "__main__":
    unittest.main()
