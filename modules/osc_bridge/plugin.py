
from __future__ import annotations

import re

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.19.0",
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
            "desc": "设备头像参数的默认名字前缀（连上第 2 台同家族自动带序号）",
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

    def link_params(self) -> list[dict]:
        """本模块向宿主登记的参数行：全部标成可改名，方向按数据流判定。

        返回字典而不是 (名字, 标签) 元组——元组会被宿主当成不可改名的系统参数，
        这些头像参数本来就是用户自己的地址，要落在变量表的可改名栏里。
        """
        if self.bridge is None:
            return []
        pool: dict[str, dict] = {}
        state = None
        try:
            state = self.bridge.get_state()
        except Exception:
            state = None
        settings = self.ctx.settings
        if state is not None and _has_devices(settings, state):
            pool.update(_wired_rows(settings, state))
        for path in self.bridge.param_names():
            if path == "avatar/change":
                continue
            # 收到的参数复用已登记的同名行；没登记过的按收包路径补一行，
            # 不再按短名另造一行（那会让同一参数在变量表里出现两次）
            leaf = str(path).rsplit("/", 1)[-1]
            pool.setdefault(str(path), {"label": f"头像参数 · {leaf}",
                                        "dir": "in", "type": "Float"})
        return [{"name": name, "label": row["label"], "dir": row["dir"],
                 "type": row["type"], "renamable": True}
                for name, row in sorted(pool.items())]

    def temp_specs(self) -> list[dict]:
        """本模块向变量表登记的行：按当前在连设备实时算出全部路径变量。

        变量名即 OSC 路径（设备参数 avatar/parameters/<名>，全局参数 <前缀>/<名>），
        方向按数据流判定（见 _wired_rows）：头像发入的核心输入参数宿主可读、
        核心读数回传头像宿主可写。不落配置文件——共享变量表由核心维护，
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
        return [{"key": key, "renamable": True, **value}
                for key, value in sorted(_wired_rows(settings, state).items())]

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
        if self.bridge is not None:
            self.bridge.config["param_names"] = table
            self.bridge.apply_config()
        self.ctx.log(f"OSC 参数改名：{old} → {new}")
        return ""

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        if "param_names" in ctx.settings and not isinstance(
                ctx.settings.get("param_names"), dict):
            ctx.settings["param_names"] = {}
        drop_retired_rows(ctx.settings, ctx.log)

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
            events=self.ctx.events,
            on_devices_changed=self._on_devices_changed,
            set_temp=self._write_temp,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
        if self.bridge is None:
            return
        for key in ("prefix", "device_prefixes", "param_names"):
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

def drop_retired_rows(settings, log=None) -> bool:
    """清掉表达式映射时代的配置行。

    那套 `MappingEngine` 握着核心的 `set_strength` / `fire`，绕过「模块只登记
    变量、设备动作由事件流写入卡驱动」的拦截，已经整层拆掉；旧设置里留下的
    `mappings` / `outputs` 行留着也不会再生效，只会让人以为模块里还有映射表。
    """
    retired = ("mappings", "outputs", "temps", "events", "input_expr",
               "custom_inputs", "auto_exposed", "auto_wired")
    dropped = [key for key in retired if key in settings]
    per_param = [str(spec["key"]) for spec in _core_inputs()
                 if str(spec["key"]) in settings]
    if not dropped and not per_param:
        return False
    for key in dropped + per_param:
        settings.pop(key, None)
    if hasattr(settings, "save"):
        settings.save()
    if log is not None:
        log("OSC：已清除表达式映射时代的遗留配置行（"
            + "、".join(dropped + per_param) + "）；换算与设备动作请在事件流里连线")
    return True


def _wired_rows(settings, state) -> dict[str, dict]:
    """{变量名: 登记行}，方向按**数据流**判定，不按路径判定。

    核心输入参数（强度 / 波形 / 步进 / 开火 / 急停…）的值由头像发进来，宿主
    读出来再写核心参数 → 可读（in）；设备读数（电量 / 连接状态 / 通道状态 /
    上限 / 气压 / 按键反馈）由核心读出后按变量名发回头像 → 可写（out）。
    两类都挂在 avatar/parameters/ 路径下。同名两边都有（通道强度既下发又
    回读）记成读写。
    """
    rows: dict[str, dict] = {}
    for spec in _wired_inputs(settings, state):
        name = _temp_path(spec.get("name")
                          or default_input_name(settings, spec["key"]))
        rows[name] = {"label": f"OSC 可读 · {spec['label']}", "dir": "in",
                      "type": str(spec.get("type") or ""),
                      "desc": "模块登记 · 收包镜像进同名变量，读出后写核心参数"}
    for spec in _wired_outputs(settings, state):
        name = _out_var_name(settings, spec)
        row = rows.get(name)
        if row is None:
            rows[name] = {"label": f"OSC 可写 · {spec['label']}", "dir": "out",
                          "type": str(spec.get("type") or ""),
                          "desc": "模块登记 · 核心读数按变量名回传头像"}
        else:
            row["dir"] = "inout"
            row["label"] = f"OSC 读写 · {spec['label']}"
    return rows


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
        item["name"] = (_param_override(settings, item["key"])
                          or default_input_name(settings, str(item["key"])))
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
