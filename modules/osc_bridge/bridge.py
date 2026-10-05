"""VRChat OSC 桥接：动态参数表 + 路径命名临时变量回传。

头像侧参数是**动态定义**的：任何收到的 ``/avatar/parameters/<名称>`` 都以
``<名称>`` 建立同名参数进入模块参数表 —— 模块不需要预声明头像参数名。

回传通道：设备可读参数由模块建为**完整路径命名的临时变量**（变量名 =
OSC 地址，如 ``avatar/parameters/DGLabStrengthA``，表达式取核心输出
信号），推送循环把所有含 ``/`` 的临时变量按变量名回传（值变化才发、
按来源参数类型归一）；用户重命名变量即改回传地址。事件流与映射表
（``mappings`` / ``outputs``）仅保留引擎装载接口兼容用户自建/旧配置，
模块不再自动生成。
"""

from __future__ import annotations

import asyncio
import copy
import re
import socket
import threading
import time
from typing import Any

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.udp_client import SimpleUDPClient

from dglab import expr
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
           "default_input_name", "default_output_name", "TEMP_PATH_PREFIX"]

# 回传临时变量的命名前缀：变量名 = OSC 地址（去开头 /），桥接按名回传
TEMP_PATH_PREFIX = "avatar/parameters/"


def _typed_value(kind: str, raw):
    """按声明类型归一输出值：Int 取整、Bool 正值归真、Float 保留三位，
    其余原样。"""
    k = str(kind or "").upper()
    try:
        if k == "INT":
            return int(round(float(raw)))
        if k == "BOOL":
            return bool(float(raw) > 1e-9)
        if k == "FLOAT":
            return round(float(raw), 3)
    except (TypeError, ValueError):
        pass
    return raw


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
                 on_devices_changed=None, set_temp=None):
        self.config = config
        self.get_state = get_state
        self.commands = commands
        # 设备集变化回调：fn(state)，模块据此刷新维护参数声明（见 plugin）
        self._on_devices_changed = on_devices_changed
        # 共享临时变量写入回调：fn(变量名, 数值)，模块维护的回传参数写入
        # 宿主共享空间（新版核心面板可见；发行版无此能力时跳过）
        self._set_temp = set_temp
        self._on_devices_changed = on_devices_changed

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
        self._temp_rows: list[dict] = []   # 路径命名临时变量（模块自算回传）
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
        """装载两张映射表与路径型临时变量；首轮只静默求值，避免启动即把设备写成 0。"""
        rows_in, rows_out = effective_rows(self.config, self._safe_state())
        self._temp_rows = self._path_temp_rows()
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(rows_in)
        self.engine.set_outputs(rows_out)
        if first:
            self.engine.armed = True
            self._primed = True

    def _path_temp_rows(self) -> list[dict]:
        """配置临时变量中的路径型行（变量名含 ``/``，即 OSC 回传地址）。

        模块自持自算，不依赖宿主把临时变量装载进引擎（发行版核心无该
        能力）；非路径行留给宿主/映射引擎。表达式经 normalize 校验，
        非法行跳过。
        """
        rows: list[dict] = []
        seen: set[str] = set()
        for row in (self.config.get("temps") or []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if "/" not in name or name in seen:
                continue
            try:
                text = expr.normalize(row.get("expr") or "")
            except expr.ExprError:
                continue
            if not text:
                continue
            seen.add(name)
            rows.append({"name": name, "expr": text})
        return rows

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
                self._check_devices(state)  # 接入设备变化时通知模块刷新
                self.engine.pump()
                self._push_maintained(state)
                self._push_values()
                tick += 1
                if tick % 10 == 0:
                    self._send_keepalive()
        except asyncio.CancelledError:
            pass

    def _check_devices(self, state) -> None:
        sig = _device_sig(state, self.config.get("device_prefixes") or {})
        if sig == self._auto_sig:
            return
        self._auto_sig = sig
        self._notify_devices_changed(state)

    def _notify_devices_changed(self, state) -> None:
        """设备集变化 → 交给模块以事件流/临时变量接线新设备参数。"""
        callback = self._on_devices_changed
        if callback is None or state is None:
            return
        try:
            callback(state)
        except Exception as exc:
            self.log(f"[OSC] 设备参数自动接线失败: {exc!r}")

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

    def _push_maintained(self, state) -> None:
        """模块自动维护的回传参数：设备可读参数 → 路径命名临时变量。

        变量名 = ``avatar/parameters/<默认参数名>``，值取核心输出参数实时
        值并按声明类型归一；写入共享临时变量空间（新版核心联动页可见，
        发行版无此能力时跳过），并按变量名回传 OSC（值变化才发送）。
        """
        if state is None:
            return
        try:
            names = device_osc_names(state,
                                     self.config.get("device_prefixes") or {})
            vals = device_state_values(state)
        except Exception:
            return
        for sid in sorted(names):
            info = names[sid]
            for spec in output_specs(info["family"],
                                     int(info.get("index", 1))):
                if spec["key"] in vals:
                    self._maintain_temp(f"{info['name']}{spec['signal']}",
                                        spec["type"], vals[spec["key"]])
        self._maintain_temp(f"{self.config.get('prefix') or 'DGLab'}Action",
                            "Int",
                            float(self._action_value
                                  if time.monotonic() < self._action_until
                                  else 0))

    def _maintain_temp(self, avatar_name: str, kind: str, raw) -> None:
        name = f"{TEMP_PATH_PREFIX}{str(avatar_name or '').lstrip('/')}"
        value = _typed_value(kind, raw)
        writer = self._set_temp
        if writer is not None:
            try:
                writer(name, value)
            except Exception as exc:
                self.log(f"[OSC] 临时变量写入 {name} 失败: {exc!r}")
        if self._last_sent.get(name) != value:
            self.send_value(f"/{name.lstrip('/')}", value)
            self._last_sent[name] = value

    def _push_values(self) -> None:
        for name, value in self.engine.out_values.items():
            if self._last_sent.get(name) != value:
                self._send_param(name, value)
                self._last_sent[name] = value
        self._push_temps()

    def _push_temps(self) -> None:
        """路径命名临时变量 → 按变量名回传 OSC（模块自算，发行版核心可用）。

        值空间 = 引擎值空间（头像参数信号 ∪ 核心输出参数实时值 ∪ 引擎
        临时变量）；对配置里路径型行用表达式求值，类型按裸引用的核心
        参数归一，值变化才发送。新核心宿主装载进引擎 temps 的路径变量
        （联动页追加行）一并回传（模块自持行优先，同值去重）。
        """
        for row in self._temp_rows:
            name = row["name"]
            try:
                raw = expr.evaluate(row["expr"], self.engine.values())
            except expr.ExprError:
                continue          # 变量未就绪（如设备未上报），下一拍再试
            except Exception:
                continue
            value = self._typed_temp(row, raw)
            if self._last_sent.get(name) != value:
                self.send_value(f"/{name.lstrip('/')}", value)
                self._last_sent[name] = value
        for key, value in list(self.engine.temps.items()):
            key = str(key)
            if "/" not in key or any(r["name"] == key for r in self._temp_rows):
                continue
            if self._last_sent.get(key) != value:
                self.send_value(f"/{key.lstrip('/')}", value)
                self._last_sent[key] = value

    _BARE_EXPR = re.compile(r"^\{([^{}]+)\}$")

    def _typed_temp(self, row: dict, value):
        """临时变量值按来源参数类型归一：表达式为裸 ``{核心输出参数}`` 时
        取其声明类型（Int/Bool/Float），其余（自定义表达式）原样发送。"""
        m = self._BARE_EXPR.match(str(row.get("expr") or ""))
        kind = ""
        if m:
            spec = output_spec(m.group(1).strip())
            if spec is not None:
                kind = str(spec.get("type") or "")
        kind = kind.upper()
        try:
            if kind == "INT":
                return int(round(float(value)))
            if kind == "BOOL":
                return bool(float(value) > 1e-9)
            if kind == "FLOAT":
                return round(float(value), 3)
        except (TypeError, ValueError):
            pass
        return value

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


def effective_rows(config: dict, state=None) -> tuple[list, list]:
    """配置 → 引擎实际使用的两张表：**仅显式配置行**。

    旧版在此处按设备自动补默认行（输入直传/输出回传的兜底），该机制已由
    模块的事件流 + 临时变量自动接线取代（设备接入即建卡片与变量，
    联动页可编辑），引擎不再隐式建行，避免旧链路在新模型下重复派发。
    """
    return _valid(config.get("mappings") or []), _valid(config.get("outputs") or [])


def _valid(rows: list) -> list[dict]:
    return [row for row in (rows or []) if isinstance(row, dict)
            and str(row.get("param") or "").strip()]


# 兼容旧引用：输出信号目录现由核心参数目录提供
from dglab.params import OUTPUT_SIGNALS as OUT_SIGNALS    # noqa: E402


def output_map_key(family: str, index: int, signal: str) -> str:
    """输出参数 id：家族 1 号设备用 ``FAMILY.Signal``，其余带设备序号。"""
    return output_key(family, index, signal)
