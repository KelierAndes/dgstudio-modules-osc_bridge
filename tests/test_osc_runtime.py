from __future__ import annotations

import asyncio
import unittest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from dglab.official_waveforms_ovc import OvcWaveform
from dglab.state import EngineState, Slot
from dglab.waves import CONTINUOUS, PULSE_STREAM, SILENT
from modules.osc_bridge.bridge import OscBridge, OscConfig, default_input_rows


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


class OscFamilyInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_inputs_target_first_device_per_family(self):
        state = _state(("ovc-1", "OVC_1"), ("coyote-1", "COYOTE_030"),
                       ("coyote-2", "COYOTE_030"))

        calls: list[tuple[str, int, str | None]] = []

        class Commands:
            async def set_strength(self, ch, v, slot_id=None):
                calls.append(("set", ch, slot_id))

            async def set_wave(self, ch, name, slot_id=None):
                calls.append(("wave", ch, slot_id))

            async def zap(self, ch, sec, slot_id=None):
                calls.append(("zap", ch, slot_id))

            async def emergency_stop(self):
                calls.append(("stop",))

        port = free_udp_port()
        cfg = OscConfig({"in_port": port})
        cfg["mappings"] = default_input_rows(cfg)
        bridge = OscBridge(cfg, lambda: state, Commands())
        await bridge.start()
        try:
            from pythonosc.udp_client import SimpleUDPClient

            sender = SimpleUDPClient("127.0.0.1", port)
            sender.send_message("/avatar/parameters/DGLabStrengthA", [42])
            sender.send_message("/avatar/parameters/DGLabOvcInStrengthA", [77])
            sender.send_message("/avatar/parameters/DGLabOvcInStrengthB", [3])
            await asyncio.sleep(0.4)
        finally:
            await bridge.stop()

        assert ("set", "A", "coyote-1") in calls, calls
        assert ("set", "A", "ovc-1") in calls, calls
        assert ("set", "B", "ovc-1") in calls, calls
        assert not any(c[2] == "coyote-2" for c in calls), calls

    async def test_wave_direct_and_step_inputs(self):
        from dglab.waves import wave_order

        order = wave_order("COYOTE")
        assert order[0] == SILENT and order[1] == CONTINUOUS
        assert len(order) == 27
        assert order[-1] == PULSE_STREAM
        assert wave_order("OVC")[2:-1] == [w.value for w in OvcWaveform]
        assert wave_order("OVC")[-1] == PULSE_STREAM

        state = _state(("coyote-1", "COYOTE_030"))

        waves: list[tuple[str, str]] = []

        class Commands:
            _selected_wave = {"A": SILENT, "B": SILENT}

            async def set_strength(self, ch, v, slot_id=None):
                pass

            async def set_wave(self, ch, name, slot_id=None):
                waves.append((ch, name))
                self._selected_wave[ch] = name

            def wave_selection(self):
                return dict(self._selected_wave)

            async def fire_start(self, slot_id=None, channel=None):
                pass

            async def fire_stop(self, slot_id=None, channel=None):
                pass

            async def emergency_stop(self):
                pass

        port = free_udp_port()
        cfg = OscConfig({"in_port": port})
        cfg["mappings"] = default_input_rows(cfg)
        bridge = OscBridge(cfg, lambda: state, Commands())
        await bridge.start()
        try:
            from pythonosc.udp_client import SimpleUDPClient

            sender = SimpleUDPClient("127.0.0.1", port)
            sender.send_message("/avatar/parameters/DGLabWaveA", [1])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabWaveStepA", [1])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabWaveStepA", [-1])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabWaveStepA", [0])
            await asyncio.sleep(0.3)
        finally:
            await bridge.stop()

        assert waves[0] == ("A", CONTINUOUS), waves
        assert waves[1] == ("A", order[2]), waves
        assert waves[2] == ("A", CONTINUOUS), waves
        assert len(waves) == 3, waves

    async def test_fire_parameter_is_trigger(self):
        state = _state(("coyote-1", "COYOTE_030"))

        events: list[str] = []

        class Commands:
            _selected_wave = {"A": SILENT, "B": SILENT}

            async def set_strength(self, ch, v, slot_id=None):
                pass

            async def set_wave(self, ch, name, slot_id=None):
                pass

            async def fire_start(self, slot_id=None, channel=None):
                events.append(f"start:{slot_id}:{channel}")

            async def fire_stop(self, slot_id=None, channel=None):
                events.append(f"stop:{slot_id}:{channel}")

            async def emergency_stop(self):
                pass

        port = free_udp_port()
        cfg = OscConfig({"in_port": port})
        cfg["mappings"] = default_input_rows(cfg)
        bridge = OscBridge(cfg, lambda: state, Commands())
        await bridge.start()
        try:
            from pythonosc.udp_client import SimpleUDPClient

            sender = SimpleUDPClient("127.0.0.1", port)
            sender.send_message("/avatar/parameters/DGLabFire", [True])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabFire", [False])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabFireA", [True])
            await asyncio.sleep(0.3)
            sender.send_message("/avatar/parameters/DGLabFireA", [False])
            await asyncio.sleep(0.3)
        finally:
            await bridge.stop()

        assert events == ["start:coyote-1:None", "stop:coyote-1:None",
                          "start:coyote-1:A", "stop:coyote-1:A"], events


class OscProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rx_probe_tracks_packets(self):
        state = _state(("coyote-1", "COYOTE_030"))

        class Commands:
            async def set_strength(self, ch, v, slot_id=None):
                pass

        port = free_udp_port()
        bridge = OscBridge(OscConfig({"in_port": port}), lambda: state, Commands())
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


class OscMappingRuntimeTests(unittest.TestCase):

    def test_dynamic_input_param_drives_core_dispatch(self):
        from modules.osc_bridge.bridge import OscBridge, OscConfig
        from modules.osc_bridge.plugin import OSC_CONFIG_DEFAULTS

        cfg = OscConfig(
            {"mappings": [{"param": "in_strength_a", "expr": "{blood}"}]},
            defaults=OSC_CONFIG_DEFAULTS)
        bridge = OscBridge(cfg, lambda: None, None)
        try:
            bridge._track_input("/avatar/parameters/blood", 120)
            self.assertIn("blood", bridge.param_names())
            self.assertEqual(bridge.engine.last_values["in_strength_a"], 120)
        finally:
            bridge.close()

    def test_output_rows_rename_wins(self):
        from dglab.state import EngineState, Slot
        from modules.osc_bridge.bridge import default_output_rows

        state = EngineState(connected=True, paired=True, slots={
            "1": Slot(slot_id="1", name="Coyote", type="COYOTE",
                      strength={"A": 80, "B": 0}, battery=66)})
        cfg = {"prefix": "DGLab", "device_prefixes": {"COYOTE": "DGLab"},
               "output_map": {"COYOTE.StrengthA": "myStrength",
                              "Action": "Btn"}}
        rows = default_output_rows(cfg, state)
        by = {row["param"]: row for row in rows}
        self.assertEqual(by["COYOTE.StrengthA"]["name"], "myStrength")
        self.assertEqual(by["COYOTE.StrengthB"]["name"], "DGLabStrengthB")
        self.assertEqual(by["COYOTE.Battery"]["name"], "DGLabBattery")
        self.assertEqual(by["Action"]["name"], "Btn")

    def test_expression_mixes_device_vars(self):
        from dglab.state import EngineState, Slot
        from modules.osc_bridge.bridge import OscBridge, OscConfig
        from modules.osc_bridge.plugin import OSC_CONFIG_DEFAULTS

        state = EngineState(connected=True, paired=True, slots={
            "1": Slot(slot_id="1", type="COYOTE",
                      strength={"A": 300, "B": 0},
                      strength_limit={"A": 200, "B": 200})})
        cfg = OscConfig(
            {"mappings": [{"param": "in_strength_a",
                           "expr": "{Strength-max}*({HP}+{Hurt}/{HPmax})"}]},
            defaults=OSC_CONFIG_DEFAULTS)
        bridge = OscBridge(cfg, lambda: state, None)
        try:
            bridge.engine.signal("HP", 60)
            bridge.engine.signal("Hurt", 30)
            bridge.engine.signal("HPmax", 100)
            self.assertEqual(bridge.engine.last_values["in_strength_a"], 200)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
