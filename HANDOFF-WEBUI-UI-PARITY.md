# 交接提示词：Havoc Web 控制台 —— UI 高保真复刻 Qt 客户端

> 适用对象：下一个会话的 AI/开发者。工作目录 `/home/kali/桌面/Havoc`。
> 只用于已获得明确书面授权的测试环境。**不要修改 Qt 客户端 / teamserver / 9901 投递链**，只改 `webui/`。

---

## 0. 一句话目标

当前 Web 控制台功能已齐全，但**界面与 Qt 客户端仍有明显差距**。本阶段目标：
把 `webui/` 从“结构相似”提升到“**像素级接近** Qt 客户端（hexhub 深色主题）：
菜单栏、会话表、右侧事件查看器、底部 dock、每个标签内容、图标、间距、字体、QSS 全部对齐；
同时不破坏已实现的载荷生成 / 会话图 / 扩展商店 / Python 脚本与已有测试。

---

## 1. 环境现状（已验证）

- 主机 Kali `192.168.121.132`；目标 Windows `192.168.121.1`，当前 **7 个 Demon 在线**。
- 运行中：
  - teamserver：`wss://0.0.0.0:40056`，HTTP 监听 8080，pid 约 456528
  - Qt 客户端：`./client/Havoc`，pid 约 456684，操作员 `5pider`
  - stager：9901，pid 442291
  - webui 网关：`:8081`，pid 见 `webui/run/webui.pid`，操作员 `Neo`
- Web 账号：`admin` / `havoc1234`（`webui/config.json`）
- 监听器：`LAN-Test` → `http://0.0.0.0:8080`，Hosts `[192.168.121.132]`
- 浏览器访问 `http://192.168.121.132:8081/`；静态资源已 `Cache-Control: no-cache`，改动后 Ctrl+F5。
- 不要重启 teamserver / Qt 客户端（会中断 7 个会话）；webui 重启用 `webui/start-webui.sh` / `stop-webui.sh`。

启动 / 停止：

```bash
cd /home/kali/桌面/Havoc/webui
./start-webui.sh
./stop-webui.sh
```

验收命令（当前应为全绿）：

```bash
cd /home/kali/桌面/Havoc/webui
python3 acceptance.py        # 15/15：登录/会话/命令/审计/重连/9901
python3 ui_feature_test.py   # 15/15：聊天/商店/脚本/Python/图/载荷/控制台/文件/进程
```

---

## 2. 已实现能力（不要重做，避免回归）

- 登录（HMAC cookie）、网关自动重连、浏览器自动重连。
- 会话表 + 心跳/健康度、交互控制台（shell/powershell/fs/ps/sleep/screenshot…）。
- 事件查看器、团队聊天、文件浏览器、进程列表、监听器增删改、战利品截图、团队服务器。
- **载荷生成**：`Event=5 Gate Stageless` → teamserver 本地交叉编译 → 下载；实测 99,328B PE32+。
- **会话图**：TEAMSERVER 根 + 会话/枢轴树（SVG）。
- **扩展商店**：havoc-store 目录（19 项）、gist/GitHub 仓库安装、镜像与本地回退。
- **Python 脚本 / Python 控制台**：`pysdk/havoc.py`、`RegisterCommand` 注册命令可由控制台调用、
  `InlineExecute` / BOF 回调、持久 REPL。
- 最近修复：右键菜单项被全局 `mousedown` 隐藏导致 click 不触发；已在 `static/app.js`
  的 `handleInteractions()` 修复，并加了回归测试“右键菜单打开文件浏览器”。**不要改回去**。

---

## 3. 当前 UI 与客户端的差距（按优先级）

先读这些源码/样式，所有尺寸、颜色以客户端 QSS/资源为准：

