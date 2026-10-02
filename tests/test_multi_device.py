from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

import asyncio
import unittest

from dglab.socket_v4 import SocketV4Client
from dglab.state import EngineState, Slot, StateEvents, family_of
from modules.osc_bridge.bridge import OscBridge, OscConfig, device_osc_names


def _state_with(slots: dict[str, tuple[str, int, int]]) -> EngineState:
    state = EngineState(backend="v4", paired=True, connected=True)
    for sid, (dtype, a, b) in slots.items():
        slot = Slot(slot_id=sid, name=sid, type=dtype)
        slot.strength = {"A": a, "B": b}
        state.slots[sid] = slot
    return state


class FamilyTests(unittest.TestCase):
    def test_family_of(self):
        self.assertEqual(family_of("COYOTE_030"), "COYOTE")
        self.assertEqual(family_of("COYOTE_020"), "COYOTE")
        self.assertEqual(family_of("OVC_1"), "OVC")
        self.assertEqual(family_of("BMTR_1"), "BMTR")
        self.assertEqual(family_of(""), "COYOTE")


class OscNamingTests(unittest.TestCase):
    def test_device_osc_names(self):
        state = _state_with({
            "slot-b": ("COYOTE_030", 1, 2),
            "slot-a": ("COYOTE_030", 3, 4),
            "ovc-1": ("OVC_1", 5, 6),
            "bmtr-1": ("BMTR_1", 0, 0),
        })
        names = device_osc_names(state, {"COYOTE": "DGLab", "OVC": "DGLabOvc", "BMTR": "DGLabBmtr"})
        self.assertEqual(names["slot-a"]["name"], "DGLab")
        self.assertEqual(names["slot-b"]["name"], "DGLab2")
        self.assertEqual(names["ovc-1"]["name"], "DGLabOvc")
        self.assertEqual(names["bmtr-1"]["name"], "DGLabBmtr")
        self.assertEqual(names["ovc-1"]["family"], "OVC")

    def test_push_state_per_device(self):
        state = _state_with({
            "slot-a": ("COYOTE_030", 11, 7),
            "slot-b": ("COYOTE_030", 20, 30),
            "bmtr-1": ("BMTR_1", 0, 0),
        })
        state.slots["bmtr-1"].pressure = 7.9
        state.slots["bmtr-1"].edge_state = 2

        # 空映射表 → 按设备前缀自动生成默认输出行，逐设备落地参数名
        bridge = OscBridge(OscConfig({"rate_hz": 100}), lambda: state, None)
        sent: dict[str, object] = {}
        bridge._send_param = lambda name, value: sent.__setitem__(name, value)
        bridge.apply_config()
        bridge._push_values()

        self.assertEqual(sent.get("DGLabStrengthA"), 11)
        self.assertEqual(sent.get("DGLab2StrengthA"), 20)
        self.assertEqual(sent.get("DGLab2StrengthB"), 30)
        self.assertEqual(sent.get("DGLabBmtrPressure"), 7.9)
        self.assertEqual(sent.get("DGLabBmtrEdgeState"), 2)
        self.assertNotIn("DGLabPressure", sent)

    def test_input_target_prefers_coyote(self):
        state = _state_with({
            "ovc-1": ("OVC_1", 0, 0),
            "slot-a": ("COYOTE_030", 0, 0),
        })
        bridge = OscBridge(OscConfig(), lambda: state, None)
        self.assertEqual(bridge.input_target_slot(), "slot-a")


class V4SlotRoutingTests(unittest.TestCase):
    def test_require_peer_explicit_slot(self):
        client = SocketV4Client(events=StateEvents())
        client._handle_frame({"type": "hello", "clientId": "ctrl"})
        client._handle_frame({"type": "client_attached", "clientId": "app"})
        client._replace_devices("app", [
            {"slotId": "s1", "type": "COYOTE_030"},
            {"slotId": "s2", "type": "OVC_1"},
        ])
        cid, sid = client._require_peer("s2")
        self.assertEqual((cid, sid), ("app", "s2"))
        cid, sid = client._require_peer(None)
        self.assertEqual(sid, "s1")

    def test_add_intensity_targets_slot(self):
        client = SocketV4Client(events=StateEvents())
        client._handle_frame({"type": "hello", "clientId": "ctrl"})
        client._handle_frame({"type": "client_attached", "clientId": "app"})
        client._replace_devices("app", [
            {"slotId": "s1", "type": "COYOTE_030"},
            {"slotId": "s2", "type": "OVC_1"},
        ])
        sent: list[dict] = []

        async def fake_send(frame):
            sent.append(frame)

        client._send_raw = fake_send

        async def main():
            await client.add_intensity("A", 5, slot_id="s2")

        asyncio.run(main())
        payload = sent[0]["data"]
        self.assertEqual(payload["m"], "device.op")
        self.assertEqual(payload["data"]["s"], "s2")
        self.assertEqual(payload["data"]["t"], 3)
        self.assertEqual(payload["data"]["v"], 5)

    def test_emergency_stop_hits_all_devices(self):
        client = SocketV4Client(events=StateEvents())
        client._handle_frame({"type": "hello", "clientId": "ctrl"})
        client._handle_frame({"type": "client_attached", "clientId": "app"})
        client._replace_devices("app", [
            {"slotId": "s1", "type": "COYOTE_030"},
            {"slotId": "s2", "type": "OVC_1"},
        ])
        sent: list[dict] = []

        async def fake_send(frame):
            sent.append(frame)

        client._send_raw = fake_send

        async def main():
            await client.emergency_stop()

        asyncio.run(main())
        ops = [f["data"]["data"] for f in sent if f["data"].get("m") == "device.op"]
        resets = [o for o in ops if o.get("t") == 7]
        self.assertEqual({o["s"] for o in resets}, {"s1", "s2"})
        self.assertEqual(len(resets), 4)
        clears = [f["data"] for f in sent if f["data"].get("m") == "device.op.clear"]
        self.assertEqual(len(clears), 1)
        self.assertNotIn("data", clears[0])


if __name__ == "__main__":
    unittest.main()
