"""VRChat OSC 联动模块：把桥接器以外部模块形式接入宿主。

头像参数是动态定义的：模块收到哪个参数名就以同名参数建立参数表，
不需要在配置里预声明。META["config"] 声明全部配置项，宿主装载
config/osc.json 时自动补齐缺省。

设备接入后自动向核心暴露该设备全部可写/可读参数，**以事件流 + 临时变量
形式接线，不写映射表**：

* 输入值（vrc 侧参数 → 设备可写参数）：收包值镜像进共享临时变量空间
  （``temp_specs`` 声明为模块维护行），每个核心输入参数建一张
  「变量变更时」事件卡片直派（首拍采基线，不重刷同值、不强推 0）；
* 输出值（核心可读参数 → vrc 侧）：一张周期事件卡片把核心输出信号
  实时值写入临时变量并回传（``out_values`` → OSC 发送按值变化去重）。

接线以参数 id 记账（``auto_wired``）：用户删除过的动作不复活，卡片与
变量均可联动页编辑。映射表仅兼容旧配置（引擎只装载显式行，无默认兜底）。
OscModule 负责桥接器的生命周期（每次启动重建桥接器，
使「修改地址/端口 → 重新开关」立即生效）。
"""

META = {
    "id": "osc_bridge",
    "name": "VRChat OSC 联动",
    "version": "1.8.0",
    "description": "头像参数动态建表；设备接入即以事件流与临时变量自动接线"
                   "（输入按变量变更直派设备，输出按周期回传头像参数），"
                   "联动页可自由编辑，不再使用映射表。",
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
        """动态参数表：近期收到的头像参数 + 已接入设备的默认参数名。"""
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
            # 接入设备的默认头像参数名也进入变量池：未收到任何 OSC 包时
            # 事件卡片与表达式同样可以点选
            settings = self.ctx.settings
            for spec in _wired_inputs(settings, state):
                name = default_input_name(settings, spec["key"])
                pool.setdefault(name, f"头像参数默认名 · {name}")
            for spec in _wired_outputs(settings, state):
                pool.setdefault(spec["name"], f"头像参数默认名 · {spec['name']}")
        return sorted(pool.items())

    def temp_specs(self) -> list[dict]:
        """模块维护的临时变量声明：设备接入后自动暴露的输入/输出值。

        输入值由桥接收包镜像写入（``ctx.set_temp``），输出值由周期事件
        卡片的输出动作写入——联动页「临时变量」面板显示为模块维护行。
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
        for spec in _wired_inputs(settings, state):
            # 输入/输出默认参数名可能同名（如 DGLabStrengthA 双向），
            # 以输入侧声明为准，避免联动页重复行
            specs.setdefault(default_input_name(settings, spec["key"]),
                             {"label": str(spec["label"]),
                              "desc": "OSC 收包写入（模块维护），事件输入动作可引用"})
        for spec in _wired_outputs(settings, state):
            specs.setdefault(spec["name"],
                             {"label": str(spec["label"]),
                              "desc": f"事件输出动作写入（{spec['type']}）· 随 OSC 回传"})
        return [{"key": key, **item} for key, item in specs.items()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        migrate_legacy(ctx.settings)
        materialize_names(ctx.settings)
        if "auto_exposed" in ctx.settings:   # v1.7 映射表记账本，已废弃
            ctx.settings.pop("auto_exposed")

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
            set_temp=self._mirror_temp,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
        """映射表/前缀配置变更后立即重载引擎装载（无需重启桥接）。"""
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

    def _on_devices_changed(self, state) -> None:
        """设备集变化 → 以事件流 + 临时变量接线新设备参数（桥接回调）。

        输入：每个核心输入参数一张「变量变更时」卡片（首拍采基线，不重刷
        同值、不强推 0）；输出：单张周期卡片把核心输出信号写入临时变量并
        回传。以参数 id 记账（auto_wired）：用户删除过的动作不复活。
        """
        settings = self.ctx.settings
        wired = {str(x) for x in (settings.get("auto_wired") or [])}
        cards = [e for e in (settings.get("events") or [])
                 if isinstance(e, dict)]
        changed = False
        for spec in _wired_inputs(settings, state):
            if spec["key"] in wired:
                continue
            name = default_input_name(settings, spec["key"])
            cards.append({"name": f"OSC {spec['label']}（自动）",
                          "trigger": "change", "arg": name,
                          "actions": [{"dir": "in", "param": spec["key"],
                                       "var": name}]})
            wired.add(spec["key"])
            changed = True
        new_outs = []
        for spec in _wired_outputs(settings, state):
            if spec["key"] in wired:
                continue
            new_outs.append({"dir": "out", "param": spec["key"],
                             "var": spec["name"], "name": spec["name"],
                             "type": spec["type"]})
            wired.add(spec["key"])
            changed = True
        if new_outs:
            card = next((e for e in cards if e.get("name") == _OUT_CARD), None)
            if card is None:
                card = {"name": _OUT_CARD, "trigger": "period", "arg": 50,
                        "actions": []}
                cards.append(card)
            card["actions"] = list(card.get("actions") or []) + new_outs
        if "auto_exposed" in settings:      # v1.7 映射表记账本，已废弃
            settings.pop("auto_exposed")
            changed = True
        if not changed:
            return
        settings["events"] = cards
        settings["auto_wired"] = sorted(wired)
        self._reload_logic()
        bus = getattr(self.ctx, "events", None)
        if bus is not None:
            bus.emit("modules_changed", self.id)

    def _reload_logic(self) -> None:
        """让宿主重载逻辑表：临时变量/事件流装载进引擎并启动事件节拍。"""
        host = getattr(self.ctx.engine, "modules", None)
        reload_fn = getattr(host, "reload", None) if host is not None else None
        if reload_fn is None:
            return
        try:
            self.ctx.submit(reload_fn(self.id))
        except Exception as exc:
            self.ctx.log(f"重载事件流/临时变量失败: {exc!r}")

    def _mirror_temp(self, key, value) -> None:
        """收包值 → 宿主共享临时变量空间（老宿主无 set_temp 时跳过）。"""
        set_temp = getattr(self.ctx, "set_temp", None)
        if set_temp is not None:
            set_temp(str(key), value)


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


# 输出接线共用的事件卡片名（模块维护，设备变化时向其追加输出动作）
_OUT_CARD = "OSC 状态回传（自动）"


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
