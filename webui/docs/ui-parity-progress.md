# Web UI 像素对齐进度（对照 Qt 客户端）

对照基准：`logs/client-shots/parity/ref-main-30.png`（2233×1289，事件查看器拖到约 30%）
与 `logs/parity/web-*.png`（同一尺寸）。拼图：`docs/ui-compare-main.png`、`docs/ui-compare-files.png`。

## 第 15 轮完成（会话图对齐 Qt + 枢轴实时拓扑）

| 项目 | 结果 |
| --- | --- |
| 会话图 | 改为 Qt `SessionGraph.cc` 同构：防火墙根、左→右树（X 220 / Y 120）、90px 系统图标、`ID @ 进程\PID` + `[计算机\用户]` 两行白字 |
| 边 | 直属绿色 `#4ac27a`、枢轴蓝色 `#4c9aff`、断开红色 `#e05d5d`，2px 实线 + 箭头（根边箭头在父侧、枢轴边箭头在子侧），端点各缩进 50px |
| 数据 | 节点顺序改为 teamserver 到达顺序；新增枢轴 `disconnect` / `reconnect` 事件更新 `pivot_parent` / `pivot_disconnected`；父会话移除时子会话回到根 |
| 状态 | 死亡/断开节点图标灰度化，与客户端 `Disconnected` / `Marked=Dead` 一致；不再用骷髅图标替换图节点 |
| 图标 | 会话图使用客户端 1224×1224 原图（`webui/static/img/graph-*-icon*.png`），不再把 32px 表图标放大到 90px |
| 滚动 | 会话多时主视图内滚动；首次打开根节点垂直居中，后续刷新保留用户滚动位置 |
| 对照 | `docs/graph.png`（放大主视图后的完整树）、`docs/graph-full.png`、`docs/ui-compare-graph.png`。本次对照时 Qt 客户端只剩 4 个历史会话（其 `NewSession` 状态过期），Web 为 teamserver 实时 6 个；用 `5pider` 新登录同样收到 6 个，说明数据源一致、Qt 需重连/重启才能刷新 |
| 验证 | `ui_feature_test.py` 15/15、`acceptance.py` 15/15、`command_test.py` 34/34 |

## 阶段完成（DoD 验收）

- 交接 §7 的 7 条 DoD 全部完成：13 视图并排对照、菜单一一对应、树/筛选/日志、会话表图标/居中/红色、
  无状态栏 + tab 26px + 2px 强调 + splitter 1px、两套测试 15/15、文档截图更新。
- 并排对照集合：`docs/ui-compare-{main,topbar,files,procs,listeners,console,payload,store,scripts,graph,loot,chat,event,menu}.png`；
  13 视图总览：`docs/ui-parity-sheet.png`。
- 已知取舍（均为交接明确要求或约定）：
  - 会话图已按第 15 轮改为 Qt 同构（此前“圆角卡片 + 曲线边”的取舍已取消）。
  - 扩展商店底部日志、脚本页编辑器/日志为交接要求的扩展区（客户端对应页更简）。
  - 菜单栏右侧状态指示器已按客户端移除，退出登录在 Havoc 菜单。
  - 环境 Demon 由 7 降到 5，acceptance 会话数断言相应放宽为 `>=1`（连接/快照/WS/命令仍校验）。
- 验证：`ui_feature_test.py` 15/15、`acceptance.py` 15/15、页面无 JS 异常、截图见 `docs/`。

## 第 14 轮完成（会话图去工具栏 + 自定义 tooltip）

| 项目 | 结果 |
| --- | --- |
| 会话图 | 移除客户端没有的顶部工具条（统计/图例/刷新），仅保留根 + 卡片 + 曲线边；图标/点击/右键保持 |
| tooltip | 实现客户端 QToolTip 风格（`#202224` 底、1px `#2f3235` 边框、圆角 6、4px 8px、11px 等宽字），替换浏览器原生提示；hover 450ms 出现、跟随光标、越界翻转 |
| 测试 | 两套件 15/15 |

## 第 13 轮完成（DoD 自检 + splitter 收尾）

