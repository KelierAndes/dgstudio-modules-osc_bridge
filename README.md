# VRChat OSC 联动（osc_bridge）

依赖：`python-osc>=1.9`（随仓库 `wheels/` 分发，打包版 DGStudio 安装时直接合并、离线无需 pip；源码运行 pip 补装）。

联动页的 OSC 卡片提供**实时数据 / 事件流 / 临时变量 / 模块设置**四个区块：收到的头像参数即实时数据，设备可读参数由模块建为回传临时变量（见下），数值表达式与事件接线均可在联动页编辑。

头像参数是**动态定义**的：模块收到哪个 `/avatar/parameters/<名称>` 就以同名参数建立参数表，不需要预声明。

## 设备接入即建回传临时变量

设备连接后，模块自动把其**全部可读参数**建立为**临时变量**（不建事件流、不写映射表）：

* **变量名 = 完整 OSC 回传路径**，如 `avatar/parameters/DGLabStrengthA`（郊狼通道 A 强度）、`avatar/parameters/DGLabBmtrPressure`（灵猫气压）、`avatar/parameters/DGLabAction`（App 按键反馈）；
* **表达式 = 核心输出信号**（如 `{BMTR.Pressure}`），可在联动页临时变量表直接改写（如 `{BMTR.Pressure} * 7.5`）；
* 桥接推送循环把**所有路径型临时变量（变量名含 `/`）按变量名回传**到对应 OSC 地址，值变化才发送，并按来源参数类型归一（Int/Bool/Float）；自定义表达式按数值原样发送；
* **重命名变量即改回传地址**（变量接受自定义）；以参数 id 记账（配置里的 `auto_wired`），删除或改名过的变量不会随设备变化复活。

输入侧（头像参数 → 设备）：收到的头像参数值进入信号空间（联动页实时数据可见、变量池可引用 `{DGLabStrengthA}` 等），派发到设备由你在事件流中自行接线（输入动作把变量当前值派发给核心输入参数）。

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
