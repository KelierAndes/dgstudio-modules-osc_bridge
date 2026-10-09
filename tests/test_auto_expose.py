from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from dglab.state import EngineState, Slot
from modules.osc_bridge.bridge import (TEMP_PATH_PREFIX, OscBridge, OscConfig,
                                       effective_rows)
from modules.osc_bridge.plugin import OSC_CONFIG_DEFAULTS, OscModule

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


def _free_port() -> int:
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Settings(dict):
    def save(self):
        self.saved = getattr(self, "saved", 0) + 1


class _Events:
    def __init__(self):
        self.emitted: list[tuple] = []

    def on(self, *args):
        pass

    def off(self, *args):
        pass

    def emit(self, event, *args):
        self.emitted.append((event, *args))


class _Host:
    def __init__(self):
        self.reloaded: list[str] = []

    async def reload(self, module_id: str):
        self.reloaded.append(module_id)


class _Ctx:
    def __init__(self, state=None):
        self.settings = _Settings(dict(CONFIG))
        self.engine = self._Engine(state)
        self.engine.modules = _Host()
        self.events = _Events()
        self.temps: dict[str, float] = {}
        self.log = lambda msg: None
        self.submitted = 0

    def set_temp(self, key, value):
        from dglab.mapping import as_number
        num = as_number(value)
        if num is not None:
            self.temps[str(key)] = num

    def submit(self, coro):
        self.submitted += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return None
        loop.create_task(coro)
        return None

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


class EffectiveRowsTests(unittest.TestCase):
    def test_no_default_rows_without_config(self):
        rows_in, rows_out = effective_rows({}, None)
        self.assertEqual(rows_in, [])
        self.assertEqual(rows_out, [])

    def test_explicit_rows_pass_through(self):
        rows_in, rows_out = effective_rows(
            {"mappings": [{"param": "in_strength_a", "expr": "{MyStr}"}],
             "outputs": [{"param": "COYOTE.Battery", "name": "Bat",
                          "expr": "{COYOTE.Battery}", "type": "Int"}]},
            None)
        self.assertEqual([row["param"] for row in rows_in],
                         ["in_strength_a"])
        self.assertEqual([row["param"] for row in rows_out],
                         ["COYOTE.Battery"])