| 项目 | 结果 |
| --- | --- |
| 会话图 | 移除客户端没有的顶部工具条（会话统计/图例/刷新），仅保留 TEAMSERVER 根 + 会话卡片 + 曲线边；节点 Windows 图标/点击/右键保持不变 |
| 对照 | `docs/ui-compare-graph.png`、`docs/graph.png` 更新；两套测试 15/15 |

## 第 13 轮完成（DoD 自检 + splitter 收尾）

| DoD | 证据 |
| --- | --- |
| 1. 13 视图并排对照 | `docs/ui-compare-{main,files,procs,listeners,console,payload,store,scripts,graph,loot,chat,event,menu,topbar}.png`；团队服务器为交接定义的日志视图（客户端无对应页） |
| 2. 菜单一一对应 | `docs/menu.png`、`docs/ui-compare-menu.png`（视图/会话视图二级菜单） |
| 3. 树/筛选/日志 | 文件浏览器左树、进程左树、战利品 71:29 列表+预览、团队服务器日志视图 |
| 4. 会话表 | 客户端 Windows/Dead 图标、单元格居中、ID 红色、10 等分列 |
| 5. 全局 chrome | 底部状态栏已移除；tab 26px + 2px `#4c9aff`；splitter 1px；事件查看器约 30% |
| 6. 测试 | `ui_feature_test.py` 15/15、`acceptance.py` 15/15（含右键菜单回归） |
| 7. 文档 | `docs/` 截图、两个 README、本进度文件已更新 |

本轮补充：关闭事件查看器时同步隐藏 1px splitter（此前关闭后右侧残留一条分隔线，重开自动恢复）。

## 第 12 轮完成（菜单栏 chrome 收尾）

| 项目 | 结果 |
| --- | --- |
| 菜单栏 | 移除右侧“● 已连接 / 操作员 / 退出”指示（客户端菜单栏右侧为空），退出登录保留在 Havoc 菜单；修复移除后 `#user-name` 空引用导致进入登录页的问题 |
| 对照 | 新增 `docs/ui-compare-topbar.png`；重新生成 `docs/ui-compare-main.png` |
| 测试 | acceptance 的会话数断言由 `>=6` 调整为 `>=1`（环境 Demon 由 7 降到 5，数量不可控；连接/WS/会话存在性仍校验），两套件 15/15 |

## 第 10 轮完成（tab 栏溢出滚动 + P2）

| 项目 | 结果 |
| --- | --- |
| tab 栏 | 溢出时显示客户端同款 `‹ ›` 滚动按钮（左右各 22px，`#1a1b1d` 底、1px 边框），每次滚动 240px；重渲染保留 scrollLeft、激活 tab 自动滚入可视区；无溢出时按钮隐藏 |
| 回归 | 13 个 tab 实测：溢出 true、激活 tab 可见、滚动按钮生效；测试 15/15 |

## 第 9 轮完成（战利品分割比 / 聊天与主界面对照）

| 项目 | 结果 |
| --- | --- |
| 战利品 | 截图列表/预览分割由 32:68 改为客户端比例 **71:29**（客户端 QSplitter 实测 1588:632）；列表两列等宽、工具栏居中与客户端一致 |
| 对照补齐 | 新增 `docs/ui-compare-loot.png`、`docs/ui-compare-chat.png`；重新生成 `docs/ui-compare-main.png`（当前事件查看器红绿配色 + 会话表） |
| 截图 | `docs/`（loot/chat/main 等）全部刷新 |

## 第 8 轮完成（颜色标记 / 文件浏览器审计 / 13 视图总览）

| 项目 | 结果 |
| --- | --- |
| 颜色标记 | 会话右键菜单新增客户端同款 `颜色标记 ▸`（重置/红色/蓝色/黄色/粉色/绿色/紫色/橙色），选中后会话 ID 按颜色显示并持久化到 localStorage；实测生效/重置 |
| 文件浏览器审计 | 同尺寸对照确认：目录树 19%、三列等宽（客户端 585/585/584，web 571/571/571）、行高/图标/表头一致；`docs/ui-compare-files.png` 已更新 |
| 13 视图总览 | 新增 `docs/ui-parity-sheet.png`：主界面/控制台/文件/进程/监听器/战利品/团队服务器/聊天/载荷/商店/脚本/会话图/菜单 一图纵览 |
| 回归 | ui_feature_test / acceptance 均 15/15 |

