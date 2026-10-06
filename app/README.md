# 灵桥 v3.5 使用与维护

灵桥聚合 Claude Code、Codex、ZCode、WorkBuddy 的本地文字会话。安装见仓库根目录的 README；双击 `会话桥.app` 启动。关闭主窗口隐藏到菜单栏；面板退出按钮结束实例。再次双击会唤醒已验证的现有实例。

## 浏览和同步

- 四工具计数包括当前扫描范围；归档和子代理单独标为仅浏览。搜索匹配标题、目录，不是全文搜索。
- 点开分批读取文字，每批最多100条；附件和工具执行事件仍由原工具查看。
- 12方向有转换实现；目标数据库或桌面账号不可用时按钮禁用。手动同步默认限制最近3天活跃会话，可在 `.bridge/config.json` 设置正整数 `sync_window_days`。
- 同一来源到同一目标重复同步不再新增。来源关系独立保存于 `.bridge/provenance.json`；删除来源也不会把遗留副本重新当原始会话。
- 迁移副本默认采用正常/受限权限，不把历史内容当成用户授权执行。查看可见性与在目标工具继续运行需分别验收。

## 删除与恢复

删除前将会话文件、涉及的数据库行、关联外键记录、索引与来源关系保存到 `.bridge/operations/<操作ID>/`，校验后再删除。失败时尝试补偿；遇到并发变化/冲突则保留材料并停止覆盖。

左侧回收站可恢复新版删除。恢复数据库身份、结构、文件哈希及ID冲突均会检查；Codex索引按ID合并，不覆盖删除之后的新索引。旧 `.bridge/trash` 材料继续保留，缺少数据库备份的条目显示不可自动完整恢复。

启动时检查中断日志并补偿；遇到不确定的半写状态显示在回收站，暂停新写入等待复核。不要用老快照覆盖当前整库，可能丢失快照后的真实会话。

## 闲置清理

会话列表中的「清理闲置会话」支持超过15天或30天没有活动的会话，可选择全部或单个工具，并选择「清理对象」：主对话（闲置会话）或子代理对话（主对话保留）。点击「预览清单」后逐条勾选要清理的会话，也可全选或取消全选。默认不选，实际选中的数量与大小会同步显示；点击「确认移入回收站」只处理勾选项。预览有效期5分钟；重新选择时间或范围后需要重新预览。

闲置时间同时核对会话文件及可识别的官方数据库活动记录，不只使用文件修改时间。正在运行、状态或时间不明、归档、子代理、同步副本和受保护任务会跳过。清理前和备份后再次核验；期间出现新活动的会话会跳过。超过1000个候选时需按工具分批处理。

每条实际清理都会保留新版恢复材料，可在左侧回收站恢复。恢复遇到已有同名文件、会话ID或数据变化时会停止覆盖。中断后不自动继续批量清理，需要重新预览。主对话不会被定时自动删除。

### 子代理对话

「清理对象」选「子代理对话（主对话保留）」时，只收这三种：

- ZCode 的子会话：`session.parent_id` 不为空；
- Codex 的子代理线程：`session_meta.source` 里有 `subagent`；
- Claude Code 的子代理记录：`<项目>/<主对话>/subagents/*.jsonl`。

预览清单里每条都写"属于：哪个主对话"。核验规则、备份、移入回收站、恢复都和主对话清理一样。ZCode 子会话连带的消息、片段、用量、任务关联一起备份、一起删；`workflow_activity` 等指向它的记录按外键置空，恢复时还原。主对话里点开对应子任务的详情会显示找不到，从回收站恢复后就回来了。

移入回收站不会马上省出磁盘空间：回收站要留备份才能恢复，ZCode 的数据库文件也不会自己缩小。

选子代理对话时，窗口里会出现定时清理开关，**默认关**。打开后，灵桥启动 3 分钟后开始检查，之后大约每 20 小时按所选天数（15 或 30）清一次，规则和手动完全一样：预览、逐条核验、备份、移入回收站。开关和上次结果在 `.bridge/subagent-cleanup.json`（0600）。有中断操作待复核时不跑。

## Claude Code 双账号会话

在侧栏 Claude Code 下面。Claude 桌面版的侧栏只显示当前账号目录 `~/Library/Application Support/Claude/claude-code-sessions/<账号>/<组织>/local_*.json` 里的会话；聊天记录本体在 `~/.claude/projects/`，不分账号。这个页面把另一个账号的侧栏条目复制过来。

