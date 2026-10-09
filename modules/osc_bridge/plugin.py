
from __future__ import annotations

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.13.0",
    "description": "设备接入即向核心变量表登记全部可读 / 可写参数"
                   "（变量名 = OSC 路径，带可读 / 可写标记，由模块实时维护不落配置），"
                   "事件流画布用读数 / 回传卡片与读写变量卡片直接收发。",
    "settings_key": "osc",
    "actions": ["osc"],
    "default_enabled": False,
    "dynamic_params": True,
    "config": {
        "out_ip": {
            "label": "VRChat 地址", "type": "str", "default": "127.0.0.1",
            "group": "bridge", "desc": "OSC 输出目标 IP（127.0.0.1 自动换为网卡地址）",
        },
        "out_port": {
            "label": "输出端口", "type": "int", "default": 9000,
            "min": 1, "max": 65535, "group": "bridge", "desc": "发送设备数值到 VRChat 的端口",
        },
        "in_port": {
            "label": "监听端口", "type": "int", "default": 9001,
            "min": 1, "max": 65535, "group": "bridge", "desc": "接收 VRChat 数据的端口",
        },
        "rate_hz": {
            "label": "发送频率", "type": "int", "default": 10,
            "min": 1, "max": 30, "unit": "Hz", "group": "bridge",
            "desc": "设备数值回写头像参数的频率",
        },
        "prefix": {
            "label": "全局参数前缀", "type": "str", "default": "DGLab",
            "group": "bridge", "desc": "Action / Emergency 等全局参数的前缀",
        },
        "device_prefixes": {
            "label": "设备参数前缀", "type": "map",
            "default": {"COYOTE": "DGLab", "OVC": "DGLabOvc", "BMTR": "DGLabBmtr"},
            "group": "settings",
            "desc": "默认映射行的参数名前缀（仅在映射表为空时用于自动生成）",
        },
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，表达式以 {头像参数名} "
                    "引用动态参数表，可混合核心输出参数，结果取整钳制后派发；"
                    "留空即同名直传",
        },
        "outputs": {
            "label": "输出映射表", "type": "list", "default": [],
            "group": "map", "rows": "out",
            "desc": "行 {param: 核心输出参数, name: 头像参数名, expr: 表达式}，"
                    "求值后写入 /avatar/parameters/<name>，参数名可自由更改",
        },
    },
}

from plugins import ButtonAction, ModuleBase, spec_defaults

from dglab.params import core_inputs as _core_inputs
from dglab.params import output_specs as _output_specs
from modules.osc_bridge.bridge import (OscBridge, OscConfig,
                                       TEMP_PATH_PREFIX as _TEMP_PATH_PREFIX,
                                       default_input_name, device_osc_names)

OSC_CONFIG_DEFAULTS = spec_defaults(META["config"])


