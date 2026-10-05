"""VRChat OSC 桥接：动态参数表 + 核心参数双向映射。

头像侧参数是**动态定义**的：任何收到的 ``/avatar/parameters/<名称>`` 都以
``<名称>`` 建立同名参数进入模块参数表，并作为信号参与运算 —— 模块不需要
预声明头像参数名。联动关系全部落在两张映射表上（配置文件也只保存它们）：

* ``mappings`` 行 ``{param: 核心输入参数 id, expr: 表达式}``：表达式引用头像
  参数名（``{DGLabStrengthA}``）、其他模块量与核心输出参数，结果取整钳制后
  派发到设备动作；表达式留空即同名直传 ``{in_strength_a}``；
* ``outputs`` 行 ``{param: 核心输出参数 id, name: 头像参数名, expr: 表达式}``：
  求值后写回 ``/avatar/parameters/<name>``，参数名由用户重命名。

未配置映射表时按「设备前缀 + 信号名」的默认命名自动建表，行为与旧版一致。
"""

from __future__ import annotations

import asyncio
import copy
import socket
import threading
import time
from typing import Any

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.udp_client import SimpleUDPClient

from dglab.mapping import MappingEngine, signal_specs
from dglab.naming import (INPUT_NAME_TEMPLATES, default_input_name,
                          default_output_name, device_osc_names)
from dglab.params import (build_dispatchers, core_alias_values, core_inputs,
                          device_state_values, input_ranges, output_key,
                          output_spec, output_specs)
from dglab.state import EngineState, family_of
from dglab.waves import wave_order

__all__ = ["OscBridge", "OscConfig", "device_osc_names", "wave_order",
           "output_map_key", "signal_specs", "OUT_SIGNALS",
           "default_input_rows", "default_output_rows", "effective_rows",
           "default_input_name", "default_output_name"]


class OscConfig(dict):
    """OSC 模块配置：缺省值优先取模块声明（defaults 参数），DEFAULTS 为兜底。"""

    DEFAULTS = {
        "out_ip": "127.0.0.1",
        "out_port": 9000,
        "in_port": 9001,
        "prefix": "DGLab",
        "rate_hz": 10,
        "device_prefixes": {
            "COYOTE": "DGLab",
            "OVC": "DGLabOvc",
            "BMTR": "DGLabBmtr",
        },
        "mappings": [],
        "outputs": [],
    }

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__(copy.deepcopy(defaults or self.DEFAULTS))
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