- **预览（只读）**：随时能看。按 cliSessionId 去重，分成新复制、刷新标题、已在、跳过、不在时间范围。跳过的原因有：聊天记录不在本机、目标账号删过（不复活）、没有会话 ID、目标里有同名文件。
- **同步**：
  - Claude 桌面版主进程和它的 Helper 都退出后才能写，写前写后各查一次；
  - 每次写之前把整个 `claude-code-sessions` 备份一份，逐个文件核对 SHA，旁边放一份 `灵桥同步记录.json`。备份放 `claude_accounts.backup_dir` 指定的目录（比如外接硬盘上的 `<目录>/Claude会话迁移备份_<时间>/`）；没指定，或者指定的目录不在，就放 `.bridge/account-sync/backups/`；
  - 新文件用 link 落地，不覆盖已有文件，权限 0600；`bridgeSessionIds` 置空，`remoteControlAutoEligible` 置 false；
  - 标题只在来源那边更新过时才刷新。
- **撤销**：把这次复制进去、之后没被桌面版动过的文件挪到 `.bridge/account-sync/undo/`（不删除），标题改回原样。桌面版动过的条目保留不动。
- **不做**：不切换登录账号，不读 Claude 的 `config.json`、Cookies、Local Storage，不改聊天记录。
- **配置**：`claude_accounts.labels` 写账号目录到名字的对照，`backup_dir` 写备份放哪。
- **接口**：GET `/api/accounts/status`、`claude`、`runs`、`run?id=`；POST `/api/accounts/preview`、`sync`（要带 `confirm:true`）、`undo`、`open-claude`。

## 会话导出 / 导入

在两台电脑的灵桥之间搬会话。入口都在会话列表里：每条会话旁边的「导出」；工具栏「清理闲置会话」旁边的「批量导出」（列的是当前分组和筛选下的会话）和「导入会话」。

- **导出**：选好保存位置（本机的“存储”对话框，默认“下载”），打成一个 .zip（0600）。每条会话里有两样：
  - 原始材料：Claude Code 的聊天记录、会话文件夹（工具输出、子代理记录、自定义标题）和桌面版侧栏条目；Codex 的 rollout 文件、索引行和两个库里属于这条会话的记录；ZCode、WorkBuddy 库里属于这条会话的记录；
  - 整理好的文字对话（只有人和 AI 的正文）。
  - 正在写的会话，最后没写完的半行不带。导出结果会提示里面有几处“像密钥”的内容（只数个数，不记录内容）。
  - ZCode 的附件 / 产物文件夹（`~/.zcode/cli/artifacts/<会话>`）、Claude 的文件改动历史不带。
- **导入**：选压缩包（本机的“打开”对话框，或“下载”“桌面”里最近的灵桥导出包），先只读预览，逐条显示怎么导：
  - **能原样就原样**：ID 不变。Claude 聊天记录按 Claude Code 自己的规则放进 `~/.claude/projects/<目录名>/`，侧栏条目放进最近在用的账号；Codex、ZCode、WorkBuddy 的库记录要表结构逐字一致才原样插入，rollout 路径、WorkBuddy 账号、Codex 账号换成这台的；
  - **不行就转文字**：表结构不同、关联的主记录这台没有、这台没装那个工具时，用文字对话新建一条（新 ID），可以选导进哪个工具；
  - 这台已经有同一条会话、或之前已经转文字导入过的，跳过，不覆盖；
  - 工作目录在导出那台的用户目录下时，换成这台的用户目录。
- **写入保护**：每条一个操作日志（`.bridge/operations`，action=import），先记下要建的文件和库，文件先写临时文件、校验 SHA、再用 link 落地；出错当场撤回，灵桥中途被关掉的话下次启动按日志补偿。往 Claude 桌面版侧栏写条目前，桌面版必须完全退出。
- **撤销**：导入结果里或「最近的导出和导入」里点撤销：导入的会话进回收站（可以恢复），Claude 会话文件夹里带过来的文件挪到 `.bridge/transfer/undo/`。
- **安全**：接口不收文件路径，选好的文件在服务端换成一次性编号；压缩包当外来数据：清单逐项校验，只读清单上列出的条目，大小和 SHA-256 都要对上；库记录里每一行都必须属于这条会话；写入位置一律在这台电脑上重新算。
- **记录**：`.bridge/transfer/runs/`（导出、导入各一条）、`imported.json`（防止重复导入）、`machine.json`（这台电脑的随机编号）。
- **配置**（可选）：`.bridge/config.json` 的 `transfer.export_dir`（默认“下载”）、`transfer.import_dirs`（导入时额外去找压缩包的文件夹）。
- **接口**：GET `/api/transfer/status`、`candidates`、`job?id=`、`runs`、`claude`；POST `/api/transfer/export`、`inspect`、`import`（要带 `confirm:true`）、`undo`（要带 `confirm:true`）、`discard`、`reveal`。
- 两台电脑的灵桥都要是 3.4 或更新的版本才能互相导。

