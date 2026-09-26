# one_search 安装、设置与接入

用户和 Agent 的主安装合同在 [README 的安装与接入](../README.md#安装与接入给用户和-agent-的执行入口)，包括同版本包选择、校验、参数、状态含义和失败恢复。本文补充各平台维护细节。v0.4 的模型准备已经独立于基础服务启动；下文使用 v0.5 包名，下载时保持版本一致。

本版先交付单机服务。每台机器使用独立 `node_id`，协议保留节点标识；远程认证、传输与跨机汇总尚未实现。新安装默认发现本机文件范围，传入目录参数可限制范围；重装保留现有设置。

## 选择发行形式

| 形式 | 文件或入口 | 前提 |
|---|---|---|
| Windows 原生运行时包 | `one-search-0.5.0-windows-amd64-native.zip` | 64 位 Windows；随包包含 CPython 和应用依赖，无需另装 Python |
| Windows Python bootstrap 包 | `one-search-0.5.0-windows-amd64-py311-bootstrap.zip` | 已安装 CPython **3.11 x64**、venv/pip；包含匹配的依赖 wheel |
| 源码 | 仓库中的 `scripts/install.ps1` / `scripts/install.sh` | Python 3.11+、venv/pip；安装依赖需要网络或匹配 wheelhouse |
| 项目 wheel | `data_search-0.5.0-py3-none-any.whl` | Python 环境；依赖另行安装，项目 wheel 本身不是免 Python 程序 |

本轮发行目标为 Windows amd64，实测主机是 Windows 11、内核 10.0.22631；其他 Windows 版本/ARM64 不作为已验收平台。Linux 已在 Ubuntu 24.04 CI 完成无界面、手动启动模式的安装、重装、检索、迁移和卸载；尚无 Linux 免 Python 二进制，systemd 用户服务与目标服务器仍待验收。源码最低 Python 版本与 bootstrap 包的固定小版本要求不同，安装器会检查包的 `RELEASE_MANIFEST.json` 并拒绝不匹配解释器。

最终发行目录为 `dist/release-v0.5.0`。每个 ZIP 有对应 `.manifest.json`，目录另含 `SHA256SUMS.txt`；解压包内有逐文件校验 `SHA256SUMS.json`。源码哈希在发行清单的 `source_sha256` 中。具体构建、安装和校验结果见 [验证报告](VALIDATION.md)。

模型不在默认 ZIP 内。首次安装先启动基础检索，再启动后台任务准备固定版本本地 embedding 模型；可以指定离线模型目录，在后台校验，或先关闭语义功能。推理始终在本机。网络失败只改变模型任务状态，安装不等待下载完成。

## Windows 安装

安装并接入 DSH Web 后，侧栏 **one_search** 是设置与索引进度入口。后台与 DSH bundle 均需 0.5.0；升级后台后请用新包重新注册 bundle，并重启对应 profile。详见 [Web 面板说明](DSH-WEB.md)。

完整解压 ZIP，在解压目录运行：

```powershell
.\scripts\install.ps1
```

原生包还可双击 `Install.cmd`。安装器识别包类型，原生包校验并暂存随附运行时；bootstrap 包建立隔离 venv 并使用随包 wheelhouse。随后初始化配置、注册当前用户登录启动项、隐藏启动服务、生成 MCP 配置，再启动独立模型任务。最后用真实文件名查询验证基础服务，把结构化结果保存到 `<DataDir>/install-result.json`。原生分支不调用系统 Python。

默认目录：

- 程序：`%LOCALAPPDATA%\data-search\app`
- 配置、模型、索引：`%LOCALAPPDATA%\data-search\data`
- 配置文件：数据目录下 `config.json`

只检索指定目录，或选择独立安装路径：

```powershell
.\scripts\install.ps1 -Root @('D:\资料', 'D:\项目') `
  -InstallDir 'D:\apps\data-search' -DataDir 'D:\app-data\data-search'
```

安装目录与数据目录须独立且不嵌套，首次安装要求空目录；已有安装需有本程序的管理清单。原始资料应放在这两个目录之外，安装目录、索引与模型目录会被排除。

bootstrap 指定解释器示例：

```powershell
.\scripts\install.ps1 -Python 'C:\Python311\python.exe'
```

安装器不修改全局 PowerShell 执行策略。如果本机策略禁止运行脚本，应按设备/组织政策审阅并允许运行本地脚本。自启动使用当前用户 Run 项和隐藏启动脚本，不需要管理员权限；它不是开机即运行的系统服务。Windows Script Host 或 Run 项被策略禁用时可使用 `-NoAutostart`，以后手工启动。

隔离试用或验收应显式指定合成资料范围：

```powershell
.\scripts\install.ps1 -Root 'D:\samples' `
  -InstallDir 'D:\test-app' -DataDir 'D:\test-data' -SkipModel -NoAutostart
```

`-SkipModel` 在首次配置中关闭 `semantic.enabled`，关键词和文件名仍可工作；重装不会替换原语义开关。`-NoAutostart` 省去登录启动项，但安装末尾仍会启动一次服务。

## 设置窗口与范围

原生安装后双击 `<InstallDir>\Settings.vbs`，也可执行：

```powershell
& "$env:LOCALAPPDATA\data-search\app\runtime\data-search.exe" setup `
  --config "$env:LOCALAPPDATA\data-search\data\config.json"
```

源码/bootstrap 环境使用：

```powershell
& "$env:LOCALAPPDATA\data-search\app\venv\Scripts\python.exe" -m data_search.setup_ui `
  --config "$env:LOCALAPPDATA\data-search\data\config.json"
```

`data-search setup` 是原生启动器的入口；Python CLI 使用上面的模块入口。直接运行解压包 `runtime/data-search.exe`（不带参数）也会打开设置窗口，但不会完成安装、自启动注册或 MCP 配置生成。建议先运行安装器。

设置窗口可以：

- 在本机范围和指定目录之间切换；通过“添加目录”选择路径。
- 为正文和语义分别设置 `all` / `directories` / `none`、目录和扩展名；留空扩展名表示不额外筛选。
- 通过表单添加 SQLite/MySQL/PostgreSQL，或编辑 JSON 以配置多个表、TLS 和同步预算。
- 点击“测试数据库”，检查当前待保存的连接及授权结构。
- 在“资源预算”设置进程树内存预算、最低可用内存、worker 内存/CPU 限额、索引与模型磁盘预算、最低空闲空间。
- 保存并启动、刷新状态、暂停/恢复索引、停止服务、下载模型。

数据库测试由用户点击触发，在独立子进程中执行有时限的只读检查：连接、允许字段读取权限、持续索引使用的稳定单列唯一键及非空水位。检查报告不返回数据库正文或密码；默认每个来源最多 8 秒、整次测试最多 30 秒。大表的检查可能超时，此时配置不会激活，应核查必要索引和连接条件。

更改数据库配置后，必须测试同一份待保存内容并全部通过，才能“保存并启动”；编辑内容后需要重新测试。只修改文件范围或预算、保留原数据库配置时不自动联网。保存先验证配置，再停止旧服务、原子保存并启动；启动失败会恢复此前配置并尝试重启原服务。预检通过只证明测试当时和当前进程环境有效，不能保证稍后的网络、权限或登录环境不变。

状态页展示已发现文件/记录、待处理正文、已知语义覆盖、持久化队列、预算限制和来源错误，并保留完整 JSON 详情。首次发现尚未完成时，不把已知计数换算成全机完成百分比。窗口保留未编辑的高级配置；密码仅填环境变量名，没有图形凭据保险箱。CPU 硬限额和 worker 提交内存限额只在 Windows Job 可用时生效，回退原因可在状态中查看。

新安装 `scope: "machine"` 在 Windows 发现当前账号可访问的固定本地磁盘，跳过网络/可移动卷、目录链接、程序/数据/模型目录与排除项，不提升权限。Linux 使用本地挂载发现并排除远程、虚拟文件系统和 `/proc`、`/sys`、`/dev`、`/run`；全机挂载发现仍需目标 Linux 服务器验收。`scope: "directories"` 使用 `roots`。旧配置未写 `scope` 时沿用旧目录范围，升级不会扩大为整机。

正文/语义进一步受 `indexing.content_scope`、`indexing.semantic_scope`、各自的 `*_roots`、`*_extensions` 约束。语义只处理已经提取的正文。数据库正文由自己的 `index` 控制；文件目录/扩展名过滤不限制数据库，`semantic_scope: "none"` 也会关闭数据库嵌入。示例见 [项目说明](../README.md)。

首次文件发现通过持久化队列分批进行，正文和语义随后按预算处理。Windows 可读取当前账号有权限的既有 NTFS USN 日志以加速变更发现；不会创建日志、申请管理员权限或枚举 MFT 来替代首次发现。日志不可用、重置、回卷或路径无法可靠解析时触发核对。指定目录模式最多 32 个有效根时还会尝试监听；周期核对继续兜底。Linux 当前采用周期发现。看 `file_scope`、`coverage`、`scheduler` 和 `database_sync` 判断实际覆盖，不能将“服务正在运行”理解为“所有资料已索引完成”。

## Linux 源码安装

要求 Python 3.11+、pip 和 venv；部分发行版需另装 `python3-venv`。可选图形窗口还需要桌面会话和 Tk/Tkinter（例如发行版提供的 `python3-tk`）。本轮未验收 Linux，下面是实现提供的安装接口。

```bash
bash scripts/install.sh
```

限定目录：

```bash
bash scripts/install.sh --root /srv/docs --root /srv/project \
  --install-dir "$HOME/.local/share/data-search/app" \
  --data-dir "$HOME/.local/share/data-search/data"
```

默认注册 systemd **用户服务**，要求当前用户有可用的 systemd 会话；不可用会明确报错。无 systemd 时：

```bash
bash scripts/install.sh --root /srv/docs --skip-model --no-autostart
```

此模式安装完成仍分离启动后台进程，重新登录/开机后需手工 `start`。安装器不启用 linger、不注册 root 系统服务。默认路径以 `${XDG_DATA_HOME:-$HOME/.local/share}/data-search/{app,data}` 为准；自定义路径按参数配置。systemd 日志可使用 `journalctl --user -u <安装清单中的 unit_name>` 查看。

## 离线模型与构建

模型后台任务：

```powershell
data-search model-status --config CONFIG
data-search model-start --config CONFIG
data-search model-import --source 'D:\verified-offline-model' --config CONFIG
data-search model-cancel --config CONFIG
```

`model-start` 下载到配置中的模型目录；`model-import` 将来源目录按固定 SHA-256 验证并复制到配置中的模型目录。安装器 `-ModelDir` 保留原有外部目录语义，不擅自搬动它。任务状态在数据目录 `model-job/status.json`，包含状态、资产、字节进度、错误代码和恢复建议，不保存下载凭据。完成的资产可在重试时复用；未完成资产重新传输。取消为协作式，正在等待网络时通常需等当前最多 15 秒的网络操作结束。旧同步 `model-download` 保持可用，但安装器不再使用它。

基础就绪不意味着正文或语义就绪。`installation-status --config CONFIG --install-dir APP` 返回运行时、服务、基础查询探测和模型状态；`dsh_connection=not_checked` 需要继续在 DSH 内验证。模型状态 `failed/interrupted` 时先检查错误代码，重新运行 `model-start` 或 `model-import`，无需重装服务或删除索引。

离线模型应由同版本 `data-search model-download --config CONFIG` 准备，包含 `model.onnx`、`tokenizer.json`、`config.json`、`manifest.json`，并通过固定版本校验。不要使用任意聊天模型目录。

```powershell
.\scripts\install.ps1 -ModelDir 'D:\models\bge-small-zh-v1.5'
```

```bash
bash scripts/install.sh --model-dir /srv/models/bge-small-zh-v1.5
```

外部模型目录不会复制或随卸载删除。`ModelDir` 仅在首次配置时设置路径，不能与 `SkipModel` 同用。已有配置按原模型路径运行。Windows 原生包自带程序依赖，bootstrap 包自带匹配的依赖 wheel；配合有效离线模型目录可离线安装。

源码安装使用自备 wheelhouse 示例：

```powershell
.\scripts\install.ps1 -Root 'D:\docs' `
  -PackagePath '.\wheelhouse\data_search-0.5.0-py3-none-any.whl' `
  -Wheelhouse '.\wheelhouse' -ModelDir 'D:\models\bge-small-zh-v1.5'
```

开发者在已安装项目开发依赖的环境构建：

```powershell
python -m pip install -e . --no-deps
python scripts/build_release.py --output dist/release-v0.5.0 --native
```

`--native` 仅接受 64 位 Windows，使用 PyInstaller 生成完整运行时目录，同时生成 bootstrap 包；不传该参数只构建 bootstrap。`--wheelhouse PATH` 可复用构建/依赖 wheel。输出包目录已存在会拒绝覆盖；另选空输出目录。不要混用 Windows/Linux、不同架构或不同 CPython 小版本的原生依赖。当前没有打包 Linux 原生运行时。

## MCP 与 DSH 接入

DeepSeek Harness 使用随包的 `plugins/deepseek-harness` Cordis bundle。按 [README 注册步骤](../README.md#安装与接入给用户和-agent-的执行入口) 准备 `dsh-register.json`，在完整解压的发行目录执行：

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
node ./plugins/deepseek-harness/register.mjs ./dsh-register.json
dsh --profile web
```

注册脚本先把插件按内容校验复制到 DSH 用户目录所在卷，解决 DSH/pnpm 的跨盘 file 依赖问题，再调用官方 `plugin add`；首次启动 profile 才安装缺失的后台服务、连接 MCP。缺少后台版本或版本旧于 0.4 时，需要匹配发行目录来升级；显式配置的外部运行时只报告升级要求。退出 DSH 不会停止后台。插件包注册不依赖 `postinstall`；依赖安装需要 npm 网络或已有 pnpm 缓存。自定义范围、资源档位、离线模型和连接诊断见 [DSH bundle 说明](../plugins/deepseek-harness/README.md)。

其他支持标准 `mcpServers` JSON 的宿主可读取 `<InstallDir>/mcp.json`，把其中 `data-search` 条目加入自己的配置。DSH 的 Cordis 配置不是这种 JSON 容器。安装器生成绝对路径，但不会自行改写未知宿主或 Codex 的全局配置。

原生安装示意：

```json
{
  "mcpServers": {
    "data-search": {
      "command": "C:/Users/you/AppData/Local/data-search/app/runtime/data-search.exe",
      "args": ["mcp", "--config", "C:/Users/you/AppData/Local/data-search/data/config.json"]
    }
  }
}
```

bootstrap 的 `command` 指向 `<InstallDir>/venv/Scripts/data-search.exe`；Linux 指向 `<InstallDir>/venv/bin/data-search`。Codex 格式副本位于 `<InstallDir>/plugin`，`.mcp.json` 同样已绑定绝对路径。源码 `plugins/data-search` 是待安装的外壳，空 MCP 文件不代表已经运行；Codex marketplace 分发未验收。

标准 JSON 宿主也可显式指定配置路径，让辅助模块合并条目。它保留其他设置，原子替换前保存原文件备份；已有同名但不同的条目默认拒绝覆盖，确认要替换时加 `--replace`。例如源码/bootstrap 环境：

```powershell
python -m data_search.host_integration --host-config 'D:\Host\mcp.json' `
  --mcp-config "$env:LOCALAPPDATA\data-search\app\mcp.json"
```

原生包将 `python -m data_search.host_integration` 替换为 `data-search.exe --internal-module data_search.host_integration`。此辅助模块仅支持明确指定的 JSON 文件，不推测 DSH/Codex 配置路径，也不保存新密码。

MCP 使用 stdio，后台 HTTP 仅在 loopback 监听并验证本机 token。远程端口转发不是已支持的多机协议。数据在本机解析和嵌入；返回给宿主的片段后续如何处理，取决于宿主自身部署。

## 服务控制、维护与升级

用上节对应的可执行文件替代下列 `data-search`，`CONFIG` 使用配置绝对路径：

```text
data-search status --config CONFIG
data-search pause --config CONFIG
data-search resume --config CONFIG
data-search scan --config CONFIG
data-search search "服务器成本" --mode hybrid --config CONFIG
data-search stop --config CONFIG
data-search start --config CONFIG
```

`scan` 请求后台调度，不表示扫描同步完成。`status` 包含文件覆盖、源错误、数据库扫描进度、ANN 是否正在构建/等待发布、worker 实际控制及回退原因。

升级时完整解压新包，向相同程序/数据路径重新运行安装器。Windows 原生分支按以下顺序执行：

1. 校验包内运行时的逐文件 SHA-256 和完整文件清单，检查暂存及备份所需空间，将新运行时复制到独立暂存目录。此阶段失败不停止原服务。
2. 停止服务并取得实例锁，复制迁移前数据与配置快照。快照包含 SQLite 及可能存在的 WAL、文件目录/队列、向量缓存和 ANN 分段；不复制通过固定哈希校验的模型文件、锁和临时服务状态/日志。模型目录中的其他文件仍会备份。
3. 保留旧运行时，再启用新运行时，执行安装续步并检查服务健康。配置、数据库授权、预算与原检索范围保持不变。
4. 若本次启动失败，先确认新实例停止且实例锁可用，再恢复旧运行时、配置和索引快照；此前正在运行的服务会尝试重启。若服务仍持锁，拒绝覆盖正在使用的数据，保留恢复材料并明确报错。

每次事务目录为 `<InstallDir>/.upgrade-<ID>`，包括 `transaction.json`、`activation.log`、`data-snapshot`、`app-snapshot` 及适用时的 `previous-runtime`。成功后也保留这些材料，不自动清理。升级前应预留约“当前索引与配置大小 + 新运行时大小 + 64 MiB”的额外空间；已有备份另占空间，文件复制及首次迁移耗时取决于索引大小。**这是安装当次失败的恢复机制，不是长期自动降级**；新版本投入使用后，旧快照不包含后续索引更新。确认新版本稳定且不需该备份后，才清理对应事务目录；不要删除仍在进行的事务或恢复受阻的备份。

Python bootstrap 和 Linux 源码安装暂未使用上述运行时事务；升级前应停止服务并自行备份数据/配置，再重装。各安装形式都会保留搜索范围、数据库、预算与语义设置；再次传 `Root` 不覆盖旧范围，改范围应使用设置窗口，或 `stop` 后编辑配置再 `start`。旧配置缺少新字段时由加载器补默认值。

新写入向量为 float16，旧 float32 缓存仍可读取；FTS external-content 结构会按需要迁移。要转换旧向量并回收 SQLite 空间，可停服务后维护：

```text
data-search stop --config CONFIG
data-search compact --config CONFIG
data-search start --config CONFIG
```

`compact` 持有同一实例锁，避免与后台同时操作；需额外临时磁盘空间，预算不足会拒绝。升级首次迁移及离线压缩耗时取决于原索引规模，不承诺瞬时完成。

## 数据库接入

把 [数据库配置示例](../examples/databases.json) 改成实际参数后加入配置的 `databases`，或在设置窗口添加。

| 类型 | 需要提供 |
|---|---|
| SQLite | 文件绝对路径、当前账号读取权限、允许表/字段 |
| MySQL | 主机、端口、库名、只读用户名、密码环境变量名、允许表/字段，必要时 TLS |
| PostgreSQL | 同上，表名使用 `schema.table` |
| 持续正文索引 | 每表 `id_column`、`text_columns`，可选 `updated_column` 与 `sync` 预算 |

密码本身不写 JSON 或启动命令。`password_env` 必须在后台进程启动前可用；临时终端环境变量不会自动成为登录自启动环境。账号权限、对象/字段白名单和结构化查询限制共同生效。

正文持续同步要求非空稳定的单列主键/唯一键，先完整分页，之后按水位增量并周期完整核对；每轮限额不会截断整个表。删除只在完整扫描后确认。授权视图可以实时查询，但不用于持续正文索引。详细一致性、键约束和复合索引建议见 [数据库说明](DATABASES.md)。

可用 `python examples/create_sample_database.py <新路径>/orders.sqlite` 生成合成 SQLite，脚本拒绝覆盖已有文件；首次安装试用也可把 `examples/sample-documents` 作为明确搜索范围。

## 卸载与保留数据

Windows 可执行随包或安装目录中的卸载脚本：

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -InstallDir 'D:\apps\data-search'
```

Linux：

```bash
bash scripts/uninstall.sh --install-dir "$HOME/.local/share/data-search/app"
```

卸载先停止实例、移除本实例用户启动项，再删除受管理的程序目录；默认保留配置、模型和索引。只有显式 `-DeleteData` / `--delete-data` 才删除带管理标识的数据目录。外部模型目录和原始资料不删除。目录缺少管理标识、相互嵌套、包含不允许的链接或服务无法停止时拒绝删除。

恢复保留的数据可重新安装到相同路径。更详细的已通过验证和待验收条件见 [验证报告](VALIDATION.md) 与 [路线图](roadmap/README.md)。8GB/16GB 实机、几百 GB 正文、Linux 硬件、多机连接不能由本机烟雾测试替代。
