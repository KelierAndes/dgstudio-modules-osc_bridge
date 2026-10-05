"""设备接入自动建临时变量（路径命名回传）回归测试。

口径：设备连接后模块**只建临时变量**（变量名 = 完整 OSC 回传路径，
表达式取核心输出信号），不建事件流、不写映射表；桥接把路径型临时
变量按变量名回传（值变化才发、按来源参数类型归一）。
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
from modules.osc_bridge.plugin import (OSC_CONFIG_DEFAULTS, _temp_path,
                                       OscModule, _strip_auto_cards)

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
    """宿主 ModuleHost 替身：reload 记录并真实装载临时变量表。"""

    def __init__(self, mod: OscModule | None = None):
        self._mod = mod
        self.reloaded: list[str] = []

    async def reload(self, module_id: str):
        self.reloaded.append(module_id)
        if self._mod is not None and self._mod.bridge is not None:
            self._mod.bridge.engine.set_temp_rows(
                self._mod.ctx.settings.get("temps"))


class _Ctx:
    def __init__(self, state=None):
        self.settings = _Settings(dict(CONFIG))
        self.engine = self._Engine(state)
        self.engine.modules = _Host()
        self.events = _Events()
        self.log = lambda msg: None
        self.submitted = 0

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


class TempRowTests(unittest.TestCase):
    def test_device_connect_creates_path_temps_only(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        mod._on_devices_changed(ctx.engine.get_state())

        rows = {row["name"]: row for row in ctx.settings["temps"]}
        # 变量名 = 完整 OSC 回传路径，表达式取核心输出信号
        self.assertEqual(rows[_temp_path("DGLabBmtrPressure")],
                         {"name": _temp_path("DGLabBmtrPressure"),
                          "expr": "{BMTR.Pressure}"})
        self.assertEqual(rows[_temp_path("DGLabStrengthA")]["expr"],
                         "{COYOTE.StrengthA}")
        self.assertIn(_temp_path("DGLabAction"), rows)
        self.assertIn(_temp_path("DGLabConnected"), rows)
        # 不建事件流、不写映射表
        self.assertNotIn("events", ctx.settings)
        self.assertNotIn("mappings", ctx.settings)
        self.assertNotIn("outputs", ctx.settings)
        # 记账 + 通知宿主重载与界面刷新
        self.assertIn("BMTR.Pressure", ctx.settings["auto_wired"])
        self.assertIn("Action", ctx.settings["auto_wired"])
        self.assertEqual(ctx.engine.modules.reloaded, ["osc_bridge"])
        self.assertEqual(ctx.events.emitted, [("modules_changed", "osc_bridge")])

    def test_second_call_is_noop(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        mod._on_devices_changed(ctx.engine.get_state())
        count = len(ctx.settings["temps"])
        mod._on_devices_changed(ctx.engine.get_state())
        self.assertEqual(len(ctx.settings["temps"]), count)
        self.assertEqual(ctx.engine.modules.reloaded, ["osc_bridge"])

    def test_user_renames_and_deletes_survive(self):
        mod, ctx = _make_module(_state(("c1", "COYOTE_030")))
        mod._on_devices_changed(ctx.engine.get_state())
        # 用户改名（自定义回传地址）与删除各一行
        rows = ctx.settings["temps"]
        for row in rows:
            if row["name"] == _temp_path("DGLabStrengthA"):
                row["name"] = _temp_path("MyStrength")
        ctx.settings["temps"] = [row for row in rows
                                 if row["name"] != _temp_path("DGLabBattery")]
        mod._on_devices_changed(_state(("c1", "COYOTE_030"),
                                       ("b1", "BMTR_1")))
        names = {row["name"] for row in ctx.settings["temps"]}
        self.assertIn(_temp_path("MyStrength"), names)      # 改名保留
        self.assertNotIn(_temp_path("DGLabStrengthA"), names)  # 不重建默认名
        self.assertNotIn(_temp_path("DGLabBattery"), names)  # 删除不复活
        # 新设备（灵猫）变量照常补齐
        self.assertIn(_temp_path("DGLabBmtrPressure"), names)


class StripAutoCardsTests(unittest.TestCase):
    def test_v18_auto_cards_removed_user_cards_kept(self):
        settings = {"events": [
            {"name": "OSC 郊狼通道 A 强度（自动）", "trigger": "change",
             "arg": "DGLabStrengthA", "actions": []},
            {"name": "OSC 状态回传（自动）", "trigger": "period",
             "arg": 50, "actions": []},
            {"name": "我的事件", "trigger": "period", "arg": 100,
             "actions": []},
        ]}
        self.assertTrue(_strip_auto_cards(settings))
        self.assertEqual([c["name"] for c in settings["events"]], ["我的事件"])
        self.assertFalse(_strip_auto_cards(settings))


class PushTempsTests(unittest.TestCase):
    def _bridge(self, temp_rows):
        bridge = OscBridge(OscConfig(dict(CONFIG, temps=temp_rows)),
                           lambda: None, None)
        sent: list[tuple[str, object]] = []
        bridge.send_value = lambda addr, value: sent.append((addr, value))
        return bridge, sent

    def test_path_temps_are_sent_by_name(self):
        bridge, sent = self._bridge([
            {"name": _temp_path("DGLabStrengthA"),
             "expr": "{COYOTE.StrengthA}"},
            {"name": _temp_path("DGLabBmtrPressure"),
             "expr": "{BMTR.Pressure}"},
            {"name": _temp_path("DGLabConnected"),
             "expr": "{COYOTE.Connected}"},
            {"name": "plain_temp", "expr": "1"},          # 非路径行不回传
            {"name": _temp_path("Broken"), "expr": "{"},  # 非法表达式跳过
        ])
        bridge.apply_config()
        try:
            bridge.engine.signals["COYOTE.StrengthA"] = 42.0
            bridge.engine.signals["BMTR.Pressure"] = 7.9125
            bridge.engine.signals["COYOTE.Connected"] = 1.0
            bridge._push_values()

            addrs = {addr: value for addr, value in sent}
            self.assertEqual(addrs["/avatar/parameters/DGLabStrengthA"], 42)
            self.assertEqual(addrs["/avatar/parameters/DGLabBmtrPressure"],
                             7.912)          # Float 三位
            self.assertIs(addrs["/avatar/parameters/DGLabConnected"], True)
            self.assertNotIn("/plain_temp", addrs)
            self.assertNotIn("/avatar/parameters/Broken", addrs)
            # 值未变不重发
            bridge._push_values()
            self.assertEqual(len(sent), 3)
        finally:
            bridge.close()

    def test_changed_value_and_custom_expr_resend(self):
        bridge, sent = self._bridge([
            {"name": _temp_path("DGLabStrengthA"),
             "expr": "{COYOTE.StrengthA} * 2"}])
        bridge.apply_config()
        try:
            bridge.engine.signals["COYOTE.StrengthA"] = 10.5
            bridge._push_values()
            bridge.engine.signals["COYOTE.StrengthA"] = 15.0
            bridge._push_values()
            # 自定义（非裸引用）表达式原样发送，值变化才重发
            self.assertEqual(sent, [("/avatar/parameters/DGLabStrengthA", 21.0),
                                    ("/avatar/parameters/DGLabStrengthA",
                                     30.0)])
        finally:
            bridge.close()

    def test_device_vars_feed_evaluation(self):
        # 核心输出参数实时值来自设备状态（device_vars），无需外部信号
        state = _state(("c1", "COYOTE_030"))
        state.slots["c1"].strength = {"A": 80, "B": 0}
        state.slots["c1"].strength_limit = {"A": 200, "B": 200}
        bridge = OscBridge(OscConfig(dict(CONFIG, temps=[
            {"name": _temp_path("DGLabStrengthA"),
             "expr": "{COYOTE.StrengthA}"}])),
            lambda: state, None)
        bridge.apply_config()
        try:
            sent: list[tuple[str, object]] = []
            bridge.send_value = lambda addr, value: sent.append((addr, value))
            bridge._push_values()
            self.assertEqual(sent,
                             [("/avatar/parameters/DGLabStrengthA", 80)])
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
            self.assertIn(_temp_path("DGLabStrengthA"), names)
            self.assertIn(_temp_path("DGLabBmtrPressure"), names)
            self.assertIn(_temp_path("DGLabAction"), names)
            self.assertIn("DGLabEmergency", names)   # 输入侧默认名仍可用
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
    """建变量 → 桥接自算求值 → 按路径回传 全链路（旧核心路径：宿主无
    reload/临时变量装载能力，回传完全由模块承担）。"""

    async def test_temp_rows_flow_to_osc(self):
        state = _state(("c1", "COYOTE_030"))
        state.slots["c1"].strength = {"A": 42, "B": 0}

        mod, ctx = _make_module(state)
        ctx.settings.clear()
        ctx.settings.update(dict(CONFIG, in_port=_free_port()))

        class Commands:
            async def set_strength(self, ch, v, slot_id=None):
                pass

        class LegacyHost:      # 旧核心宿主：无 reload/临时变量装载能力
            pass

        ctx.engine.modules = LegacyHost()
        mod.ctx = ctx
        cfg = OscConfig(dict(ctx.settings), defaults=OSC_CONFIG_DEFAULTS)
        mod.bridge = OscBridge(cfg, ctx.engine.get_state, Commands(),
                               events=ctx.events,
                               on_devices_changed=mod._on_devices_changed)
        mod.bridge.log = lambda msg: None
        sent: list[tuple[str, object]] = []
        mod.bridge.send_value = lambda addr, value: sent.append((addr, value))
        await mod.bridge.start()
        try:
            eng = mod.bridge.engine
            mod.bridge._check_devices(state)   # 推送循环首拍会触发，这里手动加速
            self.assertIn(_temp_path("DGLabStrengthA"),
                          [r["name"] for r in ctx.settings["temps"]])
            await asyncio.sleep(0.1)
            # 桥接自持行表已装载（不依赖宿主 reload），求值回传设备当前强度
            self.assertIn(_temp_path("DGLabStrengthA"),
                          [r["name"] for r in mod.bridge._temp_rows])
            mod.bridge._push_values()
            addrs = {addr: value for addr, value in sent}
            self.assertEqual(addrs["/avatar/parameters/DGLabStrengthA"], 42)
            # 未建立事件流
            self.assertEqual(ctx.settings.get("events") or [], [])
        finally:
            await mod.bridge.stop()
            mod.bridge.close()


if __name__ == "__main__":
    unittest.main()
