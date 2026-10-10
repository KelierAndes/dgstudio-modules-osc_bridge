from __future__ import annotations

import asyncio
import unittest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from dglab.state import EngineState, Slot
from dglab.waves import CONTINUOUS, PULSE_STREAM, SILENT, wave_order
from modules.osc_bridge.bridge import (OscBridge, OscConfig,
                                       TEMP_PATH_PREFIX, default_input_name)


def free_udp_port() -> int:
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _state(*slots: tuple[str, str]) -> EngineState:
    state = EngineState(backend="v4", paired=True, connected=True)
    for sid, dtype in slots:
        state.slots[sid] = Slot(slot_id=sid, type=dtype)
    return state


class WaveOrderTests(unittest.TestCase):
    """波形表顺序是事件流写入卡的依据，模块侧不再自己步进，但顺序不能漂。"""

    def test_wave_order_layout(self):
        order = wave_order("COYOTE")
        self.assertEqual(order[0], SILENT)
        self.assertEqual(order[1], CONTINUOUS)
        self.assertEqual(len(order), 27)
        self.assertEqual(order[-1], PULSE_STREAM)
        from dglab.official_waveforms_ovc import OvcWaveform

        self.assertEqual(wave_order("OVC")[2:-1], [w.value for w in OvcWaveform])
        self.assertEqual(wave_order("OVC")[-1], PULSE_STREAM)


class OscReceiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_rx_probe_tracks_packets(self):
        state = _state(("coyote-1", "COYOTE_030"))
        port = free_udp_port()
        bridge = OscBridge(OscConfig({"in_port": port}), lambda: state)
        await bridge.start()
        try:
            from pythonosc.udp_client import SimpleUDPClient

            sender = SimpleUDPClient("127.0.0.1", port)
            sender.send_message("/avatar/parameters/UnmappedParam", [1])
            sender.send_message("/avatar/parameters/DGLabStrengthA", [5])
            for _ in range(50):
                if bridge.rx_count >= 2:
                    break
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(bridge.rx_count, 2)
            self.assertIsNotNone(bridge.last_rx)
        finally:
            await bridge.stop()

    def test_received_value_mirrors_into_named_variable(self):
        """登记过的头像参数收到即镜像进变量表；没登记的只进动态参数表。"""
        state = _state(("coyote-1", "COYOTE_030"))
        written: dict[str, object] = {}
        bridge = OscBridge(OscConfig(), lambda: state, set_temp=written.__setitem__)
        bridge._track_input("/avatar/parameters/DGLabStrengthA", 55)
        bridge._track_input("/avatar/parameters/Whatever", 9)
        self.assertEqual(
            written.get(TEMP_PATH_PREFIX + default_input_name(
                bridge.config, "in_strength_a")), 55)
        self.assertNotIn(TEMP_PATH_PREFIX + "Whatever", written)
        self.assertIn("Whatever", bridge.param_names())
        # 镜像进来的值不再回发，避免头像自己回声
        self.assertIn(TEMP_PATH_PREFIX + "DGLabStrengthA", bridge._no_send)

    def test_received_values_surface_as_module_signals(self):
        """宿主按 `bridge.engine.signals` 取模块读数，接口名与语义都不能动。

        核心 `flow_host.module_signals()` 取不到时是静默返回空的（被 except 兜住），
        画布上只会表现为「一直没有数据」，所以这条挂接要有用例钉住。
        """
        bridge = OscBridge(OscConfig(), lambda: None)
        bridge._track_input("/avatar/parameters/blood", 120)
        bridge._track_input("/avatar/parameters/flag", True)
        bridge._track_input("/avatar/parameters/text", "abc")
        self.assertEqual(bridge.engine.signals.get("blood"), 120.0)
        self.assertEqual(bridge.engine.signals.get("flag"), 1.0)
        self.assertNotIn("text", bridge.engine.signals)
        self.assertEqual(bridge.input_values["text"]["value"], "abc")
        bridge._track_input("/avatar/change")
        self.assertEqual(bridge.engine.signals, {})

    def test_avatar_change_forces_resend(self):
        bridge = OscBridge(OscConfig(), lambda: None)
        bridge.engine.temps[TEMP_PATH_PREFIX + "X"] = 1
        sent: list[tuple[str, object]] = []
        bridge.send_value = lambda addr, value: sent.append((addr, value))
        bridge._push_values()
        bridge._push_values()
        self.assertEqual(len(sent), 1)
        bridge._track_input("/avatar/change")
        bridge._push_values()
        self.assertEqual(len(sent), 2)


class OscSendTests(unittest.TestCase):
    """发出去的东西只来自两处：设备读数与宿主写进共享变量表的值。"""

    def test_host_written_variable_is_sent_at_its_own_path(self):
        bridge = OscBridge(OscConfig(), lambda: None)
        shared: dict[str, float] = {}
        bridge.engine.attach_temps(shared)
        shared[TEMP_PATH_PREFIX + "EventFlowOut"] = 7.5
        shared["plain_variable"] = 3          # 不带路径的普通变量不发
        sent: dict[str, object] = {}
        bridge.send_value = lambda addr, value: sent.__setitem__(addr, value)
        bridge._push_values()
        self.assertEqual(sent, {"/" + TEMP_PATH_PREFIX + "EventFlowOut": 7.5})

    def test_pump_is_a_safe_hook(self):
        bridge = OscBridge(OscConfig(), lambda: None)
        bridge.engine.attach_temps({TEMP_PATH_PREFIX + "A": 1})
        bridge.engine.pump()                  # 宿主 set_temp 后调它，不能抛
        sent: list = []
        bridge.send_value = lambda *a: sent.append(a)
        bridge._push_values()
        self.assertEqual(sent, [(("/" + TEMP_PATH_PREFIX + "A"), 1)])

    def test_no_command_surface_left(self):
        """派发层已经拆掉：桥不再握着核心命令，也没有表达式求值的入口。"""
        bridge = OscBridge(OscConfig(), lambda: None)
        for name in ("commands", "dispatchers", "input_target_slot",
                     "_dispatch"):
            self.assertFalse(hasattr(bridge, name), name)
        for name in ("set_mappings", "set_outputs", "signal", "out_values",
                     "last_values", "values"):
            self.assertFalse(hasattr(bridge.engine, name), name)


class OscRealUdpTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_round_trip(self):
        state = _state(("coyote-1", "COYOTE_030"))
        port = free_udp_port()
        cfg = OscConfig({"in_port": port, "out_port": free_udp_port(),
                         "rate_hz": 50})
        bridge = OscBridge(cfg, lambda: state)
        await bridge.start()
        self.assertTrue(bridge._running)
        from pythonosc.udp_client import SimpleUDPClient

        SimpleUDPClient("127.0.0.1", port).send_message(
            "/" + TEMP_PATH_PREFIX + "DGLabBattery", [88])
        await asyncio.sleep(0.3)
        self.assertEqual(bridge.input_values.get("DGLabBattery", {}).get("value"),
                         88)
        await bridge.stop()
        self.assertFalse(bridge._running)


if __name__ == "__main__":
    unittest.main()