## 视觉与品牌图标

会话列表、用量页和菜单栏面板共享五套皮肤：樱桃奶油、海盐蓝、抹茶薄荷、薰衣草、奶油白。右上「外观」提供圆润可爱（华文圆体）、清爽现代、手写趣味（楷体）和系统稳重四种字体，设置保存在本机。

工具图标不随代码分发：安装时（`install.sh`）和每次启动时，`brand_icons.py` 用 macOS 自带的 `sips` 从本机已装的官方 App（Claude、Codex、ZCode、WorkBuddy、Kimi、CC Switch 等）里提取 64×64 的图标，放在 `app/assets/icons/`（不进 Git）。没装的工具、只有网站的服务（GLM、MiniMax、Gemini）显示字母徽标。

切换皮肤会同步更新主页面图表、菜单栏主按钮和额度环图。

## 用量采集

用量页用灵桥内置的采集器 `usage_collector.py`：智谱 GLM、Kimi Coding、MiniMax 的套餐额度接口，以及 ZCode、CC Switch、WorkBuddy、Claude Code 的本地用量，按日、按模型、按小时统计，带成本和热力图。这部分最早是作者自己的菜单栏小工具 Mtoken 的数据层，现在完全在灵桥里，不需要 Mtoken；以前用过 Mtoken 的，第一次启动时会把旧缓存搬过来。

快照位于 `~/Library/Application Support/LingqiaoUsage/cache.json`，目录权限为 0700、文件为 0600，采用临时文件加 fsync 和原子替换；缓存损坏时只提示重新采集，不覆盖原文件。额度缓存 5 分钟、本机用量缓存 30 分钟；灵桥后台每 5 分钟检查，跨日立即重新统计。界面显示采集器写入的 `at` 时间，采集失败保留上一份有效快照。

供应商密钥存在 macOS 钥匙串里（服务名前缀 `aiquota-`），通过 Security 原生 API 读写，只在内存中传递，不写进快照、日志或命令行。后台读取不弹隐藏授权窗口；钥匙串受保护或锁定时显示读取错误。在用量页「额度设置」里填写。

## 插件

`plugins/<文件夹>/` 里放 `plugin.json` 和一个 Python 模块，灵桥启动时加载，侧栏多出一项。格式、接口和页面约定见 [../plugins/README.md](../plugins/README.md)。

- 加载：清单逐项校验（id、文件名、版本、要求的最低灵桥版本），模块名不能和灵桥或 Python 自带的重名；插件文件夹加在 `sys.path` 的最后。
- 出错隔离：清单不对、导入或创建出错的插件只记原因，侧栏显示成灰色，其他功能照常。
- 接口：`/api/<id>/...` 转给插件的 `handle_get` / `handle_post`，和灵桥自己的接口一样要 token、核对 Host 和来源；`/plugins/<id>/<文件>` 只提供 `assets` 里登记过的 .js / .css；`GET /api/plugins` 列出已装的插件和状态。
- 插件是独立的 Git 仓库时，「检查更新」会连它一起更新，并跑它 `tests/` 里的测试。

## 检查更新

侧栏底部「检查更新」。灵桥文件夹和插件文件夹是 `git clone` 下来的才能用。

- 检查：对每个仓库 `git fetch`，算出落后几处、本机有没有改过程序文件、有没有还没上传的提交，列出新版本号和改动说明。只下载版本信息，不改文件。默认每天自动看一次，`.bridge/config.json` 里写 `"update": {"auto_check": false}` 可以关掉。
- 更新：只做快进（`git merge --ff-only`）。本机改过程序文件、或者两边都改过，停下来说明原因，交给人处理。灵桥正在同步、删除、清理、导出导入或插件有任务时不更新；更新期间占着操作锁。依赖清单变了会重跑 `repair-runtime.sh`。
- 测试：更新后跑一遍灵桥和插件的测试，全过才算成功；没过就 `git reset --keep` 退回原来的版本（依赖也按原清单装回去），失败时最后几行测试输出显示在弹窗里。
- 重启：更新成功后点「现在重启」，灵桥退出并自动重新打开。
- 记录：`.bridge/update/`（上次检查时间、最近 30 次更新结果）。git 报错里的账号口令会先去掉再显示。
- 接口：GET `/api/update/status`、`job`；POST `/api/update/check`、`apply`（要带 `confirm:true` 和检查时看到的版本）、`restart`。