class OscBridge:
    def __init__(self, config: OscConfig, get_state, commands, events=None,
                 on_auto_rows=None):
        self.config = config
        self.get_state = get_state
        self.commands = commands
        # 设备集变化回调：fn(默认输入行, 默认输出行)，模块据此把新设备
        # 参数落地进配置表（联动页即见），见 plugin._persist_auto_rows
        self._on_auto_rows = on_auto_rows

        self._client = SimpleUDPClient(config["out_ip"], int(config["out_port"]))
        self._dispatcher = Dispatcher()
        self._server = None
        self._server_thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._out_ip: str = str(config["out_ip"])
        self._action_value = 0
        self._action_until = 0.0
        self._last_sent: dict[str, Any] = {}
        self.input_values: dict[str, dict] = {}
        self.last_rx: float | None = None
        self.rx_count = 0

        self._events = events
        if events is not None:
            events.on("action", self._on_event_action)
        # 映射引擎：信号空间 = 头像参数（动态）∪ 核心输出参数实时值
        self.engine = MappingEngine(self._dispatch,
                                    device_vars=self._device_vars,
                                    ranges=input_ranges())
        self._api = self._DeviceApi(self)
        self.dispatchers = build_dispatchers(self._api, core_inputs())
        self._auto_sig: tuple | None = None
        self._primed = False
        self._dispatcher.set_default_handler(self._track_input)
        self.apply_config()

    def close(self) -> None:
        """解除事件总线订阅（模块卸载时调用，避免残留处理器）。"""
        if self._events is not None:
            self._events.off("action", self._on_event_action)
            self._events = None

    # ---- 映射表 ---------------------------------------------------------
    def apply_config(self) -> None:
        """装载两张映射表；首轮只静默求值，避免启动即把设备写成 0。"""
        rows_in, rows_out = effective_rows(self.config, self._safe_state())
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(rows_in)
        self.engine.set_outputs(rows_out)
        if first:
            self.engine.armed = True
            self._primed = True

    def _safe_state(self):
        try:
            return self.get_state()
        except Exception:
            return None

    def _on_event_action(self, action: int | None) -> None:
        if action is None:
            return
        self._action_value = int(action)
        self._action_until = time.monotonic() + 0.3

    def send_value(self, address: str, value) -> None:
        try:
            self._client.send_message(address, value)
        except Exception as exc:
            log = getattr(self, "log", None)
            if log is not None:
                log(f"OSC 发送 {address} 失败: {exc!r}")

    def _track_input(self, addr: str, *args) -> None:
        """收到的头像参数按同名建立参数表并送入信号空间。

        pythonosc 的默认处理器以 ``callback(address, *args)`` 调用，故这里用
        变长参数接收实际的参数值（数组消息时 ``args[0]`` 即为该数组）。
        """
        self.last_rx = time.monotonic()
        self.rx_count += 1
        if str(addr) == "/avatar/change":
            self._last_sent.clear()
            self.engine.reset()
            return
        if not args:
            return
        name = str(addr).rstrip("/").rsplit("/", 1)[-1]
        self.input_values[name] = {"value": args[0], "ts": time.monotonic()}
        self.engine.signal(name, args[0])

    def param_names(self, max_age_s: float = 120.0) -> list[str]:
        """动态参数表内的参数名（联动页表达式变量池）。"""
        now = time.monotonic()
        names = [name for name, rec in self.input_values.items()
                 if now - rec.get("ts", now) <= max_age_s]
        return sorted(names)

    def recent_inputs(self, max_age_s: float = 30.0) -> list[dict]:
        """最近收到的输入参数值（供界面「输入数据值」展示）。"""
        now = time.monotonic()
        rows = []
        for name, rec in self.input_values.items():
            age = now - rec.get("ts", now)
            if age > max_age_s:
                continue
            rows.append({"param": name, "value": rec.get("value"), "age": age})
        return sorted(rows, key=lambda r: r["param"])

    # ---- 派发 -----------------------------------------------------------
    def _dispatch(self, target: str, value: int) -> None:
        runner = self.dispatchers.get(target)
        if runner is None:
            return
        try:
            runner(value)
        except Exception as exc:
            self.log(f"[OSC] 映射派发 {target}={value} 失败: {exc!r}")

    class _DeviceApi:
        """把引擎命令层适配成核心参数派发器需要的接口。"""

        def __init__(self, bridge: "OscBridge"):
            self._b = bridge

        @property
        def _cmd(self):
            return self._b.commands

        def resolve_slot(self, family: str = "") -> str | None:
            return self._b.input_target_slot(family or "COYOTE")

        def wave_order(self, family: str = "") -> list[str]:
            return wave_order(family or "COYOTE")

        def wave_selection(self) -> dict:
            getter = getattr(self._cmd, "wave_selection", None)
            return (getter() or {}) if getter is not None else {}

        def set_strength(self, channel, value, slot_id=None):
            return self._cmd.set_strength(channel, value, slot_id=slot_id)

        def set_wave(self, channel, name, slot_id=None):
            return self._cmd.set_wave(channel, name, slot_id=slot_id)

        def zap(self, channel, seconds=1.0, slot_id=None):
            return self._cmd.zap(channel, seconds, slot_id=slot_id)

        def fire_start(self, slot_id=None, channel=None):
            return self._cmd.fire_start(slot_id=slot_id, channel=channel)

        def fire_stop(self, slot_id=None, channel=None):
            return self._cmd.fire_stop(slot_id=slot_id, channel=channel)

        def emergency_stop(self):
            return self._cmd.emergency_stop()

        def run(self, coro) -> None:
            self._b._spawn(coro)

    def _device_vars(self) -> dict[str, float]:
        """表达式可用的核心输出参数实时值 + 短名别名 + Action。"""
        vals = device_state_values(self._safe_state())
        vals.update(core_alias_values(vals))
        vals["Action"] = float(self._action_value
                               if time.monotonic() < self._action_until else 0)
        return vals

    def _spawn(self, coro) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    @staticmethod
    def _local_ip() -> str:
        """本机出口网卡 IP (UDP connect 不发包, 仅查路由)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return "127.0.0.1"

    async def start(self) -> None:
        if self._running:
            return
        from pythonosc.osc_server import ThreadingOSCUDPServer

        self._running = True
        self._loop = asyncio.get_running_loop()

        out_ip = str(self.config["out_ip"])
        if out_ip in ("127.0.0.1", "localhost", "::1"):
            resolved = self._local_ip()
            if resolved and resolved != out_ip:
                self.log(f"[OSC] 发送目标由 {out_ip} 改为本机网卡地址 {resolved}"
                         f" (部分加速器/驱动会拦截回环 UDP, 127.0.0.1 收不到)")
                out_ip = resolved
        self._client = SimpleUDPClient(out_ip, int(self.config["out_port"]))

        self._server = ThreadingOSCUDPServer(
            ("0.0.0.0", int(self.config["in_port"])), self._dispatcher
        )
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        self.apply_config()
        self._task = asyncio.create_task(self._push_loop())
        self.log("[OSC] 桥接已启动: 输出 -> "
                 f"{out_ip}:{self.config['out_port']}, "
                 f"监听 0.0.0.0:{self.config['in_port']}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.log("[OSC] 桥接已停止")

    log = print

    async def _push_loop(self) -> None:
        interval = 1.0 / max(1, int(self.config["rate_hz"]))
        tick = 0
        try:
            while self._running:
                await asyncio.sleep(interval)
                state = self._safe_state()
                self._refresh_rows(state)   # 接入设备变化时补默认输出行
                self.engine.pump()
                self._push_values()
                tick += 1
                if tick % 10 == 0:
                    self._send_keepalive()
        except asyncio.CancelledError:
            pass

    def _refresh_rows(self, state) -> None:
        sig = _device_sig(state, self.config.get("device_prefixes") or {})
        if sig == self._auto_sig:
            return
        self._auto_sig = sig
        self.apply_config()
        self._notify_device_rows(state)

    def _notify_device_rows(self, state) -> None:
        """设备集变化 → 把当前设备的默认参数行交给模块落地配置。"""
        callback = self._on_auto_rows
        if callback is None or state is None:
            return
        try:
            callback(default_input_rows(self.config, state),
                     default_output_rows(self.config, state))
        except Exception as exc:
            self.log(f"[OSC] 设备参数自动落地失败: {exc!r}")

    def _send_keepalive(self) -> None:
        """从接收端口向 VRChat 发注册包, 使其把回传 OSC 发往本机网卡地址.

        VRChat 只向"最近发来 OSC 的地址"回传数据; 若回环 UDP 被加速器等拦截,
        用 127.0.0.1 学到的地址会导致反向链路同样失效, 故从 0.0.0.0:in_port
        的套接字发往网卡地址, 让 VRChat 学到 (网卡IP, in_port)。
        """
        server = self._server
        if server is None:
            return
        try:
            builder = OscMessageBuilder("/dglab/keepalive")
            builder.add_arg(1)
            server.socket.sendto(builder.build().dgram,
                                 (self._out_ip, int(self.config["out_port"])))
        except Exception:
            pass

    def _push_values(self) -> None:
        for name, value in self.engine.out_values.items():
            if self._last_sent.get(name) != value:
                self._send_param(name, value)
                self._last_sent[name] = value

    def _send_param(self, name: str, value) -> None:
        """把模块侧参数名写回头像参数（pythonosc 按 Python 值类型推断 OSC 类型）。"""
        self.send_value(f"/avatar/parameters/{name}", value)

    def _device_names(self, state: EngineState) -> dict[str, dict[str, str]]:
        return device_osc_names(state, self.config["device_prefixes"])

    def input_target_slot(self, family: str = "COYOTE") -> str | None:
        state = self._safe_state()
        if state is None:
            return None
        slots = {sid: state.slots[sid] for sid in sorted(state.slots)}
        for sid, slot in slots.items():
            if family_of(slot.type) == family:
                return sid
        for sid, slot in slots.items():
            if family == "BMTR" or family_of(slot.type) != "BMTR":
                return sid
        return None


def _device_sig(state, prefixes: dict) -> tuple:
    if state is None:
        return ()
    try:
        names = device_osc_names(state, prefixes)
    except Exception:
        return ()
    return tuple((sid, info["family"], info["index"], info["name"])
                 for sid, info in sorted(names.items()))


def default_input_rows(config: dict, state=None) -> list[dict]:
    """按设备前缀生成默认「直传」映射行（与旧版内置参数名一致）。

    给出 ``state`` 时只为已接入设备家族生成（全局急停始终保留），供设备
    连接后自动暴露参数；``state`` 为 None 时保持旧行为生成全部家族
    （引擎空表兜底口径）。
    """
    prefixes = dict(config.get("device_prefixes") or {})
    global_prefix = str(config.get("prefix") or "DGLab")
    if state is None:
        families = None
    else:
        try:
            names = device_osc_names(state, prefixes)
        except Exception:
            names = {}
        families = {info["family"] for info in names.values()}
    rows: list[dict] = []
    for spec in core_inputs():
        if families is not None and spec["family"] \
                and spec["family"] not in families:
            continue
        if spec["action"] == "emergency":
            name = f"{global_prefix}Emergency"
        else:
            prefix = str(prefixes.get(spec["family"]) or
                         f"DGLab{spec['family'].capitalize()}")
            templates = INPUT_NAME_TEMPLATES.get(spec["family"], ())
            index = {"strength": 0, "wave": 1, "wave_step": 2, "zap": 3,
                     "fire": 4}.get(spec["action"])
            if index is None or index >= len(templates):
                continue
            name = templates[index].format(prefix=prefix, ch=spec["channel"])
        rows.append({"param": spec["key"], "expr": "{" + name + "}"})
    return rows


def default_output_rows(config: dict, state) -> list[dict]:
    """按设备前缀生成默认输出行（头像参数名 = 前缀 + 信号名）。

    旧版 ``output_map``（信号键 → 参数名）里的重命名优先，供启动时落地使用。
    """
    read = config.get("output_map") or {}
    names = device_osc_names(state, config.get("device_prefixes") or {}) \
        if state is not None else {}
    rows: list[dict] = []
    for sid in sorted(names):
        info = names[sid]
        index = int(info.get("index", 1))
        for spec in output_specs(info["family"], index):
            key = spec["key"]
            custom = str(read.get(key) or "").strip()
            rows.append({"param": key,
                         "name": custom or f"{info['name']}{spec['signal']}",
                         "expr": "{" + key + "}",
                         "type": spec["type"]})
    prefix = str(config.get("prefix") or "DGLab")
    rows.append({"param": "Action",
                 "name": str(read.get("Action") or "").strip()
                 or f"{prefix}Action",
                 "expr": "{Action}", "type": "Int"})
    return rows


# default_input_name / default_output_name / device_osc_names 由核心
# dglab.naming 提供并经上方 import 再导出（__all__ 兼容旧引用路径）。


def effective_rows(config: dict, state) -> tuple[list, list]:
    """配置 → 引擎实际使用的两张表。

    输入表：有任一行即完全由配置决定；整表为空时按默认参数名自动生成直传行。
    输出表：配置行优先，接入设备上未被覆盖的信号补默认行（新设备接入即有输出）。
    """
    rows_in = _valid(config.get("mappings") or [])
    if not rows_in:
        rows_in = default_input_rows(config)
    rows_out = _valid(config.get("outputs") or [])
    covered = {str(row.get("param") or "") for row in rows_out}
    for row in default_output_rows(config, state):
        if row["param"] not in covered:
            rows_out.append(row)
    return rows_in, rows_out


def _valid(rows: list) -> list[dict]:
    return [row for row in (rows or []) if isinstance(row, dict)
            and str(row.get("param") or "").strip()]


# 兼容旧引用：输出信号目录现由核心参数目录提供
from dglab.params import OUTPUT_SIGNALS as OUT_SIGNALS    # noqa: E402


def output_map_key(family: str, index: int, signal: str) -> str:
    """输出参数 id：家族 1 号设备用 ``FAMILY.Signal``，其余带设备序号。"""
    return output_key(family, index, signal)