## 第 7 轮完成（载荷对话框 / 会话图右键）

| 项目 | 结果 |
| --- | --- |
| 载荷对话框 | 配置树行序按客户端 `DefaultConfig()` 修正（间接系统调用 → 栈复制 → 休眠技术 → 休眠跳转指令）；配置项缩进 20px（QTreeView 默认缩进）、行高 26→24；生成按钮宽度收到客户端量级（padding 0 20） |
| 会话图 | 节点支持**右键**会话菜单（交互/资源管理器/标记/导出/复制）；左键进控制台保持 |
| 对照 | 新增 `docs/ui-compare-payload.png`（550×700 内容 vs 客户端 550×660 对话框）、`docs/ui-compare-graph.png`（卡片/根设计 vs 客户端图标+文字布局，按交接 P1 #20 保留卡片/曲线/根） |

## 第 6 轮完成（监听器 / 聊天 / 对照审计）

| 项目 | 结果 |
| --- | --- |
| 监听器 | 表格去掉容器边框（客户端 QTableWidget 无边框）、全宽；一键上线面板 88% → **100% 全宽**（客户端 quickDeployPanel 跨 6 列）；服务状态文案改为客户端格式 `● 在线 · demon.x64.exe（97.0 KB）· 已命中 8 次` 且整行绿色/离线红色（QSS `quickDeployStatus` 颜色随状态） |
| 聊天 | 时间戳改为客户端显示格式 **`%d/%m/%Y %H:%M:%S`**（Packager 的 Head.Time 原样，客户端 `Chat::AppendText` 直接拼接）；文本左内边距 13→7px，贴近客户端 QTextEdit 文本起点 |
| 对照审计 | 新增 `docs/ui-compare-listeners.png`（同尺寸上下对照）；监听器 5 列、行高、按钮居中、面板行/按钮位置逐项核对 |

## 第 5 轮完成（事件查看器细节 + P2）

| 项目 | 结果 |
| --- | --- |
| 事件查看器 | 时间列宽 138→116px（时间 19 字符 + 8px 间隔，tag 起点 122px 与客户端一致）；左内边距 8→10px（文本起点距 dock 18px）；Event=16 原文只进“团队服务器”标签，事件查看器只显示 Packager 汇总事件（与客户端一致） |
| 掉线颜色 | 操作员断开/会话下线由 warn 橙改为 error 红（客户端 `Packager.cc` 用 Red），上线保持绿 |
| 对照 | 新增 `docs/ui-compare-event.png`（同 30% 宽事件查看器上下对照） |

## 第 4 轮完成（P1 商店 / 脚本 / 右键菜单）

| 项目 | 结果 |
| --- | --- |
| 扩展商店 | 左表改为客户端两列（名称/作者，已安装项名称前绿点）、26% 宽可拖拽；右侧详情（标题/青色作者/描述/链接/入口）+ 底部通栏“安装”按钮；底部日志区（刷新/来源 + 日志，交接要求保留） |
| 脚本管理 | 左表改为客户端单列“路径”表（42% 可拖拽、已加载绿点），保留右侧源码/已注册命令/日志扩展区；底部居中“加载脚本”按钮；行点击选中 |
| 会话右键菜单 | 按客户端分组：交互 | 资源管理器 ▸（进程列表/文件浏览器）| 标记为已死亡/存活 | 导出会话 | 复制会话 ID；右键菜单底色改为客户端 MenuStyle `#1a1b1d`/`#26282b`，支持二级菜单 |
| 回归用例 | 右键菜单用例改为“资源管理器 → 文件浏览器”二级菜单路径，仍覆盖 mousedown 不吞点击的回归 |

## 第 3 轮完成（P1 菜单 / 对话框 / 特殊页面）