class MaintainedSpecTests(unittest.TestCase):
    def test_specs_cover_all_core_params_of_devices(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        specs = {spec["key"]: spec for spec in mod.temp_specs()}
        self.assertIn(_path("DGLabStrengthA"), specs)
        self.assertIn(_path("DGLabBmtrPressure"), specs)
        self.assertIn("DGLab/Action", specs)
        self.assertIn(_path("DGLabWaveA"), specs)
        self.assertIn(_path("DGLabWaveStepB"), specs)
        self.assertIn(_path("DGLabFire"), specs)
        self.assertIn(_path("DGLabEmergency"), specs)
        self.assertFalse(any("负鼠" in str(spec["label"])
                             for spec in specs.values()))
        self.assertEqual(specs[_path("DGLabStrengthA")]["dir"], "inout")
        self.assertEqual(specs[_path("DGLabBmtrPressure")]["dir"], "out")
        self.assertEqual(specs[_path("DGLabEmergency")]["dir"], "in")
        self.assertEqual(specs["DGLab/Action"]["dir"], "out")

    def test_registration_leaves_config_alone(self):
        """登记只由模块实时算出：不再往配置文件写 temps 行。"""
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        mod._on_devices_changed(ctx.engine.get_state())
        names = {spec["key"] for spec in mod.temp_specs()}
        self.assertIn(_path("DGLabStrengthA"), names)
        self.assertIn("DGLab/Action", names)
        self.assertNotIn("temps", ctx.settings)
        self.assertNotIn("auto_wired", ctx.settings)
        self.assertNotIn("auto_exposed", ctx.settings)

    def test_legacy_config_rows_are_dropped(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        ctx.settings["temps"] = [{"name": "avatar/parameters/MyOwn",
                                  "dir": "out", "label": "自定义"}]
        mod._on_devices_changed(ctx.engine.get_state())
        self.assertNotIn("temps", ctx.settings)
        self.assertNotIn("avatar/parameters/MyOwn",
                         {spec["key"] for spec in mod.temp_specs()})

    def test_no_device_registers_nothing(self):
        """设备没连上就不预登记：空槽位与无 state 两种情况都应为空。"""
        for state in (None, EngineState(backend="v4")):
            mod, ctx = _make_module(state)
            self.assertEqual(mod.temp_specs(), [])
            self.assertEqual([name for name, _label in mod.link_params()], [])

    def test_only_connected_families_register(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        keys = {spec["key"] for spec in mod.temp_specs()}
        self.assertIn("DGLab/Action", keys)
        self.assertIn(_path("DGLabStrengthA"), keys)
        self.assertNotIn(_path("DGLabOvcStrengthA"), keys)
        self.assertNotIn(_path("DGLabBmtrPressure"), keys)

    def test_stale_ledgers_cleaned_on_load_and_connect(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        ctx.settings["auto_wired"] = ["COYOTE.StrengthA"]
        ctx.settings["auto_exposed"] = ["in_strength_a"]
        mod._on_devices_changed(ctx.engine.get_state())
        self.assertNotIn("auto_wired", ctx.settings)
        self.assertNotIn("auto_exposed", ctx.settings)


class MirrorTests(unittest.TestCase):
    def test_received_input_params_mirror_to_path_temps(self):
        state = _state(("c1", "COYOTE_030"))
        written: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: state, None,
                           set_temp=lambda k, v: written.__setitem__(k, v))
        try:
            bridge._refresh_input_names(state)
            self.assertIn("DGLabWaveA", bridge._input_names)
            bridge._track_input("/avatar/parameters/DGLabWaveA", 3)
            self.assertEqual(written.get(_path("DGLabWaveA")), 3)
            self.assertIn(_path("DGLabWaveA"), bridge._no_send)
        finally:
            bridge.close()

    def test_unknown_params_not_mirrored(self):
        state = _state(("c1", "COYOTE_030"))
        written: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: state, None,
                           set_temp=lambda k, v: written.__setitem__(k, v))
        try:
            bridge._refresh_input_names(state)
            bridge._track_input("/avatar/parameters/blood", 120)
            self.assertEqual(written, {})
        finally:
            bridge.close()

    def test_mirror_never_sent_back(self):
        state = _state(("c1", "COYOTE_030"))
        written: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: state, None,
                           set_temp=lambda k, v: written.__setitem__(k, v))
        try:
            sent: list[tuple[str, object]] = []
            bridge.send_value = lambda a, v: sent.append((a, v))
            bridge._refresh_input_names(state)
            bridge._track_input("/avatar/parameters/DGLabWaveA", 3)
            bridge.engine.temps[_path("DGLabWaveA")] = 3.0
            bridge._push_values()
            self.assertNotIn(_addr("DGLabWaveA"), [a for a, _v in sent])
        finally:
            bridge.close()

    def test_output_maintenance_wins_over_same_name_input(self):
        state = _state(("c1", "COYOTE_030"))
        state.slots["c1"].strength = {"A": 55, "B": 0}
        written: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: state, None,
                           set_temp=lambda k, v: written.__setitem__(k, v))
        try:
            bridge._refresh_input_names(state)
            bridge._push_maintained(state)
            self.assertIn(_path("DGLabStrengthA"), bridge._maintained_keys)
            bridge._track_input("/avatar/parameters/DGLabStrengthA", 42)
            self.assertEqual(written.get(_path("DGLabStrengthA")), 55)
        finally:
            bridge.close()


class MaintainedPushTests(unittest.TestCase):
    def _bridge(self, state, written=None):
        written = {} if written is None else written
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: state, None,
                           set_temp=lambda k, v: written.__setitem__(k, v))
        sent: list[tuple[str, object]] = []
        bridge.send_value = lambda addr, value: sent.append((addr, value))
        return bridge, sent, written

    def test_maintained_values_written_and_sent(self):
        state = _state(("c1", "COYOTE_030"), ("b1", "BMTR_1"))
        state.slots["c1"].strength = {"A": 55, "B": 0}
        state.slots["b1"].pressure = 7.9125
        bridge, sent, written = self._bridge(state)
        try:
            bridge._push_maintained(state)
            addrs = {addr: value for addr, value in sent}
            self.assertEqual(addrs[_addr("DGLabStrengthA")], 55)
            self.assertEqual(addrs[_addr("DGLabBmtrPressure")], 7.912)
            self.assertIs(addrs[_addr("DGLabConnected")], True)
            self.assertIn("/DGLab/Action", addrs)
            self.assertEqual(addrs["/DGLab/Action"], 0)
            self.assertEqual(written.get(_path("DGLabStrengthA")), 55)
            count = len(sent)
            bridge._push_maintained(state)
            self.assertEqual(len(sent), count)
        finally:
            bridge.close()

    def test_maintained_resend_on_change(self):
        state = _state(("c1", "COYOTE_030"))
        bridge, sent, _ = self._bridge(state)
        try:
            state.slots["c1"].strength = {"A": 10, "B": 0}
            bridge._push_maintained(state)
            state.slots["c1"].strength = {"A": 20, "B": 0}
            bridge._push_maintained(state)
            self.assertEqual([v for a, v in sent if a.endswith("StrengthA")],
                             [10, 20])
        finally:
            bridge.close()

    def test_no_state_is_noop(self):
        bridge, sent, written = self._bridge(None)
        try:
            bridge._push_maintained(None)
            self.assertEqual(sent, [])
            self.assertEqual(written, {})
        finally:
            bridge.close()


