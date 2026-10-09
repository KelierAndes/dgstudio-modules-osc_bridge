
from __future__ import annotations

import re

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.15.2",
    "description": "设备接入即向核心变量表登记全部可读 / 可写参数"
                   "（变量名 = OSC 路径，带可读 / 可写标记，落在变量表可改名栏，"
                   "改名即改收发地址），事件流画布用读数 / 回传卡片直接收发。",
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
                                       default_input_name, default_output_name,
                                       device_osc_names)

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

    def link_params(self) -> list[dict]:
        """本模块向宿主登记的参数行：全部标成可改名，方向按路径判定。

        返回字典而不是 (名字, 标签) 元组——元组会被宿主当成不可改名的系统参数，
        这些头像参数本来就是用户自己的地址，要落在变量表的可改名栏里。
        """
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
                pool.setdefault(_temp_path(spec.get("name")
                                           or default_input_name(
                                               settings, spec["key"])),
                                f"OSC 可读参数 · {spec['label']}")
            for spec in _wired_outputs(settings, state):
                pool.setdefault(_out_var_name(settings, spec),
                                f"OSC 可写参数 · {spec['label']}")
        return [{"name": name, "label": label,
                 "dir": _path_direction(name), "type": "Float",
                 "renamable": True}
                for name, label in sorted(pool.items())]

    def temp_specs(self) -> list[dict]:
        """本模块向变量表登记的行：按当前在连设备实时算出全部路径变量。

        变量名即 OSC 路径（设备参数 avatar/parameters/<名>，全局参数 <前缀>/<名>），
        读写方向也一律按路径判定（见 _path_direction）：同一参数在
        link_params / temp_specs 两处登记必须给出同一个方向，否则核心按并集
        合并后会多出「读写」行。不落配置文件——共享变量表由核心维护，
        模块只负责声明与收发。
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

        def add(name: str, spec: dict) -> None:
            if name in specs:
                return
            writable = _path_direction(name) == "out"
            specs[name] = {
                "label": f"OSC {'可写' if writable else '可读'} · {spec['label']}",
                "dir": _path_direction(name),
                "type": str(spec.get("type") or ""),
                "desc": ("模块登记 · 按变量名回传头像" if writable
                         else "模块登记 · 收包镜像进同名变量")}

        for spec in _wired_inputs(settings, state):
            add(_temp_path(spec.get("name")
                           or default_input_name(settings, spec["key"])), spec)
        for spec in _wired_outputs(settings, state):
            add(_out_var_name(settings, spec), spec)
        return [{"key": key, "renamable": True, **value}
                for key, value in sorted(specs.items())]

    def _state(self):
        try:
            return self.ctx.engine.get_state()
        except Exception:
            return None

    def rename_var(self, old, new) -> str:
        """变量表里改 OSC 参数名：写进 param_names 覆盖表，登记与收发一起跟着改。

        返回空串表示成功，中文说明表示失败（核心会把原名字留在卡片上）。
        """
        if self.ctx is None:
            return "OSC 模块未加载，无法改名"
        settings = self.ctx.settings
        bare_old = _avatar_bare(old, settings)
        bare_new = _avatar_bare(new, settings)
        if not bare_old:
            return "找不到要改名的 OSC 参数"
        if not _NAME_OK.match(bare_new):
            return "变量名需字母开头，可用字母/数字/下划线，或 a/b 形式路径"
        if bare_new == bare_old:
            return ""
        resolved = {k: v for k, v in _resolved_params(
            settings, self._state()).items() if v}
        keys = [k for k, name in resolved.items() if name == bare_old]
        if not keys:
            return "找不到要改名的 OSC 参数"
        if bare_new in set(resolved.values()):
            return f"「{bare_new}」已被其它 OSC 参数占用，换一个名字"
        table = dict(settings.get("param_names") or {})
        for key in keys:
            table[str(key)] = bare_new
        settings["param_names"] = table
        if hasattr(settings, "save"):
            settings.save()
        _retarget_expr_rows(settings, bare_old, bare_new)
        if self.bridge is not None:
            self.bridge.config["param_names"] = table
            self.bridge._input_names = None
        self.ctx.log(f"OSC 参数改名：{old} → {new}")
        return ""

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        if "param_names" in ctx.settings and not isinstance(
                ctx.settings.get("param_names"), dict):
            ctx.settings["param_names"] = {}
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
                    "device_prefixes", "param_names"):
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


def _path_direction(name) -> str:
    """方向按路径判定：avatar/parameters/* 是发给头像的参数（宿主可写回传），
    其余（全局 <前缀>/… 与裸头像参数名）是从头像 / App 收进来的（宿主只读）。"""
    return "out" if str(name or "").startswith(_TEMP_PATH_PREFIX) else "in"


def _temp_path(avatar_name: str) -> str:
    return _TEMP_PATH_PREFIX + str(avatar_name or "").lstrip("/")


def _out_var_name(settings, spec: dict) -> str:
    """可写参数的变量名 = OSC 路径：设备参数走 avatar/parameters/，全局参数走 <前缀>/。

    spec["name"] 已由 _wired_outputs 套上 param_names 改名覆盖表，这里只管拼路径。
    """
    if str(spec["key"]) == "Action":
        prefix = str(settings.get("prefix") or "DGLab").strip("/")
        return f"{prefix}/{_param_override(settings, 'Action') or 'Action'}"
    return _temp_path(spec["name"])


_NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_PREFIX = "avatar/parameters/"


def _avatar_bare(path, settings) -> str:
    """OSC 路径 → 头像参数名：设备参数剥掉 avatar/parameters/，全局参数剥掉前缀。"""
    text = str(path or "").strip().lstrip("/")
    if not text:
        return ""
    if text.startswith(_PATH_PREFIX):
        return text[len(_PATH_PREFIX):].strip()
    prefix = str(settings.get("prefix") or "DGLab").strip("/")
    if prefix and text.startswith(f"{prefix}/"):
        return text[len(prefix) + 1:].strip()
    return text.rsplit("/", 1)[-1].strip()


def _retarget_expr_rows(settings, old: str, new: str) -> None:
    """模块自带的表达式映射行里 {旧名} 令牌跟着改名走，别让配置指空变量。"""
    rows = settings.get("mappings") or []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("expr") or "")
        if "{" + old + "}" in text:
            row["expr"] = text.replace("{" + old + "}", "{" + new + "}")
            changed = True
    if changed:
        settings["mappings"] = rows
        if hasattr(settings, "save"):
            settings.save()


def _resolved_params(settings, state) -> dict[str, str]:
    """{参数键: 当前头像参数名}——改名要按现有名字找回参数键，登记与收发同源。"""
    out: dict[str, str] = {}
    for spec in _wired_inputs(settings, state):
        out[str(spec["key"])] = str(spec.get("name") or "")
    for spec in _wired_outputs(settings, state):
        out.setdefault(str(spec["key"]), str(spec.get("name") or ""))
    for spec in _core_inputs():
        out.setdefault(str(spec["key"]),
                       default_input_name(settings, str(spec["key"])))
    out["Action"] = _param_override(settings, "Action") or "Action"
    return out


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
    """设备真的连着才算：槽位会残留历史设备，只有 connected + paired 同时为真
    才登记参数，否则变量表里会提前铺一表拿不到值的 avatar 参数。"""
    if state is None:
        return False
    if not (getattr(state, "connected", False) and getattr(state, "paired", False)):
        return False
    try:
        return bool(device_osc_names(state, settings.get("device_prefixes") or {}))
    except Exception:
        return False


def _param_override(settings, key: str) -> str:
    """变量表改过名的参数：param_names = {参数键: 头像参数名}。"""
    table = settings.get("param_names") or {}
    if not isinstance(table, dict):
        return ""
    return str(table.get(str(key)) or "").strip()


def _wired_inputs(settings, state) -> list[dict]:
    try:
        names = device_osc_names(state, settings.get("device_prefixes") or {})
    except Exception:
        names = {}
    families = {info["family"] for info in names.values()}
    out: list[dict] = []
    for spec in _core_inputs():
        if spec["family"] and spec["family"] not in families:
            continue
        item = dict(spec)
        item["name"] = _param_override(settings, item["key"]) or             default_input_name(settings, str(item["key"]))
        out.append(item)
    return out


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
                        "name": _param_override(settings, spec["key"])
                        or f"{info['name']}{spec['signal']}",
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