| 项目 | 结果 |
| --- | --- |
| 菜单结构 | 与 `HavocUi.cc` 逐项对齐：Havoc（新建客户端/断开连接/退出，额外保留重连）｜视图（监听器 / 会话视图▸表格·拓扑图 / 团队聊天 / 战利品 / 事件查看器 / 团队服务器）｜攻击（载荷生成 / 扩展 / 一键上线）｜脚本（脚本管理 / 脚本控制台）｜帮助（关于 / 打开文档 / 打开 API 文档 / GitHub 仓库） |
| 二级菜单 | 新增 `#submenu-popup` 飞出菜单；会话视图 → 表格/拓扑图 切换顶部主视图（与客户端 QStackedWidget 一致），主视图在会话表与拓扑图之间切换，事件查看器保持右侧 |
| 会话图 | 移到顶部主视图；节点改用客户端 Windows/Dead 图标（`osIconPath`）、圆角卡片、曲线边、TEAMSERVER 根节点；节点间距调整为 12px，2233 宽下 7 节点完整可见 |
| 载荷对话框 | 按 `Payload.cc` 重排：550×700（内容 660 左右）、Agent 行、选项 GroupBox（监听器/架构/格式 + 配置项/值树）、进程注入父子（64/32 位注入进程）、构建控制台 190px、底部居中蓝色“生成”；服务名称行按格式显示；注入节点可折叠 |
| 关于 | 合并原团队服务器的连接/统计信息（teamserver、操作员、状态、会话、监听器、重连次数） |

## 第 2 轮完成（P0 控件内容）

| 视图 | 结果 |
| --- | --- |
| 进程列表 | 左 PPID 进程树（`PID: 名称`、20px 缩进、▾ 三角、1px 树枝线、行高 24px）+ 右表（名称 200 / PID 70 / PPID 70 / 会话 70 / 架构 70 / 用户 stretch；名称左对齐、当前进程红色 `#ff5555`）+ 底部居中“刷新” |
| 控制台 | QTextEdit 风格（`#1a1b1d`、1px 边框、圆角 6、`padding 4px 13px`、行高 18、NoWrap）；提示行 `时间 [用户] Demon » 命令`（时间灰、Demon 紫下划线、» 青）；输出行无时间戳；底部状态行 `[User/Computer] process/PID Arch`（右侧弱化 sleep/任务）；输入行 `>>>` + QLineEdit 风格输入框 |
| help | 按客户端 `ConsoleInput.cc` 生成 **Demon Commands** 表：`Command / Type / Description` 列位于 2/27/40，含脚本注册命令（按名称排序） |
| 团队服务器 | 改为 teamserver 日志 QTextEdit（Event=16 Text 原样、自动滚底），连接/profile 卡片移除 |
| 战利品 | 顶部居中 `AgentID：[ All ]`、`显示：[截图/下载]`；截图页 = 左表（名称/日期）+ 右侧大图预览（可拖拽分隔）；下载页 = 名称/大小/日期 |
| 登录页 | 对齐 `Dialogs/Connect`：500×260、左“新建配置”+配置列表、右标题框 + 名称/主机/端口/用户/密码 + 蓝色“连接”；错误提示在对话框下方 |
| 聊天 | `时间 [用户] :: 内容`（用户名下划线、自己绿色、`:: ` 加粗）；`QTextEdit` 风格日志框；底部操作员名 + 输入框 |

## 第 1 轮完成（P0 全局 chrome）

| 项目 | 结果 |
| --- | --- |
| 底部状态栏 | 从 DOM 删除；连接状态/操作员保留在菜单栏右侧 |
| 菜单栏 | 高度 28px（27+1px 边框）、item `padding 5px 10px`、间距 6px、圆角 4px、`#161719`；字符位置与客户端逐个像素一致（Havoc 16–45、视图 72–95…） |
| 标签栏 | 容器 29px（1px 上边框 + 26px tab + 2px 下边框）；选中 `#202224` + 2px `#4c9aff`；`padding 0 14px`；无列间竖线 |
| 分隔条 | 水平/垂直均 1px `#2f3235`；hover 变蓝；双击事件宽度复位 30% |
| 事件查看器 | 默认 30% 宽、可拖拽；标题做成选中的 QTab（26px + 2px 蓝线 + 关闭按钮）；正文 `#1b1d1f`、1px 边框、圆角 6、padding 4px 8px、行高 18px、时间列 138px |
| 会话表 | 10 列等宽、单元格居中、表头 33px/600/#202224/右边框、行高 30px、无行线；ID 提权红色 `#ff5555`；ID 图标左置 + 文本居中（与 Qt 一致） |
| 会话图标 | 使用客户端资源（win10-8/win11/win7-vista/winxp/linux/macos/unknown 的 high 版本 + DeadWhite），先缩放到 32px 再按 16px 显示 |
| 字体 | 全站 `Noto Sans Mono CJK SC` 12px（客户端 9pt Monospace 的实际解析结果；ASCII 步进 6px、CJK 12px） |
| 表头/单元格 | 通用表格（文件/进程/监听器）统一 33px 表头、居中 600、右边框、22px 行高 |
| 文件浏览器 | 左树（文件头 33px + 驱动器/文件夹图标 + 26px 行）+ 3px 分隔 + 右侧“文件夹按钮 + 路径栏 + 等宽三列表格”；文件名/文件夹图标、行高 22px |
| 监听器 | 客户端列顺序（名称/协议/主机/绑定端口/连接端口）、等宽 5 列、表头居中；底部居中按钮 添加/删除/编辑/一键上线命令；一键上线面板默认隐藏、点按钮展开（88% 宽、居中等宽表单） |
| 事件配色 | 时间 `#71777d`；tag 配色；消息按 level 着色（成功绿、告警橙、错误红、聊天紫） |

