"""OSC 收发桥：把设备读数登记成变量，把宿主写入的变量按同名地址回传头像。

本模块是纯输入设备：收进来的值镜像进共享变量表，事件流的写入卡片把数值写进
同名变量，桥再按变量名当 OSC 地址发出去。表达式求值与设备派发已经退役（那套
`MappingEngine` 直接握着核心的 `set_strength` / `fire`，绕过「模块不得直写设备」
的拦截），要换算或驱动设备请在事件流里连线。
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

from dglab.mapping import as_number
from dglab.naming import default_input_name, device_osc_names
from dglab.params import core_inputs, device_state_values, output_specs
from dglab.waves import wave_order

__all__ = ["OscBridge", "OscConfig", "OscValueSpace", "device_osc_names",
           "wave_order", "default_input_name", "TEMP_PATH_PREFIX",
           "param_name_override"]

TEMP_PATH_PREFIX = "avatar/parameters/"


def param_name_override(config: dict, key: str) -> str:
    """变量表里改过名的参数：param_names = {参数键: 头像参数名}。"""
    table = config.get("param_names") or {}
    if not isinstance(table, dict):
        return ""
    return str(table.get(str(key)) or "").strip()


def _typed_value(kind: str, raw):
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
    }

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__(copy.deepcopy(defaults or self.DEFAULTS))
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


class OscValueSpace:
    """核心的共享值空间在桥这边的挂接点（宿主按 `bridge.engine` 找它）。

    - `signals`：收到的参数值，键 = 去斜杠的收包地址（avatar 参数即
      `avatar/parameters/<名>`），与登记行同名。事件流的读数卡与变量表实时值
      都从这里取（`flow_host.module_signals`），所以收包必须写它，不能只留
      时间戳。
    - `temps`：核心的共享变量表，由宿主 `attach_temps` 接进来；事件流的写入卡片
      往这里写，桥按变量名当 OSC 地址发出去。
    - `pump()`：宿主写完一个值后调它。这里不再求值也不派发，发什么由 `_push_loop`
      每拍读，所以它只是让下一拍尽早跟上。
    """

    def __init__(self):
        self.signals: dict[str, float] = {}
        self.temps: dict[str, Any] = {}

    def attach_temps(self, shared: dict) -> None:
        if shared is self.temps:
            return
        self.temps = shared

    def reset(self) -> None:
        self.signals.clear()

    def pump(self) -> None:
        return None


def _device_sig(state, prefixes: dict) -> tuple:
    """设备指纹：只有连上的设备集合变了才重新登记参数。"""
    if state is None:
        return ()
    try:
        names = device_osc_names(state, prefixes)
    except Exception:
        return ()
    return tuple((sid, info["family"], info["index"], info["name"])
                 for sid, info in sorted(names.items()))


class OscBridge:

    def __init__(self, config: OscConfig, get_state, *, events=None,
                 on_devices_changed=None, set_temp=None):
        self.config = config
        self.get_state = get_state
        self._on_devices_changed = on_devices_changed
        self._set_temp = set_temp

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
        self._maintained_keys: set[str] = set()
        self._no_send: set[str] = set()
        self._input_names: set[str] = set()
        self.input_values: dict[str, dict] = {}
        self.last_rx: float | None = None
        self.rx_count = 0

        self._events = events
        if events is not None:
            events.on("action", self._on_event_action)
        self.engine = OscValueSpace()
        self._auto_sig: tuple | None = None
        self._dispatcher.set_default_handler(self._track_input)
        self.apply_config()

    def close(self) -> None:
        if self._events is not None:
            self._events.off("action", self._on_event_action)
            self._events = None

    def apply_config(self) -> None:
        """改名与前缀即时生效：地址每拍现算，这里只刷新收包侧的名字集合。"""
        self._refresh_input_names(self._safe_state())

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
        self._last_send_fail_log = 0.0

    def send_value(self, address: str, value) -> None:
        try:
            self._client.send_message(address, value)
        except Exception as exc:
            log = getattr(self, "log", None)
            if log is None:
                return
            # socket 挂掉时每个 tick 都会发失败：限流，别按发送频率刷日志
            now = time.monotonic()
            if now - self._last_send_fail_log < 30.0:
                return
            self._last_send_fail_log = now
            log(f"OSC 发送 {address} 失败: {exc!r}")

    def _track_input(self, addr: str, *args) -> None:
        self.last_rx = time.monotonic()
        self.rx_count += 1
        if str(addr) == "/avatar/change":
            # 换头像：旧参数名不再有效，读数一并清掉，别留着给卡片当现值
            self._last_sent.clear()
            self.engine.reset()
            return
        if not args:
            return
        # 键 = 去斜杠的收包地址，与登记行同名；读数卡与实时值刷新都按这个键取
        path = str(addr).strip("/")
        if not path:
            return
        self.input_values[path] = {"value": args[0], "ts": time.monotonic()}
        num = as_number(args[0])
        if num is not None:
            self.engine.signals[path] = num
        self._mirror_input(path.rsplit("/", 1)[-1], args[0])

    def _mirror_input(self, name: str, value) -> None:
        if name not in self._input_names:
            return
        writer = self._set_temp
        if writer is None:
            return
        key = f"{TEMP_PATH_PREFIX}{name}"
        if key in self._maintained_keys:
            return
        try:
            writer(key, value)
            self._no_send.add(key)
        except Exception as exc:
            self.log(f"[OSC] 临时变量镜像 {name} 失败: {exc!r}")

    def param_names(self, max_age_s: float = 120.0) -> list[str]:
        """近期收到过的参数：键与登记行同名（去斜杠的收包地址）。"""
        now = time.monotonic()
        names = [name for name, rec in self.input_values.items()
                 if now - rec.get("ts", now) <= max_age_s]
        return sorted(names)

    def recent_inputs(self, max_age_s: float = 30.0) -> list[dict]:
        now = time.monotonic()
        rows = []
        for path, rec in self.input_values.items():
            age = now - rec.get("ts", now)
            if age > max_age_s:
                continue
            rows.append({"param": path, "value": rec.get("value"), "age": age})
        return sorted(rows, key=lambda r: r["param"])

    @staticmethod
    def _local_ip() -> str:
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
                self._check_devices(state)
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
        self._refresh_input_names(state)
        callback = self._on_devices_changed
        if callback is None or state is None:
            return
        try:
            callback(state)
        except Exception as exc:
            self.log(f"[OSC] 设备参数自动接线失败: {exc!r}")

    def _refresh_input_names(self, state) -> None:
        if state is None:
            return
        try:
            names = device_osc_names(state,
                                     self.config.get("device_prefixes") or {})
        except Exception:
            return
        families = {info["family"] for info in names.values()}
        input_names = {default_input_name(self.config, spec["key"])
                       for spec in core_inputs()
                       if not spec["family"] or spec["family"] in families}
        input_names.add(f"{self.config.get('prefix') or 'DGLab'}Emergency")
        self._input_names = input_names

    def _send_keepalive(self) -> None:
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
                    self._maintain_temp(
                        param_name_override(self.config, spec["key"])
                        or f"{info['name']}{spec['signal']}",
                        spec["type"], vals[spec["key"]])
        self._maintain_temp(f"{str(self.config.get('prefix') or 'DGLab').strip('/')}"
                            f"/{param_name_override(self.config, 'Action') or 'Action'}",
                            "Int",
                            float(self._action_value
                                  if time.monotonic() < self._action_until
                                  else 0))

    def _maintain_temp(self, avatar_name: str, kind: str, raw) -> None:
        raw_name = str(avatar_name or "").lstrip("/")
        name = raw_name if "/" in raw_name else f"{TEMP_PATH_PREFIX}{raw_name}"
        self._maintained_keys.add(name)
        self._no_send.discard(name)
        value = _typed_value(kind, raw)
        writer = self._set_temp
        if writer is not None:
            try:
                writer(name, value)
            except Exception as exc:
                self.log(f"[OSC] 临时变量写入 {name} 失败: {exc!r}")
        self._send_if_changed(name, value)

    def _push_values(self) -> None:
        """事件流写进共享变量表的路径变量，按变量名当地址发出去。"""
        for name in sorted(self.engine.temps):
            key = str(name)
            if "/" not in key or key in self._no_send:
                continue
            self._send_if_changed(key, self.engine.temps[key])

    def _send_if_changed(self, name: str, value) -> None:
        if self._last_sent.get(name) == value:
            return
        self.send_value(f"/{str(name).lstrip('/')}", value)
        self._last_sent[name] = value
