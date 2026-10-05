"""VRChat OSC 联动模块：把桥接器以外部模块形式接入宿主。

头像参数是动态定义的：模块收到哪个参数名就以同名参数建立参数表，
不需要在配置里预声明。META["config"] 声明全部配置项，宿主装载
config/osc.json 时自动补齐缺省。

设备接入后自动把设备**可读参数**维护为**路径命名的临时变量**（不建
事件流、不写映射表、不落配置行）：变量名 = 完整 OSC 回传路径（如
``avatar/parameters/DGLabBmtrPressure``），经 ``temp_specs`` 声明为
模块维护行（联动页面板可见），桥接推送循环自动写入实时值并按变量名
回传 OSC，**重命名变量即改回传地址**。用户可在面板自建带表达式的
普通路径变量（桥接同样按名自算回传）。输入侧头像参数值经信号空间
直接可用（联动页实时数据与变量池），派发由用户在事件流自行接线。
映射表仅兼容旧配置（引擎只装载显式行）。
"""

from __future__ import annotations

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.11.0",
    "description": "头像参数动态建表；设备接入即自动维护完整 OSC 路径命名的"
                   "回传临时变量（重命名即改地址），联动页面板可见，"
                   "不建事件流、不写映射表。",
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
from dglab.params import output_specs as _output_specs
from modules.osc_bridge.bridge import (OscBridge, OscConfig,
                                       TEMP_PATH_PREFIX as _TEMP_PATH_PREFIX,
                                       default_input_name, device_osc_names)

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
        """动态参数表：近期收到的头像参数 + 已接入设备的默认参数名与路径。"""
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
        if state is not None:
            # 接入设备的默认名与回传路径也进入变量池：未收到任何 OSC 包
            # 时事件动作与表达式同样可以点选
            settings = self.ctx.settings
            for spec in _wired_inputs(settings, state):
                name = default_input_name(settings, spec["key"])
                pool.setdefault(name, f"头像参数默认名 · {name}")
            for spec in _wired_outputs(settings, state):
                path = _temp_path(spec["name"])
                pool.setdefault(spec["name"], f"头像参数默认名 · {spec['name']}")
                pool.setdefault(path, f"OSC 回传路径（临时变量） · {spec['label']}")
        return sorted(pool.items())

    def temp_specs(self) -> list[dict]:
        """模块自动注册的临时变量声明：接入设备的**全部核心参数**。

        * 输出参数（可读）：``avatar/parameters/<默认输出名>``——桥接自动
          写入核心输出信号实时值并按变量名回传 OSC（模块维护）；
        * 输入参数（可写）：``avatar/parameters/<默认输入名>``——收到的
          同名头像参数值自动镜像（波形选择、开火等，不回传），供事件流
          绑定派发；
        * 与输出变量同名的输入参数（如通道强度双向同名）以输出维护为准。

        变量名即 OSC 回传路径，重命名即改地址（编辑后转为普通变量）。
        """
        if self.ctx is None:
            return []
        try:
            state = self.ctx.engine.get_state()
        except Exception:
            return []
        if state is None:
            return []
        settings = self.ctx.settings
        specs: dict[str, dict] = {}
        for spec in _wired_outputs(settings, state):
            specs[_temp_path(spec["name"])] = {
                "label": str(spec["label"]),
                "desc": f"模块自动维护（{spec['type']}）· 按变量名回传"}
        for spec in _wired_inputs(settings, state):
            name = default_input_name(settings, spec["key"])
            specs.setdefault(_temp_path(name), {
                "label": str(spec["label"]),
                "desc": "收包值自动镜像（模块维护）· 事件流绑定派发用"})
        return [{"key": key, **item} for key, item in specs.items()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        migrate_legacy(ctx.settings)
        materialize_names(ctx.settings)
        _strip_auto_cards(ctx.settings)      # v1.8 事件卡片接线，已废弃
        # 历史记账字段（v1.7 auto_exposed / v1.8-1.9 auto_wired）：废弃即清，
        # 变量建立只看 temps 现状，不依赖任何持久账本
        for stale in ("auto_exposed", "auto_wired"):
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
        """映射表/前缀/临时变量变更后立即重载引擎装载（无需重启桥接）。"""
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

    def _on_devices_changed(self, state) -> None:
        """设备集变化 → 通知联动页刷新维护参数声明（桥接推送循环回调）。

        自动维护的临时变量由 :meth:`temp_specs` 按接入设备动态声明、由
        桥接写入与回传，无需写配置；此处仅清理历史记账字段并让界面
        重建（新版核心面板即见新设备的维护行）。
        """
        settings = self.ctx.settings
        for stale in ("auto_exposed", "auto_wired"):   # 历史记账，废弃即清
            if stale in settings:
                settings.pop(stale)
        bus = getattr(self.ctx, "events", None)
        if bus is not None:
            # 设备变化 = 维护参数集变化，界面需要重建（temp_specs 动态）
            bus.emit("modules_changed", self.id)

    def _write_temp(self, key, value) -> None:
        """维护值 → 宿主共享临时变量空间（发行版核心无 set_temp 时跳过）。"""
        set_temp = getattr(self.ctx, "set_temp", None)
        if set_temp is not None:
            set_temp(str(key), value)

    def _sync_temps(self) -> None:
        """把配置临时变量同步进桥接并重载（模块自算回传立即生效）。"""
        if self.bridge is None:
            return
        self.bridge.config["temps"] = [
            r for r in (self.ctx.settings.get("temps") or [])
            if isinstance(r, dict)]
        self.bridge.apply_config()

    def _reload_logic(self) -> None:
        """请宿主重载逻辑表（新核心：临时变量进引擎与联动页面板实时值）。

        发行版核心没有 reload 钩子，静默跳过——回传链路由模块自算承担。
        """
        host = getattr(self.ctx.engine, "modules", None)
        reload_fn = getattr(host, "reload", None) if host is not None else None
        if reload_fn is None:
            return
        try:
            self.ctx.submit(reload_fn(self.id))
        except Exception as exc:
            self.ctx.log(f"重载事件流/临时变量失败: {exc!r}")


def migrate_legacy(settings) -> bool:
    """旧版逐参数名/input_expr/custom_inputs → ``mappings`` 行表。

    只迁移真正的旧配置内容；引擎仅装载显式行，默认接线由事件流 +
    临时变量承担（见 :meth:`OscModule._on_devices_changed`）。
    """
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


# 回传临时变量的命名前缀由 bridge.TEMP_PATH_PREFIX 提供（模块与桥接共用）


def _temp_path(avatar_name: str) -> str:
    """头像参数名 → 完整回传路径临时变量名。"""
    return _TEMP_PATH_PREFIX + str(avatar_name or "").lstrip("/")


def _strip_auto_cards(settings) -> bool:
    """移除 v1.8 自动创建的事件卡片（OSC …（自动）），改由临时变量回传。"""
    cards = [r for r in (settings.get("events") or [])
             if isinstance(r, dict)]
    kept = [r for r in cards
            if not (str(r.get("name") or "").startswith("OSC ")
                    and str(r.get("name") or "").endswith("（自动）"))]
    if len(kept) != len(cards):
        settings["events"] = kept
        return True
    return False


def _wired_inputs(settings, state) -> list[dict]:
    """当前接入设备对应的核心输入参数（含全局急停，BMTR 无输入参数）。"""
    try:
        names = device_osc_names(state, settings.get("device_prefixes") or {})
    except Exception:
        names = {}
    families = {info["family"] for info in names.values()}
    return [spec for spec in _core_inputs()
            if not spec["family"] or spec["family"] in families]


def _wired_outputs(settings, state) -> list[dict]:
    """当前接入设备的核心输出参数（含全局 Action）。

    返回 ``{key: 核心输出参数 id, label, name: 默认头像参数名, type}``。
    """
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