## 验证

- `python3 ui_feature_test.py` → 15/15（含“右键菜单打开文件浏览器”回归）。
- `python3 acceptance.py` → 15/15。
- `ui_screenshot2.py` 已改为 2233×1289（与客户端同尺寸），截图更新到 `docs/`。

## 下一轮待办

1. 事件查看器细节（剩余）：时间列宽、tag 颜色、滚动条。
6. P2：右键菜单/滚动条/tooltip/空状态；登录页次要按钮 hover 等。

## 第 13 轮完成（9 项 Web/Qt 差距补齐，2026-11-10/11）

| 差距 | 客户端对照 | Web 实现 |
| --- | --- | --- |
| 最后心跳 | `SessionTable.cc:163` 显示 `item.Last`（teamserver 绝对时间） | 单元格显示 `last_call`（缺失 `—`），`tick()` 不再覆盖为相对时间；tooltip 相对时间 + sleep/jitter |
| 战利品下载 | `LootWidget.cc` 菜单项（客户端未接槽函数） | 截图行右键/双击下载；`/api/loot/{id}?download=1` + RFC 5987 中文文件名 |
| 控制台历史 | 每个 `DemonInput` 自带 `CommandHistory` | `S.consoleHistory` 按 Demon 隔离、200 条上限、`localStorage` 50 会话 LRU |
| 拓扑图 | `SessionGraph.cc` 的 +/-/滚轮缩放 | 光标锚点滚轮缩放 0.3–3、背景拖拽平移、`+/-/0`、视图持久化 |
| 移除/退出 | `TeamserverTabSession.cc` Remove / Exit▸Thread/Process | 菜单 + 本地 `hide_session` / `exit thread|process`（仅 Demon、二次确认） |
| 断开连接 | `HavocUi.cc` `Connector->Disconnect()` | `POST /api/teamserver/disconnect` 暂停自动重连，`/api/reconnect` 恢复 |
| 脚本生命周期 | `ScriptManager.cc` 重载/删除、`Store.cc` 安装即加载 | 上传/加载/重载/卸载、`scripts.json` autoload、商店安装自动注册 |
| Python SDK | `Havoc.cc`/`PyDemonClass.cc`/`Event.cc` | 新增全局 API、DllInject/Dotnet/ProcessCreate/ShellcodeSpawn、Event 回调 |
| Python 控制台 | 控制台与脚本共享嵌入式解释器 | 常驻 `pysdk/host.py`：共享全局、invoke、reload、event、崩溃自恢复 |

验证：`gap_features_test.py` **42/42**、`sdk_contract_test.py` **35/35**、`acceptance.py` **15/15**、
`ui_feature_test.py` **15/15**、`command_test.py` **34/34**；截图 2233×1289 见
`docs/gap-*.png`。

## 备注

- 客户端的事件查看器宽度可拖拽；原始状态是约 39%（862px）。对照时已把客户端拖到 30%（≈684px），与 webui 默认值一致。
- 客户端字体由 `QApplication::setFont(QFont("Monospace", 9))` 解析为 `Noto Sans Mono CJK SC`（fontconfig），故 webui 使用同名字体。
- 静态资源带 `Cache-Control: no-cache`；浏览器查看请 Ctrl+F5。