class OscModule(ModuleBase):
    id = META["id"]
    name = META["name"]
    version = META["version"]
    description = META["description"]
    settings_key = META["settings_key"]

    def __init__(self):
        self.bridge: OscBridge | None = None
        self.ctx = None

    def config_spec(self) -> dict:
        return META["config"]

    def link_params(self) -> list[tuple[str, str]]:
        if self.bridge is None:
            return []
        pool: dict[str, str] = {}
        for name in self.bridge.param_names():
            if name != "change":
                pool[name] = f"头像参数 · {name}"
        state = None
        try:
            state = self.bridge.get_state()
        except Exception:
            state = None
        settings = self.ctx.settings
        if state is not None and _has_devices(settings, state):
            for spec in _wired_inputs(settings, state):
                name = default_input_name(settings, spec["key"])
                pool.setdefault(_temp_path(name), f"OSC 可读参数 · {spec['label']}")
            for spec in _wired_outputs(settings, state):
                pool.setdefault(_out_var_name(settings, spec),
                                f"OSC 可写参数 · {spec['label']}")
        return sorted(pool.items())

    def temp_specs(self) -> list[dict]:
        """本模块向变量表登记的行：按当前在连设备实时算出全部可读 / 可写参数。

        变量名即 OSC 路径（设备参数 avatar/parameters/<名>，全局参数 <前缀>/<名>），
        不落配置文件——共享变量表由核心维护，模块只负责声明与收发。
        """
        if self.ctx is None:
            return []
        try:
            state = self.ctx.engine.get_state()
        except Exception:
            state = None
        settings = self.ctx.settings
        if not _has_devices(settings, state):
            return []       # 设备没连上就不预登记：连上哪个设备才出哪些参数
        specs: dict[str, dict] = {}
        for spec in _wired_inputs(settings, state):
            name = _temp_path(default_input_name(settings, spec["key"]))
            specs[name] = {"label": f"OSC 可读 · {spec['label']}", "dir": "in",
                           "desc": "模块登记 · 收包镜像进同名变量"}
        for spec in _wired_outputs(settings, state):
            name = _out_var_name(settings, spec)
            row = specs.get(name)
            if row is None:
                specs[name] = {"label": f"OSC 可写 · {spec['label']}",
                               "dir": "out", "desc": "模块登记 · 按变量名回传"}
            else:
                row["dir"] = "inout"
                row["label"] = f"OSC 可读/可写 · {spec['label']}"
        return [{"key": key, **value} for key, value in sorted(specs.items())]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        migrate_legacy(ctx.settings)
        materialize_names(ctx.settings)
        _strip_auto_cards(ctx.settings)
        for stale in ("auto_exposed", "auto_wired", "temps"):
            if stale in ctx.settings:
                ctx.settings.pop(stale)

    def on_unload(self) -> None:
        if self.bridge is not None:
            self.bridge.close()
        self.bridge = None
        self.ctx = None

    async def start(self) -> None:
        if self.bridge is not None and self.bridge._running:
            return
        if self.bridge is not None:
            try:
                await self.bridge.stop()
            except Exception:
                pass
        cfg = OscConfig(self.ctx.settings, defaults=OSC_CONFIG_DEFAULTS)
        self.bridge = OscBridge(
            cfg,
            self.ctx.engine.get_state,
            self.ctx.engine,
            events=self.ctx.events,
            on_devices_changed=self._on_devices_changed,
            set_temp=self._write_temp,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
        if self.bridge is None:
            return
        for key in ("mappings", "outputs", "temps", "prefix",
                    "device_prefixes"):
            self.bridge.config[key] = self.ctx.settings.get(
                key, OSC_CONFIG_DEFAULTS.get(key))
        self.bridge.apply_config()

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()

    def is_running(self) -> bool:
        return self.bridge is not None and bool(getattr(self.bridge, "_running", False))

    def button_actions(self) -> list:
        return [ButtonAction(
            key="osc",
            label="发送 OSC 参数…",
            argument_placeholder="/avatar/parameters/…",
            on_press=self._send_press,
            on_release=self._send_release,
        )]

    def _send_press(self, slot_id, argument) -> None:
        self._send(argument, 1)

    def _send_release(self, slot_id, argument) -> None:
        self._send(argument, 0)

    def _send(self, address, value: int) -> None:
        address = str(address or "").strip()
        if not address:
            return
        bridge = self.ctx.engine.osc
        if bridge is None or not getattr(bridge, "_running", False):
            self.ctx.log(f"OSC 桥接未运行，无法发送 {address} = {value}")
            return
        bridge.send_value(address, value)
        self.ctx.log(f"OSC {address} = {value}")

    def _on_devices_changed(self, state) -> None:
        settings = self.ctx.settings
        for stale in ("auto_exposed", "auto_wired", "temps"):
            if stale in settings:
                settings.pop(stale)
        bus = getattr(self.ctx, "events", None)
        if bus is not None:
            bus.emit("modules_changed", self.id)

    def _write_temp(self, key, value) -> None:
        set_temp = getattr(self.ctx, "set_temp", None)
        if set_temp is not None:
            set_temp(str(key), value)

    def _sync_temps(self) -> None:
        if self.bridge is None:
            return
        self.bridge.config["temps"] = [
            r for r in (self.ctx.settings.get("temps") or [])
            if isinstance(r, dict)]
        self.bridge.apply_config()

    def _reload_logic(self) -> None:
        host = getattr(self.ctx.engine, "modules", None)
        reload_fn = getattr(host, "reload", None) if host is not None else None
        if reload_fn is None:
            return
        try:
            self.ctx.submit(reload_fn(self.id))
        except Exception as exc:
            self.ctx.log(f"重载事件流/临时变量失败: {exc!r}")


def migrate_legacy(settings) -> bool:
    changed = False
    if not _rows(settings.get("mappings")):
        rows = _legacy_input_rows(settings)
        if rows:
            settings["mappings"] = rows
            changed = True
    if changed or _has_legacy(settings):
        for key in ("input_expr", "custom_inputs"):
            settings.pop(key, None)
        for spec in _core_inputs():
            settings.pop(spec["key"], None)
    if changed:
        if hasattr(settings, "save"):
            settings.save()
    return changed


def _temp_path(avatar_name: str) -> str:
    return _TEMP_PATH_PREFIX + str(avatar_name or "").lstrip("/")


def _out_var_name(settings, spec: dict) -> str:
    """可写参数的变量名 = OSC 路径：设备参数走 avatar/parameters/，全局参数走 <前缀>/。"""
    if str(spec["key"]) == "Action":
        return f"{str(settings.get('prefix') or 'DGLab').strip('/')}/Action"
    return _temp_path(spec["name"])


def _strip_auto_cards(settings) -> bool:
    cards = [r for r in (settings.get("events") or [])
             if isinstance(r, dict)]
    kept = [r for r in cards
            if not (str(r.get("name") or "").startswith("OSC ")
                    and str(r.get("name") or "").endswith("（自动）"))]
    if len(kept) != len(cards):
        settings["events"] = kept
        return True
    return False


def _has_devices(settings, state) -> bool:
    """有没有设备在连：没有槽位就什么都不登记，避免提前铺一表参数。"""
    if state is None:
        return False
    try:
        return bool(device_osc_names(state, settings.get("device_prefixes") or {}))
    except Exception:
        return False


def _wired_inputs(settings, state) -> list[dict]:
    try:
        names = device_osc_names(state, settings.get("device_prefixes") or {})
    except Exception:
        names = {}
    families = {info["family"] for info in names.values()}
    return [spec for spec in _core_inputs()
            if not spec["family"] or spec["family"] in families]


def _wired_outputs(settings, state) -> list[dict]:
    out: list[dict] = []
    try:
        names = device_osc_names(state, settings.get("device_prefixes") or {})
    except Exception:
        names = {}
    for sid in sorted(names):
        info = names[sid]
        for spec in _output_specs(info["family"], int(info.get("index", 1))):
            out.append({"key": spec["key"], "label": spec["label"],
                        "name": f"{info['name']}{spec['signal']}",
                        "type": spec["type"]})
    prefix = str(settings.get("prefix") or "DGLab")
    out.append({"key": "Action", "label": "App 按键反馈",
                "name": f"{prefix}Action", "type": "Int"})
    return out


def materialize_names(settings) -> bool:
    rows = settings.get("mappings") or []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("param") or "")
        if key and not str(row.get("name") or "").strip():
            row["name"] = default_input_name(settings, key)
            changed = True
    if changed:
        settings["mappings"] = rows
        if hasattr(settings, "save"):
            settings.save()
    return changed


def _rows(rows) -> list:
    return [row for row in (rows or []) if isinstance(row, dict)
            and str(row.get("param") or "").strip()]


def _has_legacy(settings: dict) -> bool:
    if any(key in settings for key in ("input_expr", "custom_inputs")):
        return True
    return any(str(spec["key"]) in settings for spec in _core_inputs())


def _legacy_input_rows(settings: dict) -> list[dict]:
    exprs = {str(k): str(v or "").strip()
             for k, v in (settings.get("input_expr") or {}).items()}
    rows: list[dict] = []
    for spec in _core_inputs():
        key = spec["key"]
        text = exprs.get(key) or ""
        if not text:
            name = str(settings.get(key) or "").strip()
            if not name:
                continue
            text = "{" + name + "}"
        rows.append({"param": key, "expr": text})
    for entry in (settings.get("custom_inputs") or []):
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target") or "").strip()
        param = str(entry.get("param") or "").strip()
        if not target or not param:
            continue
        token = "{" + param + "}"
        row = next((r for r in rows if r["param"] == target), None)
        if row is None:
            rows.append({"param": target, "expr": token})
        elif token not in row["expr"]:
            row["expr"] = f"max({row['expr']},{token})"
    return rows
