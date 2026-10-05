# VRChat OSC 联动（osc_bridge）

依赖：`python-osc>=1.9`（随仓库 `wheels/` 分发，打包版 DGStudio 安装时直接合并、离线无需 pip；源码运行 pip 补装）。

联动页的 OSC 卡片提供**实时数据 / 事件流 / 临时变量 / 模块设置**四个区块：收到的头像参数即实时数据，设备参数接线落在事件流卡片上（输入 = 变量变更时直派，输出 = 周期回传），数值进临时变量面板（可引用、可改写表达式）。

头像参数是**动态定义**的：模块收到哪个 `/avatar/parameters/<名称>` 就以同名参数建立参数表，不需要预声明。

## 设备接入即自动接线（事件流 + 临时变量）

设备连接后，模块自动向核心暴露其**全部可写/可读参数**——以事件流与临时变量形式接线，**不再写映射表**：

* **输入值**（vrc 侧参数 → 设备可写参数）：收到的头像参数值镜像进共享临时变量空间（`temp_specs` 声明为「模块维护」行，如 `DGLabStrengthA` = 郊狼通道 A 强度），并为每个核心输入参数建一张**「变量变更时」事件卡片**直派（首拍采基线，不重刷同值、不强推 0，换头像自动清零）；
* **输出值**（核心可读参数 → vrc 侧）：一张**周期事件卡片**（`OSC 状态回传（自动）`）把核心输出信号实时值写入临时变量并回传（如 `BMTR.Pressure` → `DGLabBmtrPressure`），OSC 发送按值变化去重；全局 `Action` 一并回传。

设备接入 / 断开时事件流即时更新（联动页自动刷新）。接线以参数 id 记账（配置里的 `auto_wired`）：用户删除过的卡片/动作不复活，已有卡片与临时变量可在联动页自由编辑（改表达式即改下发/回传值）。映射表仅兼容旧配置（引擎只装载显式行，无默认兜底；新核心装载时自动迁移为事件流）。

默认收发端口 **9000**（VRChat 监听）/ **9001**（VRChat 本机发送），地址、端口与全局前缀在联动页的 OSC 卡片内配置；改动后重新开关桥接生效。输出默认 10 Hz 节流、值变化才发送。

## 默认头像参数名

按设备家族前缀生成，可自行修改；同家族第 2 台自动追加序号（如 `DGLabOvc2…`）。生成规则由核心 `dglab/naming.py` 提供，模块与联动页共用。

| 方向 | 郊狼 | 负鼠 | 灵猫 |
|---|---|---|---|
| **输出**（设备 → 头像） | `DGLabCoyoteStrengthA/B`、`DGLabCoyoteLimitA/B`、`DGLabCoyoteChannelOK_A/B`、`DGLabCoyoteBattery`、`DGLabCoyoteConnected` | `DGLabOvcStrengthA/B`、`DGLabOvcLimitA/B`、`DGLabOvcChannelOK_A/B`、`DGLabOvcBattery`、`DGLabOvcConnected` | `DGLabBmtrPressure`（Float, kPa）、`DGLabBmtrEdgeState`（Int 0-4）、`DGLabBmtrBattery`、`DGLabBmtrConnected` |
| **输入**（头像 → 设备） | `DGLabCoyoteStrengthA/B`、`DGLabCoyoteWaveA/B`、`DGLabCoyoteWaveStepA/B`、`DGLabCoyoteZapA/B`、`DGLabCoyoteFire` | `DGLabOvcInStrengthA/B`、`DGLabOvcInWaveA/B`、`DGLabOvcInWaveStepA/B`、`DGLabOvcInZapA/B`、`DGLabOvcInFire` | — |

全局参数：`DGLabAction`（Int，App 按键反馈 0-9）、`DGLabEmergency`（Bool，急停全部设备）。

## VRChat 侧设置

1. 游戏内打开动作菜单 → **OSC → Enabled** 开启 OSC。
2. 在头像中添加需要的参数（Int / Bool / Float，名称与「联动」页显示的一致——默认名见上表，也可在联动页自行改名）。
3. 若 9000 / 9001 端口被占用（例如 VRCOSC），在「联动」页的 OSC 卡片中改用其他端口。

## 常见问题

* **游戏内收不到 OSC**（VRChat 调试面板提示 Not receiving any OSC）：软件日志里能看到发送记录、游戏却无反应时，通常是**本机 UDP 被第三方加速器拦截**（其内核过滤驱动会吞掉发往本机端口的回环 UDP，表现为起初正常、中途断流）。把加速器切到不拦截本地 UDP 的模式，或测试期间退出加速器。程序会自动把发送目标从 `127.0.0.1` 换成本机网卡地址以绕开回环拦截，但无法对抗内核级劫持。
* **OSC 用着用着失效**：先在 VRChat 手柄菜单 **Options → OSC** 关闭再开启以重建 OSC 处理器，无效则重启游戏。

## 按键动作

本模块注册「发送 OSC 参数…」负鼠按键动作（按下发 1、抬起发 0，OSC 地址可自由填写）；模块未加载时该选项不会出现在绑定选择框里。

## 安装

在 DGStudio「模块」页的在线列表中获取本模块，安装时自动读取本仓库
`requirements.txt` 并 pip 补装依赖，卸载 / 更新即热重载生效。
也可手动把本仓库 `modules/<模块 id>/` 文件夹整个放入应用目录的
`modules/` 下。
