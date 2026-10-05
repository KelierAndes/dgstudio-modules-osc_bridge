"""设备接入自动接线（事件流 + 临时变量）回归测试。

口径：设备连接后模块**不写映射表**，而是以事件流卡片 + 模块维护临时
变量暴露设备全部可写/可读参数；引擎仅装载显式配置行，无默认兜底。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import unittest

from dglab.state import EngineState, Slot
from modules.osc_bridge.bridge import (OscBridge, OscConfig, effective_rows)
from modules.osc_bridge.plugin import OSC_CONFIG_DEFAULTS, _OUT_CARD, OscModule

CONFIG = {"prefix": "DGLab", "in_port": 19001,
          "device_prefixes": {"COYOTE": "DGLab", "OVC": "DGLabOvc",
                              "BMTR": "DGLabBmtr"}}


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
    def on(self, *args):
        pass

    def off(self, *args):
        pass

    def emit(self, event, *args):
        self.emitted.append((event, *args))

    def __init__(self):
        self.emitted: list[tuple] = []


class _Host:
    """宿主 ModuleHost 替身：reload 记录（不真正装载逻辑表）。"""

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
            asyncio.run(coro)     # 同步上下文：替身 reload 无 IO，直接跑完
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
        # 旧机制（默认直传/回传行兜底）已移除：空配置 → 引擎无任何行
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


class WireTests(unittest.TestCase):
    def test_device_connect_wires_events_not_mappings(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        mod._on_devices_changed(ctx.engine.get_state())

        cards = ctx.settings["events"]
        by_name = {card["name"]: card for card in cards}
        # 输入：每个核心输入参数一张「变量变更时」卡片（郊狼 11 + 急停）
        in_card = by_name["OSC 郊狼通道 A 强度（自动）"]
        self.assertEqual(in_card["trigger"], "change")
        self.assertEqual(in_card["arg"], "DGLabStrengthA")
        self.assertEqual(in_card["actions"],
                         [{"dir": "in", "param": "in_strength_a",
                           "var": "DGLabStrengthA"}])
        self.assertIn("OSC 急停（全部设备）（自动）", by_name)
        self.assertEqual(by_name["OSC 急停（全部设备）（自动）"]["arg"],
                         "DGLabEmergency")
        # 不应有负鼠（OVC）输入卡片
        self.assertFalse(any("负鼠" in name for name in by_name))
        # 输出：单张周期卡片，含郊狼/灵猫全部可读参数与全局 Action
        out_card = by_name[_OUT_CARD]
        self.assertEqual(out_card["trigger"], "period")
        actions = {a["param"]: a for a in out_card["actions"]}
        self.assertEqual(actions["BMTR.Pressure"],
                         {"dir": "out", "param": "BMTR.Pressure",
                          "var": "DGLabBmtrPressure",
                          "name": "DGLabBmtrPressure", "type": "Float"})
        self.assertEqual(actions["COYOTE.StrengthA"]["var"], "DGLabStrengthA")
        self.assertIn("Action", actions)
        # 映射表保持未写
        self.assertNotIn("mappings", ctx.settings)
        self.assertNotIn("outputs", ctx.settings)
        # 记账 + 通知宿主重载与界面刷新
        self.assertIn("in_strength_a", ctx.settings["auto_wired"])
        self.assertIn("BMTR.Pressure", ctx.settings["auto_wired"])
        self.assertEqual(ctx.engine.modules.reloaded, ["osc_bridge"])
        self.assertEqual(ctx.events.emitted, [("modules_changed", "osc_bridge")])

    def test_second_call_is_noop(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        mod._on_devices_changed(ctx.engine.get_state())
        count = len(ctx.settings["events"])
        mod._on_devices_changed(ctx.engine.get_state())
        self.assertEqual(len(ctx.settings["events"]), count)
        self.assertEqual(ctx.engine.modules.reloaded, ["osc_bridge"])

    def test_user_edits_survive(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        mod._on_devices_changed(ctx.engine.get_state())
        # 用户删除急停卡片与一个输出动作 → 记账在，设备变化不复活
        ctx.settings["events"] = [
            card for card in ctx.settings["events"]
            if card["name"] != "OSC 急停（全部设备）（自动）"]
        out_card = next(card for card in ctx.settings["events"]
                        if card["name"] == _OUT_CARD)
        out_card["actions"] = [a for a in out_card["actions"]
                               if a["param"] != "COYOTE.Battery"]
        mod._on_devices_changed(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        names = [card["name"] for card in ctx.settings["events"]]
        self.assertNotIn("OSC 急停（全部设备）（自动）", names)
        out_card = next(card for card in ctx.settings["events"]
                        if card["name"] == _OUT_CARD)
        self.assertNotIn("COYOTE.Battery",
                         [a["param"] for a in out_card["actions"]])
        # 新设备（灵猫）参数照常补齐
        self.assertIn("BMTR.Pressure",
                      [a["param"] for a in out_card["actions"]])


class TempSpecsTests(unittest.TestCase):
    def test_specs_follow_connected_devices(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        specs = {spec["key"]: spec for spec in mod.temp_specs()}
        self.assertEqual(specs["DGLabStrengthA"]["label"], "郊狼通道 A 强度")
        self.assertEqual(specs["DGLabBmtrPressure"]["label"], "灵猫气压 (kPa)")
        self.assertIn("DGLabAction", specs)
        self.assertFalse(any("负鼠" in str(spec["label"])
                             for spec in specs.values()))

    def test_no_state_no_specs(self):
        mod, ctx = _make_module(None)
        self.assertEqual(mod.temp_specs(), [])


class MirrorTests(unittest.TestCase):
    def test_received_params_mirror_to_temps(self):
        seen: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: None, None,
                           set_temp=lambda k, v: seen.__setitem__(k, v))
        try:
            bridge._track_input("/avatar/parameters/DGLabStrengthA", 42)
            self.assertEqual(seen.get("DGLabStrengthA"), 42)
            self.assertIn("DGLabStrengthA", bridge._mirrored)
        finally:
            bridge.close()

    def test_avatar_change_clears_mirrors(self):
        seen: dict[str, float] = {}
        bridge = OscBridge(OscConfig(dict(CONFIG)), lambda: None, None,
                           set_temp=lambda k, v: seen.__setitem__(k, v))
        try:
            bridge._track_input("/avatar/parameters/DGLabStrengthA", 42)
            bridge._track_input("/avatar/change", 1)
            self.assertEqual(seen.get("DGLabStrengthA"), 0)
            self.assertEqual(bridge._mirrored, set())
        finally:
            bridge.close()


class LinkParamsTests(unittest.TestCase):
    def test_pool_contains_default_names_of_connected_devices(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        mod.bridge = OscBridge(
            OscConfig(dict(CONFIG), defaults=OSC_CONFIG_DEFAULTS),
            ctx.engine.get_state, None)
        try:
            names = dict(mod.link_params())
            self.assertIn("DGLabStrengthA", names)      # 输入侧默认名
            self.assertIn("DGLabEmergency", names)
            self.assertIn("DGLabBmtrPressure", names)   # 输出侧默认名
            self.assertIn("DGLabAction", names)
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
    """接线 → 宿主装载事件流 → OSC 收包派发/输出回传 全链路。"""

    async def test_wiring_drives_dispatch_and_push(self):
        state = _state(("c1", "COYOTE_030"))
        calls: list[tuple] = []

        class Commands:
            async def set_strength(self, ch, v, slot_id=None):
                calls.append(("set", ch, v, slot_id))

            async def set_wave(self, ch, name, slot_id=None):
                pass

            async def zap(self, ch, sec, slot_id=None):
                pass

            async def fire_start(self, slot_id=None, channel=None):
                pass

            async def fire_stop(self, slot_id=None, channel=None):
                pass

            async def emergency_stop(self):
                pass

        mod, ctx = _make_module(state)
        ctx.settings.clear()
        ctx.settings.update(dict(CONFIG, in_port=_free_port()))

        class Host(_Host):
            async def reload(self, module_id: str):
                self.reloaded.append(module_id)
                eng = mod.bridge.engine
                eng.set_temp_rows(mod.ctx.settings.get("temps"))
                eng.set_event_cards(mod.ctx.settings.get("events"))

        ctx.engine.modules = Host()

        cfg = OscConfig(dict(ctx.settings), defaults=OSC_CONFIG_DEFAULTS)
        mod.bridge = OscBridge(cfg, ctx.engine.get_state, Commands(),
                               events=ctx.events,
                               on_devices_changed=mod._on_devices_changed,
                               set_temp=mod._mirror_temp)
        mod.bridge.log = lambda msg: None
        await mod.bridge.start()
        try:
            eng = mod.bridge.engine
            # 推送循环首拍会触发接线；这里手动触发加速
            mod.bridge._check_devices(state)
            self.assertIn("in_strength_a", ctx.settings["auto_wired"])
            await asyncio.sleep(0.1)   # 宿主 reload 任务装载事件卡片
            self.assertTrue(eng._cards)
            eng.tick_event_cards()     # 首拍采基线（未收到包 → 基线 0）
            # OSC 收包 → 变更卡片直派设备
            from pythonosc.udp_client import SimpleUDPClient

            sender = SimpleUDPClient("127.0.0.1", cfg["in_port"])
            sender.send_message("/avatar/parameters/DGLabStrengthA", [42])
            for _ in range(40):
                await asyncio.sleep(0.05)
                if ctx.temps.get("DGLabStrengthA") == 42:
                    break
            self.assertEqual(ctx.temps.get("DGLabStrengthA"), 42)
            eng.tick_event_cards()
            await asyncio.sleep(0.05)   # 派发经 create_task 调度，让出一拍
            self.assertIn(("set", "A", 42, "c1"), calls, calls)
            # 输出卡片周期触发 → 回传值就绪
            state.slots["c1"].strength = {"A": 42, "B": 0}
            eng.tick_event_cards()
            self.assertIn("DGLabStrengthA", eng.out_values)
        finally:
            await mod.bridge.stop()
            mod.bridge.close()


if __name__ == "__main__":
    unittest.main()
