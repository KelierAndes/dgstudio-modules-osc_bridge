"""VRChat OSC 联动模块：把桥接器以外部模块形式接入宿主。

头像参数是动态定义的：模块收到哪个参数名就以同名参数建立参数表，
不需要在配置里预声明。联动关系只落在两张映射表上
（``mappings`` 核心输入参数 ← 表达式，``outputs`` 头像参数名 ← 核心输出参数表达式），
配置文件除桥接设置外只保存这两张表。META["config"] 声明全部配置项，
宿主装载 config/osc.json 时自动补齐缺省，联动页据此渲染映射表与模块设置。
OscModule 负责桥接器的生命周期（每次启动重建桥接器，
使「修改地址/端口 → 重新开关」立即生效）。
"""

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.5.0",
    "description": "头像参数动态建表，核心参数映射表双向表达式驱动："
                   "设备数值经表达式写回头像参数，头像参数/游戏信号反向控制设备。",
    "settings_key": "osc",
    "actions": ["osc"],
    "default_enabled": False,
    "dynamic_params": True,
    "config": {
        # ---- 桥接通道设置（全局） ----
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
        # ---- 两张映射表（配置文件只写这些） ----
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
from modules.osc_bridge.bridge import (OscBridge, OscConfig,
                                       default_input_name,
                                       default_input_rows,
                                       default_output_rows)

# 配置缺省值唯一来源 = META["config"] 声明，OscConfig 仅做兜底
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
        """动态参数表：近期收到的头像参数名。"""
        if self.bridge is None:
            return []
        return [(name, f"头像参数 · {name}") for name in self.bridge.param_names()
                if name not in ("change",)]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        migrate_legacy(ctx.settings)
        materialize_names(ctx.settings)

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
        materialize(self.ctx.settings, self.ctx.engine.get_state())
        cfg = OscConfig(self.ctx.settings, defaults=OSC_CONFIG_DEFAULTS)
        self.bridge = OscBridge(
            cfg,
            self.ctx.engine.get_state,
            self.ctx.engine,
            events=self.ctx.events,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
        """映射表编辑后立即重载（无需重启桥接）。"""
        if self.bridge is None:
            return
        for key in ("mappings", "outputs", "prefix", "device_prefixes"):
            self.bridge.config[key] = self.ctx.settings.get(
                key, OSC_CONFIG_DEFAULTS[key])
        self.bridge.apply_config()

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()

    def is_running(self) -> bool:
        return self.bridge is not None and bool(getattr(self.bridge, "_running", False))

    def button_actions(self) -> list:
        """负鼠按键「发送 OSC 参数」动作（随本模块安装/卸载出现与撤下）。"""
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


def migrate_legacy(settings) -> bool:
    """旧版逐参数名/input_expr/custom_inputs → ``mappings`` 行表（输出表在启动时按
    当前设备落地，见 :func:`materialize`）。"""
    changed = False
    if not _rows(settings.get("mappings")):
        rows = _legacy_input_rows(settings) or default_input_rows(settings)
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


def materialize(settings, state) -> bool:
    """启动时把空的输出表按当前接入设备落地成可编辑的完整行。"""
    changed = False
    if not _rows(settings.get("outputs")):
        rows = default_output_rows(settings, state)
        if rows:
            settings["outputs"] = rows
            changed = True
    if changed and "output_map" in settings:
        settings.pop("output_map", None)
    if changed:
        if hasattr(settings, "save"):
            settings.save()
    return changed


def materialize_names(settings) -> bool:
    """输入行缺 ``name`` 时按设备前缀模板补默认头像参数名（仅显示层）。"""
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
    # 自定义输入：并入同一核心参数的表达式（多个参数取较大值）
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