class UserTempRowTests(unittest.TestCase):

    def test_path_temps_evaluated_and_sent(self):
        bridge = OscBridge(OscConfig(dict(CONFIG, temps=[
            {"name": _path("DGLabStrengthA"),
             "expr": "{COYOTE.StrengthA} * 2"},
            {"name": "plain_temp", "expr": "1"}])), lambda: None, None)
        bridge.apply_config()
        try:
            sent: list[tuple[str, object]] = []
            bridge.send_value = lambda addr, value: sent.append((addr, value))
            bridge.engine.signals["COYOTE.StrengthA"] = 10.5
            bridge._push_values()
            self.assertIn((_addr("DGLabStrengthA"), 21.0), sent)
            self.assertNotIn("/plain_temp", [a for a, _v in sent])
        finally:
            bridge.close()


class LinkParamsTests(unittest.TestCase):
    def test_pool_contains_paths_of_connected_devices(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        mod.bridge = OscBridge(
            OscConfig(dict(CONFIG), defaults=OSC_CONFIG_DEFAULTS),
            ctx.engine.get_state, None)
        try:
            names = dict(mod.link_params())
            self.assertIn(_path("DGLabStrengthA"), names)
            self.assertIn(_path("DGLabBmtrPressure"), names)
            self.assertIn(_path("DGLabEmergency"), names)
            self.assertIn("DGLab/Action", names)
        finally:
            mod.bridge.close()


class NotifyTests(unittest.TestCase):
    def test_bridge_notifies_on_device_sig_change(self):
        seen: list = []
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: None, None,
                           on_devices_changed=seen.append)
        try:
            bridge._check_devices(_state(("c1", "COYOTE_030")))
            self.assertEqual(len(seen), 1)
            bridge._check_devices(_state(("c1", "COYOTE_030")))
            self.assertEqual(len(seen), 1)
            bridge._check_devices(_state(("c1", "COYOTE_030"),
                                         ("b1", "BMTR_1")))
            self.assertEqual(len(seen), 2)
        finally:
            bridge.close()


class EndToEndTests(unittest.IsolatedAsyncioTestCase):

    async def test_maintained_flow_to_osc(self):
        state = _state(("c1", "COYOTE_030"))
        state.slots["c1"].strength = {"A": 42, "B": 0}

        mod, ctx = _make_module(state)
        ctx.settings.clear()
        ctx.settings.update(dict(CONFIG, in_port=_free_port()))

        class Commands:
            async def set_strength(self, ch, v, slot_id=None):
                pass

        class LegacyHost:
            pass

        ctx.engine.modules = LegacyHost()
        mod.ctx = ctx
        cfg = OscConfig(dict(ctx.settings), defaults=OSC_CONFIG_DEFAULTS)
        mod.bridge = OscBridge(cfg, ctx.engine.get_state, Commands(),
                               events=ctx.events,
                               on_devices_changed=mod._on_devices_changed,
                               set_temp=mod._write_temp)
        mod.bridge.log = lambda msg: None
        sent: list[tuple[str, object]] = []
        mod.bridge.send_value = lambda addr, value: sent.append((addr, value))
        await mod.bridge.start()
        try:
            mod.bridge._check_devices(state)
            await asyncio.sleep(0.1)
            self.assertIn(_path("DGLabStrengthA"),
                          [s["key"] for s in mod.temp_specs()])
            mod.bridge._push_maintained(state)
            addrs = {addr: value for addr, value in sent}
            self.assertEqual(addrs[_addr("DGLabStrengthA")], 42)
            self.assertEqual(ctx.temps.get(_path("DGLabStrengthA")), 42)
            self.assertEqual(ctx.settings.get("events") or [], [])
            self.assertNotIn("temps", ctx.settings)
        finally:
            await mod.bridge.stop()
            mod.bridge.close()


if __name__ == "__main__":
    unittest.main()