## 安全和状态

本机HTTP服务默认127.0.0.1:8791，每实例私有token；读写API都鉴权并校验Host/Origin，响应不缓存。实例token只存在0600的 `.bridge/instance.json` 和应用内存。不会将token写日志。

默认文件权限0600、目录0700。操作恢复材料保留至用户明确清理；缓存过期可重建，恢复材料不可盲删。日志只记录操作ID/步骤/错误类型，不记录正文或密钥，2MiB轮转。

## 维护与验证

- `server.py`：后台/窗口/扫描协调。
- `session_reader.py`：只读扫描、精准标题、流式解析与分页。
- `bridge_ops.py`：可恢复操作、数据库备份、失败补偿。
- `http_api.py`：接口与实例保护。
- `bulk_cleanup.py`：闲置状态核验、签名预览计划、后台清理和中断处理，包括子代理对话清理和定时开关。
- `plugin_host.py`：插件的发现、校验、加载、接口转发和页面文件。
- `updater.py`、`update.js`、`update.css`：检查更新。
- `brand_icons.py`：从本机已装的 App 提取工具图标，没有就给字母徽标。
- `account_sync.py`、`accounts.js`、`accounts.css`：Claude Code 双账号会话。
- `session_transfer.py`、`transfer.js`、`transfer.css`：会话导出 / 导入；写入用 `bridge_ops.py` 里的 `import_raw_session`、`import_text_session`。
- `usage_collector.py`：内置用量与额度采集器，保留原有四类本地来源和三类额度供应商。
- `usage_backend.py`：私有快照校验、刷新并发控制、供应商状态与钥匙串配置接口。
- `bridge_state.py`：原子私有JSON、跨进程锁、来源/清单协议。
- `sync.py`：各工具会话的文字解析和转换；`python3 app/sync.py --export-only` 只导出，旧互注入入口停用。
- `zcode_inject.py`：写入 ZCode 的工具；`python3 app/zcode_inject.py --db <练习库> --dry-run/--rollback` 的练习库状态按数据库身份隔离，回滚精确匹配清单并保留可恢复删除日志。

`.bridge/` 文件夹只放这台电脑的数据（不进 Git），各文件的作用见 [../docs/数据层说明.md](../docs/数据层说明.md)。

执行 `.bridge/runtime/bin/python3 -B -m unittest discover -s tests`。测试只用虚构内容、临时文件夹和四个工具真实表结构的空副本（`tests/fixtures/*.sql`），不修改真实工具库；页面测试用 Node.js 跑，没装 Node 会跳过。

环境隔离：`BRIDGE_HOME` 指向测试家目录，`BRIDGE_REPO` 指向测试源码根，`BRIDGE_PORT` 改测试端口。生产启动器会寻找本机可导入webview/AppKit的Python；依赖丢失时写startup-error.log并提示，不擅自安装包。

兼容性：写入前按当前目标schema检查必要列，恢复再核对原schema及数据库身份；未知结构停止写入。2026-10-02通过本机schema空副本的12方向/恢复测试。目标工具升级后应重跑隔离验收，不能把逆向结构当永远不变的协议。

## 版本

- 3.6.1：修复同步到 Codex 的会话在新版 Codex（0.160 起）打不开（"does not start with session metadata"）。会话第一行的格式字段改为照着本机 Codex 最近写的会话来写；装了 Codex 的电脑上，测试会请 Codex 自带的检查工具确认。
- 3.6.0：支持 Windows（`install.bat`、凭据管理器、跨平台文件锁），GitHub Actions 在 macOS 和 Windows 上各跑一遍测试。
- 3.5.0：拆出插件位（`plugins/`），加检查更新、本机提取图标；双账号备份目录改成可设置，默认放灵桥本地。
- 3.4.0：会话导出 / 导入。
- 3.3.0：Claude Code 双账号会话、子代理对话清理和定时开关。
- 3.2.0：逐条选择清理、标题栏随皮肤变色、用量采集内置、额度设置。
- 3.1.0：15 / 30 天闲置清理、五套皮肤和四种字体。