- `client/data/stylesheets/Havoc.qss`（全局颜色、按钮、表格、输入框、滚动条、splitter）
- `client/data/stylesheets/menubar.qss`（菜单栏 30px、item padding 5px 10px、圆角 4px）
- `client/data/stylesheets/teamserverTab.qss`（**tab 高 26px、padding 0 14px、选中背景 #202224、底部 2px #4c9aff**）
- `client/data/stylesheets/bottomTab.qss`（tab 关闭按钮）
- `client/data/resources/*.png`（win10/11/7/xp/linux/macos 图标、Havoc 图标）
- `client/data/img/**`（FileBrowser 文件夹/文件/磁盘图标、treewidget 分支、DeadWhite 等）
- `client/data/Havoc.qrc`（资源别名映射）
- 控件实现：
  - `SessionTable.cc`：10 列、单元格居中、ID 红色 `#ff5555`、WinVersionIcon 图标、健康度文本
  - `FileBrowser.cc`：**左树 + 右列表**、路径输入 + 向上按钮、列“名称/大小/修改时间”
  - `ProcessList.cc`：**左进程树 + 右表**、底部 Refresh、右键结束/复制 PID
  - `LootWidget.cc`：**AgentID 下拉 + 类型下拉（截图/下载）+ 大图预览 + 表格**
  - `Teamserver.cc`：**只是一个日志 QTextEdit**（不是信息卡片）
  - `Chat.cc`：`[user] :: text` 格式、底部输入行、左侧用户名标签
  - `ListenersTable.cc`：列“名称/协议/主机/绑定端口/连接端口”，按钮在底部居中
  - `Payload.cc`：Agent 行 + Options 组 + Config 名称/值树（Injection 父子）+ Building Console + 底部 Generate
  - `Store.cc` / `ScriptManager.cc` / `SessionGraph.cc`：商店/脚本/图形视图布局

### P0 全局 chrome（先做，收益最大）

| # | 差距 | 现状 | 对齐目标 |
|---|------|------|----------|
| 1 | 底部状态栏 | 有 24px 状态栏 | 客户端没有；删除或默认隐藏，把连接信息放到菜单栏右侧 |
| 2 | Tab 栏 | 30px、选中背景 `#1a1b1d` | 26px、`padding 0 14px`、选中 `#202224`、底部 2px `#4c9aff` |
| 3 | 菜单栏 | 30px、item padding 4px 9px | 按 menubar.qss：item `5px 10px`、圆角 4px、背景 `#161719` |
| 4 | 分隔条 | 4px 深色 | 客户端 `QSplitter::handle` 1px `#2f3235` |
| 5 | 事件查看器宽度 | 固定 340px（21%） | 客户端约 30% 宽度（参考 2233 宽截图中约 660px），可拖拽 |
| 6 | 表格 | 左对齐、行高 26、表头 11px/500 | 单元格居中、表头 600、padding 5px 8px、border-right、gridline `#26282b`、item padding 3px 6px |
| 7 | 会话表图标 | 纯色方块 | 用客户端 `win10-8-icon-high` / `win11-icon-high` / `win7-vista-icon-high`（提权）与 `DeadWhite`（死亡），复制到 `webui/static/img/` |
| 8 | 字体 | 12px 系统字体 | 客户端 9pt ≈ 12px，等宽用 Monaco/DejaVu Sans Mono；核对行距/字重 |
| 9 | 登录页 | 自绘卡片 | 对齐客户端 Connect 对话框（`Connect.cc` + `Dialogs/Connect.qss`；旧截图 `logs/connect-dialog.png`） |

### P0 控件内容

10. **控制台**
    - `help` 输出按客户端显示 **Demon Commands 表**（Command / Type / Description 三列，等宽对齐），而不是普通段落。
    - 底部状态行对齐：`[User/Computer] process:/PID Arch`（客户端没有 sleep/任务号；如需保留可放右侧弱化）。
    - 控制台背景/边框/内边距按 QSS（`QTextEdit` padding 4px 8px、边框 1px `#2f3235`、圆角 6px）。
