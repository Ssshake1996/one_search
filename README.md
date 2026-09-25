# one_search

面向 DeepSeek Harness（DSH）等 MCP 客户端的本地文件与数据库检索插件。v0.4 提供 README 驱动安装、共享后台服务、11 个 MCP 工具、Windows 自带运行时的安装包和本地设置窗口；文件解析、正文索引与语义计算在本机进行。

新安装默认发现当前账号可访问的本地磁盘；可以改为指定目录。当前交付单机，接口已保留 `node_id`；多机传输、认证与跨机结果合并尚未实现。项目名是 `one_search`，Python 包 `data_search`、命令及 MCP 条目 `data-search` 保持兼容。

## 安装与接入：给用户和 Agent 的执行入口

请让 Agent 读取本 README，再按下面顺序执行。安装由可重复运行的脚本完成，不需要图形安装向导。**先安装并启动基础检索，模型在后台单独准备；模型下载失败不会阻止文件名、关键词检索。**

1. 查看 [GitHub Releases](https://github.com/Ssshake1996/one_search/releases)，选择目标平台的同一版本资源。Windows x64 优先 `windows-amd64-native.zip`，自带 Python 和依赖。`py311-bootstrap.zip` 需要 CPython **3.11 x64**、venv/pip；项目 wheel 单独使用仍需依赖。Linux 当前使用 Python 3.11+ 源码安装，真实主机验收状态见 [验证报告](docs/VALIDATION.md)。不要将 Windows wheelhouse 用于 Linux。
2. 同时下载 `SHA256SUMS.txt`，核对 ZIP 的 SHA-256 后完整解压。原生安装器进一步验证包内逐文件清单。校验失败应重新获取同一发行资源，不能跳过校验。
3. 检查已有 `%LOCALAPPDATA%\data-search\app\install-manifest.json` 和对应 `config.json`。首次安装默认检索当前账号可访问的整机本地磁盘；仅在用户要求目录范围时传 `-Root`。现有配置会保留，重装参数不会静默改写旧范围、数据库或模型设置。
4. 在完整解压目录执行安装；检查退出码，再读取数据目录的 `install-result.json`。随后接入 DSH，并通过宿主工具实际验证连接。后台全机发现和语义索引可继续进行，不必等待它们全部完成才使用搜索。

Windows 默认安装（不需要管理员权限）：

```powershell
# 替换为实际下载的 ZIP；与 SHA256SUMS.txt 中同名条目比较完整值。
Get-FileHash '.\one-search-VERSION-windows-amd64-native.zip' -Algorithm SHA256
# 完整解压后进入解压目录，再运行：
.\scripts\install.ps1
if ($LASTEXITCODE -ne 0) { throw 'one_search installation failed' }
$searchApp = Join-Path $env:LOCALAPPDATA 'data-search\app'
$searchConfig = Join-Path $env:LOCALAPPDATA 'data-search\data\config.json'
$searchCli = Join-Path $searchApp 'runtime\data-search.exe'
Get-Content (Join-Path (Split-Path $searchConfig) 'install-result.json') -Raw
& $searchCli installation-status --config $searchConfig --install-dir $searchApp
& $searchCli search '合同' --mode files --config $searchConfig
```

bootstrap 的 `$searchCli` 是 `<InstallDir>\venv\Scripts\data-search.exe`。默认程序目录为 `%LOCALAPPDATA%\data-search\app`，配置/模型/索引为同级 `data`。自定义程序目录与数据目录必须独立、不嵌套；原始资料放在两者之外。Agent 不应删除未通过管理清单识别的目录来“修复”安装。

| 安装参数 | 用途与默认行为 |
|---|---|
| `-InstallDir DIR` / `-DataDir DIR` | 明确程序和数据位置；现有安装必须与清单一致 |
| `-Root @('D:\资料','D:\项目')` | 首次仅检索这些目录；省略时为整机范围 |
| `-Exclude @('D:\私人资料')` | 首次排除指定路径，可多项；不改变整机默认发现方式 |
| `-Preset low` | 首次资源档位 `low/balanced/fast`，默认 `balanced`；档位只改预算，不缩小范围 |
| `-ModelDir 'D:\models\bge-small-zh-v1.5'` | 首次使用离线模型目录，后台按固定模型指纹与 SHA-256 校验 |
| `-SkipModel` | 首次关闭语义索引；文件名和关键词可用；不更改旧配置开关 |
| `-NoAutostart` | 不注册当前用户登录启动项，安装末尾仍启动一次服务 |
| `-Python PATH` / `-PackagePath WHEEL` / `-Wheelhouse DIR` | 源码/bootstrap 高级入口；离线源码构建还需匹配的构建依赖，使用项目 wheel 可避免现场构建 |

例如用户明确限定范围并提供独立位置：

```powershell
.\scripts\install.ps1 -Root @('D:\资料','D:\项目') `
  -InstallDir 'D:\apps\one-search' -DataDir 'D:\app-data\one-search'
```

安装脚本退出 `0` 表示运行时和基础检索探测成功，`1` 表示安装/启动/验收失败；Linux 参数或平台前提错误可返回 `2`。PowerShell 参数绑定错误发生在脚本执行前，也可能没有结构化结果。正常日志可有多条 JSON/提示，**`<DataDir>/install-result.json` 是本次成功安装的完整验收结果**；失败时输出 `event=installation_result, ok=false, stage, error.code`，不要把旧的成功文件当作本次结果。

安装结果分别显示 `runtime_installed`、`daemon_running`、`basic_search_ready`、`semantic.state` 和 `dsh_connection`。`dsh_connection=not_checked` 只表示尚未由宿主验证。`indexing_complete=null` 表示该安装探测没有认证全机索引完成；检索不到结果时继续检查覆盖、排除项和积压，不应声称电脑没有这份资料。

模型状态与恢复（不阻塞当前基础检索）：

```powershell
& $searchCli model-status --config $searchConfig
& $searchCli model-start --config $searchConfig
& $searchCli model-import --source 'D:\offline-model' --config $searchConfig
& $searchCli model-cancel --config $searchConfig
```

`model-start`/`model-import` 返回任务后立即退出；重复启动复用正在运行的任务。`model-status` 显示 `queued/running/ready/failed/cancelled/interrupted`、当前资产、字节进度和恢复建议。校验成功的资产会在重试时复用，未完成资产重新下载/复制；离线导入只接受固定 BGE embedding 模型，不接受 DSH 聊天模型。更改 `semantic.enabled` 请通过设置/配置完成，下载模型本身不会擅自开启用户关闭的语义索引。

DeepSeek Harness 需要 Node.js 22+ 和已安装的 DSH。为兼容“发行包在 D:、DSH 用户目录在 C:”的情况，使用随包的注册脚本：它先把插件复制到 DSH 用户目录所在磁盘，再调用 DSH 官方 CLI。后台安装成功后，在完整解压目录执行：

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
$dshPackage = Join-Path ((npm root -g).Trim()) '@deepseek-ai\dsh'
if (-not (Test-Path (Join-Path $dshPackage 'package.json'))) { throw 'Locate the actual installed DSH package and set $dshPackage' }
$registration = @{dshPackage=$dshPackage; profile='web'} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path (Get-Location) 'dsh-register.json'), $registration, [Text.UTF8Encoding]::new($false))
node ./plugins/deepseek-harness/register.mjs ./dsh-register.json
if ($LASTEXITCODE -ne 0) { throw 'DSH registration failed' }
dsh --profile web
```

注册脚本返回 `registered=true, connected=false`；启动 profile 后才连接 MCP。首次激活也可安装缺失的后台服务；已有 v0.3 后台需提供匹配 Release 进行保留配置升级，不能只更新 npm 插件。请让 DSH 实际调用 `index_status` 和一次 `search`：发现工具和调用成功才算该 profile 已接入。可执行的独立 DSH 连接诊断、profile 配置、自定义路径、Linux 注册和版本要求见 [DSH 接入说明](plugins/deepseek-harness/README.md)。本地安装成功与 DSH 聊天回答质量是不同验收项。

Linux 无桌面安装：

```bash
# 源码根目录，Python 3.11+、venv/pip；无需 Tk。
bash scripts/install.sh
# 没有 systemd 用户会话时显式手动启动模式：
bash scripts/install.sh --no-autostart
# 自定义范围/路径示例：
bash scripts/install.sh --root /srv/docs --install-dir "$HOME/.local/share/data-search/app" \
  --data-dir "$HOME/.local/share/data-search/data" --no-autostart
```

默认注册 systemd 用户服务；不可用时明确失败，不自动申请 root 或启用 linger。`--no-autostart` 安装后启动一次，重启机器后需要执行 `<InstallDir>/venv/bin/data-search start --config <DataDir>/config.json`。Linux 对应参数为 `--model-dir/--skip-model/--python/--package-path/--wheelhouse`，验收文件与 Windows 相同。

原生升级继续运行新包安装器：校验、暂存、保留迁移前快照、启动失败回滚。v0.3 文件索引升级后需要按预算重新核对文件身份并处理正文，期间旧文件 ID 会被拒绝，不会悄悄指向新文件。遇到中断，先用同一配置执行 `status`、`model-status`、`installation-status`；按错误阶段修复下载/依赖/空间/路径后重试同一命令。不要用重新初始化配置替代修复。具体服务维护、回滚边界、卸载保留数据及平台限制见 [安装说明](docs/INSTALL.md)。


## 能检索什么

- 文件名、路径、扩展名、大小与修改时间；不支持解析正文的文件仍可按文件名找到。
- 关键词、本地 BGE 语义和混合检索；返回正文片段、路径、定位、索引时间和过期标记。
- SQLite 3、MySQL、PostgreSQL 的授权表/字段：结构发现、受控实时查询、选定正文的持续本地索引。
- 暂停/恢复、资源预算、重启续传、文件核对、数据库水位增量与周期删除核对。

正文支持 TXT/Markdown/LOG、常见代码和配置、JSON/JSONL、XML、HTML、CSV/TSV、DOCX、XLSX、PPTX、文字层 PDF。完整格式、编码及定位规则见 [文件说明](docs/FORMATS.md)。图片/OCR、音视频、压缩包内部、旧版 DOC/XLS/PPT 暂不提取正文。

数据库连接需要地址/文件路径、只读账号、密码环境变量、允许访问的表和字段。持续正文索引还要求稳定的单列唯一键、文本字段；提供维护正确的 `updated_column` 可启用水位增量。后台默认每个来源每轮四页、每页 250 行，后续轮次从保存的游标继续，直至遍历授权表。完整巡检完成后才删除缺失记录。数据库版本、配置、必要索引和一致性边界见 [数据库说明](docs/DATABASES.md)。

## 检索范围与资源取舍

新配置的 `scope: "machine"` 在 Windows 枚举固定本地磁盘，按当前账号权限读取；不提升权限，跳过网络/可移动卷、目录链接、安装目录、索引/模型目录及配置排除项。Linux 本地文件系统发现已有代码，实际主机验证待完成。旧配置缺少 `scope` 时继续使用原有 `roots`，升级不会自动扩大范围。

文件名、正文、语义是三层范围。正文和语义分别支持 `all`、`directories`、`none`，并可限制扩展名；语义以已提取正文为基础。下例保留整机文件名发现，只处理资料目录正文，再缩小语义范围：

```json
{
  "scope": "machine",
  "roots": [],
  "indexing": {
    "content_scope": "directories",
    "content_roots": ["D:/资料"],
    "content_extensions": [".txt", ".md", ".pdf", ".docx"],
    "semantic_scope": "directories",
    "semantic_roots": ["D:/资料/知识库"],
    "semantic_extensions": []
  }
}
```

这是现有配置的局部示例；保留其他字段。扩展名空列表表示不加额外格式过滤，仍受支持类型和资源预算限制。数据库正文由自己的 `index` 授权，文件目录/扩展名筛选不限制数据库；`semantic_scope: "none"` 会停止数据库语义嵌入。

| 设置 | 默认值与含义 |
|---|---|
| 正文与数据库调度 / 模型线程 | 按轮单路处理，ANN 独立构建；模型线程默认 1，可设 1–2 |
| 进程树 RSS 预算 / 最低可用内存 | 1,024 / 768 MiB，采样检测 |
| Windows 单 worker Job 内存限额 | 512 MiB，限制提交虚拟内存，**不是 RSS** |
| Windows worker CPU rate cap | 25%，受系统或上层 Job 配额影响；同时限制亲和性、降低优先级 |
| 索引与模型磁盘预算 / 最低空闲空间 | 10,240 / 1,024 MiB |
| 单文件正文大小 / 字符上限 | 32 MiB / 200 万字符 |
| 单文件解析超时 / 模型闲置释放 | 30 / 120 秒 |
| 后台轮次 / 有监听时完整核对 | 180 / 3,600 秒 |

Windows Job 限制若因宿主策略无法应用，会在 `worker_controls.fallback_errors` 显示；服务仍保留 RSS 采样预算。启动 worker 时显式限制 OpenBLAS、OpenMP、MKL、NumExpr 线程。Linux 当前仅有优先级/亲和性和采样预算，尚无 cgroup 硬配额，也未做 Linux 实机验收。

文件发现和正文解析使用可续传的持久化队列，先建立文件名目录，再按预算处理正文和语义。Windows 尝试读取当前账号有权访问的既有 NTFS USN 日志；日志不可用、回卷或发生无法可靠定位的变更时回退核对。它不创建日志、不提升权限，也不通过 MFT 完成首次全盘发现。指定目录模式还可使用文件监听，周期核对继续作为兜底。更新时效是调度间隔加队列积压时间，数据较多时会超过几分钟，不能等同于 Everything 的文件名引擎。

8GB/16GB 电脑、几百 GB 实际资料库还需按 [路线图](docs/roadmap/README.md) 验收。现有资源控制提供限速、暂停和缩小正文/语义范围的手段，不能仅凭磁盘容量承诺索引空间和检索耗时。

## 索引与运维能力

| 优化 | 当前行为 |
|---|---|
| 分层索引 | 文件名、正文、语义分别确定范围，避免所有文件都解析和嵌入 |
| 索引存储 | 向量以 float16 持久化；FTS5 使用 external-content，避免再存一份正文分词文本；提供离线 `compact` |
| 后台 ANN 更新 | 独立受控 worker 增量发布不可变分段并按阈值合并；上一版本继续可查询 |
| 过滤与证据读取 | 候选不足时逐步扩展，上限 8,192；批量读取命中证据并去重 |
| 数据库持续同步 | Keyset 分页、水位增量、持久化检查点、完整巡检及分批删除 |
| 进程资源控制 | Windows Job、低优先级与亲和性、数值库线程限制、RSS/磁盘预算和可见的回退状态 |
| 持久化调度 | 文件名发现、正文处理和语义工作记录进度；重启后继续处理，状态显示已知覆盖、待处理队列和错误 |
| 检索质量 | 短中文关键词与结构化分块，保留源文档定位；质量结论以固定语料评测为准 |
| 安装与设置 | Windows 原生升级快照及启动失败恢复；数据库显式预检；图形资源预算和 DSH bundle |

代码已实现不等于所有机器与数据规模均已验证。实际质量、资源和性能结论仅依据 [验证报告](docs/VALIDATION.md)；路线图单独记录尚未满足的验收条件。

## MCP 与结果语义

常见任务与命令见 [使用与排错](docs/USAGE.md)，资源、迁移、配置备份和退出见 [运维说明](docs/OPERATIONS.md)。设置窗口新增“查找与核实”“维护与退出”；数据库按连接→发现→选字段→预检完成，典型接入不需要编辑 JSON。

| 工具 | 用途 |
|---|---|
| `search` | 文件名、关键词、语义或混合检索；目录、日期、大小、类别、多扩展名筛选；精确文件名优先及重复正文折叠 |
| `fetch` | 文件返回带过期标记的索引片段；数据库按标识实时读取 |
| `inspect_source` | 有效文件范围、来源、授权结构和数据库同步进度 |
| `query_database` | 结构化筛选、排序、分页、有限关联/聚合，不接收任意 SQL |
| `index_status` | 索引覆盖、扫描错误、数据库进度、ANN 发布状态及资源控制状态 |
| `diagnose_path` | 具体文件/目录为何未找到、是否过期，以及可采取的动作 |
| `read_context` | 命中附近上下文与结构化来源引用 |
| `refresh_path` | 用户显式请求的单文件刷新；目录进入有界队列 |
| `prioritize_path` | 用户显式请求优先处理已有授权范围内的路径 |
| `pause_indexing` / `resume_indexing` | 持久化暂停、定时恢复及手动恢复，检索继续可用 |

`c:...` 从命中片段开始取上下文，`d:...` 从文档开头取。语义检索指定条件时，对符合条件的已发布向量分批精确评分，并提示 `filtered_semantic_exact`；匹配向量越多，耗时越长。不带过滤的 ANN 仍是近似检索，按文档去重时的候选上限会明确提示。ANN 更新期间可使用已发布版本并返回 `semantic_index_updating`；尚无可用版本时返回 pending 提示，不在查询请求中同步建索引。旧版缺少文件身份的记录需要一次分批关联和解析，旧引用不会绑定到可能已经替换的同名文件；详见使用说明。

DSH 聊天模型负责理解问题、调用工具并引用证据；本地 embedding 模型负责把正文和问题转成向量，二者不同。向量分数仅用于排序，不是回答可信度。本地计算不把正文上传给模型服务；MCP 返回内容进入宿主后，后续处理由宿主自身配置决定。

架构和扩展协议见 [接口约定](docs/CONTRACTS.md)。开发演示资料可按 [文件说明](docs/FORMATS.md) 生成，并仅把生成的 `files` 目录作为测试范围。