11. **文件浏览器**：改为左树 + 右列表；驱动器/文件夹图标；路径栏 + 向上按钮；右键下载/复制路径。
12. **进程列表**：左 PPID 进程树 + 右表；底部刷新；当前进程红色；右键结束/复制 PID。
13. **战利品**：顶部 AgentID 下拉 + 类型下拉（截图/下载）+ 中间大图预览 + 表格；保留缩略图。
14. **团队服务器**：改为 teamserver 日志文本视图（`Event=16` 的 Text），自动滚底；连接/profile 信息移到“关于”或菜单栏。
15. **监听器**：按客户端列顺序与底部居中按钮；添加/编辑对话框补齐字段分组与校验；一键上线面板保留（额外功能）。

### P1 菜单 / 对话框 / 特殊页面

16. **菜单结构对齐客户端**：
    - 视图：监听器、会话视图（会话表格/会话图）、团队聊天、战利品、日志、团队服务器
    - 攻击：载荷生成、扩展商店
    - 脚本：加载脚本、Python 控制台
    - 帮助：关于、帮助文档、API 参考、GitHub
    - “一键上线”作为额外项放在攻击或监听器内。
17. **载荷对话框**：按 `Payload.cc` 重排（Agent 行、Options GroupBox、Listener/Arch/Format、
    Config 名称/值树含 Injection 父子、Building Console 190px、底部居中 Generate）；尺寸 550×660 左右。
18. **扩展商店**：左表 + 右详情 + 底部日志；商店目录/分类/已安装状态对齐。
19. **脚本页**：对齐 `ScriptManager.cc`（脚本列表 + 编辑器 + 控制台/日志）；Python 控制台保留。
20. **会话图**：节点用 Windows 图标、圆角卡片、曲线边、根节点；点击/右键；视觉参考 `SessionGraph.cc`。
21. **聊天**：`[user] :: text` 格式（时间前缀弱化、用户名下划线、自己绿色）；底部输入行含用户名标签。
22. **事件查看器**：时间列宽、tag 颜色、是否换行按客户端；滚动条样式。

### P2 细节

- 弹窗/右键菜单/滚动条/tooltip 尺寸与颜色。
- 会话表右键“颜色标记”与客户端菜单项（如已实现）。
- 空状态、错误提示、加载状态的文案与位置。

---

## 4. 推荐工作流（必须先建立对照）

1. **抓客户端参考图**（原生 2233×1289）：
   ```bash
   export DISPLAY=:0
   xdotool search --name '^Havoc$'        # 取窗口 id
   xdotool getwindowgeometry <id>
   import -window <id> logs/client-shots/ref-main.png
   ```
   用 `xdotool mousemove --window <id> X Y click 1` 打开底部 tab/菜单；
   每次点击后先截图确认，再继续（坐标会随 tab 宽度变化）。
   需要覆盖：会话表+事件查看器、控制台（help 表）、文件浏览器、进程列表、监听器、
   战利品、团队服务器、聊天、载荷对话框、商店、脚本页、会话图、菜单展开。
2. **抓 Web 对照图**：用 CDP（`webui/ui_screenshot2.py` 已封装）在 **相同宽高比** 下截图，
   建议 2233×1289 或 1600×925；把两边拼图。
3. 写 `webui/tools/ui_compare.py`：调用上面的抓图逻辑，输出 side-by-side + 像素差（Pillow），
   作为每轮迭代的量化反馈。
4. **先改全局 chrome**（状态栏/tab/表格/splitter/事件宽度/图标），再逐控件重排；
   每改一个视图就截图对比，不要一次全改。
5. 每轮结束跑：
   ```bash
   python3 ui_feature_test.py && python3 acceptance.py
   ```
   并用 `ui_screenshot2.py` 更新 `webui/docs/`。
6. 更新 `webui/README.md` 截图；`README-DEPLOY.md` 第 14 节保持同步。

---

## 5. 代码入口

```
webui/static/index.html      # 结构：menubar / main-split / bottom-dock / statusbar / modal
webui/static/style.css       # 所有尺寸/颜色；按客户端 QSS 改
webui/static/app.js          # renderSessions / renderTabbar / renderConsolePane /
                             # renderFilesPane / renderProcsPane / renderListenersPane /
                             # renderLootPane / renderTeamserverPane / renderPayloadDialog /
                             # renderStorePane / renderScriptsPane / renderPyConsolePane / renderGraphPane
webui/gateway.py             # REST/WS；UI 改动一般不需要动后端
webui/state.py               # 数据字段；如需补 session 字段（OS 图标、pivot 等）在这里加
```

现有截图：

- 客户端：`logs/client-shots/client-main.png`、`client-listeners4.png`
- 旧版参考（主题较老，仅布局参考）：`logs/payload-dialog.png`、`logs/listeners-tab.png`、`logs/havoc-connected.png`
- Web：`webui/docs/*.png`、`logs/webui-shots2/*.png`

> 注意：`logs/payload-dialog.png`/`listeners-tab.png` 是 hexhub 主题之前的截图，
> 颜色以当前客户端 QSS 为准，布局可以借鉴。

---

## 6. 坑与约束

- **右键菜单回归**：全局 mousedown 隐藏菜单时不能包含 `#context-menu` 内部点击；
  否则“文件浏览器/进程列表”等菜单项点击无效。已有回归用例，改动后必须保留。
- 浏览器缓存：静态资源 `no-cache`；让用户 Ctrl+F5；截图脚本要 `ignoreCache`。
- 不要重启 teamserver / Qt 客户端；webui 重启用脚本，cookie 会保留。
- 不要删除 `webui/data/{loot,payloads,extensions}` 与 `python_commands.json`（是功能数据）。
- 载荷构建一次只能一个（teamserver 限制），一次约 60 秒，测试时避免阻塞。
- Python 脚本与网关同权限，仅加载可信脚本；`python.enabled=false` 可关闭。
- 端口 8081 不要暴露到办公网/公网。
- 客户端界面用 xdotool 操作时，先 `import` 确认当前 tab 状态，避免点错关闭 tab。

---

## 7. 本阶段完成定义（DoD）

1. 以下视图与客户端 2233×1289 截图并排对比，布局/配色/字号/间距无肉眼明显差异：
   会话表+事件查看器、控制台、文件浏览器、进程列表、监听器、战利品、团队服务器、
   聊天、载荷对话框、扩展商店、脚本页、会话图、菜单展开。
2. 菜单项与客户端一一对应（额外项可保留但位置合理）。
3. 文件浏览器有左树，进程列表有左树，战利品有筛选+预览，团队服务器为日志视图。
4. 会话表使用客户端 Windows/死亡图标、单元格居中、ID 红色。
5. 底部状态栏已移除/隐藏；tab 栏 26px、选中 `#202224` + 2px `#4c9aff`；splitter 1px。
6. `acceptance.py` 15/15、`ui_feature_test.py` 15/15 全过；右键菜单回归用例通过。
7. 文档与截图更新，用户 Ctrl+F5 后看到新界面。

---

## 8. 建议的第一条动作（给下一个会话）

```
1) 先读本文件 + client/data/stylesheets/{Havoc,menubar,teamserverTab}.qss + SessionTable.cc。
2) 用 xdotool/import 抓当前客户端 4 张参考图：主界面、文件浏览器、进程列表、监听器。
3) 用 ui_screenshot2.py 抓同尺寸 Web 图，做 side-by-side。
4) 只改 P0 全局 chrome（状态栏/tab/表格/图标/事件宽度/splitter），截图对比后再进入控件重排。
5) 每轮跑 ui_feature_test.py + acceptance.py，防回归。
```
