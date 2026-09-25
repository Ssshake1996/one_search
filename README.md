# one_search

面向 DeepSeek Harness（DSH）等 MCP 客户端的本地文件与数据库检索插件。v0.3 提供共享后台服务、MCP stdio 接口、Windows 自带运行时的安装包和本地设置窗口；文件解析、正文索引与语义计算在本机进行。

新安装默认发现当前账号可访问的本地磁盘；可以改为指定目录。当前交付单机，接口已保留 `node_id`；多机传输、认证与跨机结果合并尚未实现。项目名是 `one_search`，Python 包 `data_search`、命令及 MCP 条目 `data-search` 保持兼容。

## 安装与接入

下载：[GitHub Releases](https://github.com/Ssshake1996/one_search/releases)。版本对应的执行证据见 [验证报告](docs/VALIDATION.md)。

Windows x64 原生包名为 `one-search-0.3.0-windows-amd64-native.zip`，不要求另装 Python。完整解压后双击 `Install.cmd`，或在解压目录执行：

```powershell
.\scripts\install.ps1
```

安装器准备本地模型、安装并启动后台服务、设置当前用户登录自启动，生成 `%LOCALAPPDATA%\data-search\app\mcp.json`。支持 `mcpServers` JSON 的宿主可使用此文件。DeepSeek Harness 使用专用 Cordis bundle，接入方式见下文。

仅检索选定目录：

```powershell
.\scripts\install.ps1 -Root @('D:\资料', 'D:\项目')
```

安装后双击程序目录的 `Settings.vbs`，可切换整机/目录范围、设置正文与语义范围、编辑内存/CPU/磁盘预算、添加数据库并控制服务。更改数据库配置后，先点击“测试数据库”，通过有时限的只读连接、授权字段和持续索引唯一键检查，再“保存并启动”；检查失败保留运行中的配置。密码填写环境变量名，数据库高级参数仍可用 JSON 编辑。

也提供 `one-search-0.3.0-windows-amd64-py311-bootstrap.zip`，要求匹配的 **CPython 3.11 x64**、venv/pip。源码安装要求 Python 3.11+；Linux 当前只有源码/bootstrap 脚本，尚未完成目标系统验证。默认包不包含模型，首次安装需联网下载，也可用 `-ModelDir` 指定离线模型，或 `-SkipModel` 仅使用文件名和关键词。

DeepSeek Harness 可在完整解压的发行目录注册插件，再启动对应 profile：

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
dsh plugin --profile web add ./plugins/deepseek-harness
dsh --profile web
```

`plugin add` 完成注册；首次启动该 profile 时，插件安装缺失的后台服务，再连接官方 MCP 客户端。已有安装会复用，DSH 退出后后台服务继续运行。安装范围、自定义路径、离线模型及宿主版本条件见 [DSH 接入说明](plugins/deepseek-harness/README.md)。

Windows 原生升级仍运行新包的安装器。升级会校验并暂存新运行时，在停止服务后保存迁移前索引/配置快照，启动失败时尝试恢复；旧运行时与快照保留在程序目录的 `.upgrade-*` 中。需要额外磁盘空间，详细回滚边界和清理方式见 [升级说明](docs/INSTALL.md#服务控制维护与升级)。

完整命令、平台条件、升级及卸载见 [安装说明](docs/INSTALL.md)。当前 Windows 实测环境与具体发行验证见 [验证报告](docs/VALIDATION.md)，不把未测试系统当作已经支持。

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

| 工具 | 用途 |
|---|---|
| `search` | 文件名、关键词、语义或混合检索，可指定数据源和扩展名 |
| `fetch` | 文件返回带过期标记的索引片段；数据库按标识实时读取 |
| `inspect_source` | 有效文件范围、来源、授权结构和数据库同步进度 |
| `query_database` | 结构化筛选、排序、分页、有限关联/聚合，不接收任意 SQL |
| `index_status` | 索引覆盖、扫描错误、数据库进度、ANN 发布状态及资源控制状态 |

`c:...` 从命中片段开始取上下文，`d:...` 从文档开头取。语义检索指定来源/扩展名时，对符合条件的已发布向量分批精确评分，并提示 `filtered_semantic_exact`；匹配向量越多，耗时越长。不带过滤的 ANN 仍是近似检索，按文档去重时的候选上限会明确提示。ANN 更新期间可使用已发布版本并返回 `semantic_index_updating`；尚无可用版本时返回 pending 提示，不在查询请求中同步建索引。升级后已有正文会分批重新切分，旧片段在替换成功前继续可用。

DSH 聊天模型负责理解问题、调用工具并引用证据；本地 embedding 模型负责把正文和问题转成向量，二者不同。向量分数仅用于排序，不是回答可信度。本地计算不把正文上传给模型服务；MCP 返回内容进入宿主后，后续处理由宿主自身配置决定。

架构和扩展协议见 [接口约定](docs/CONTRACTS.md)。开发演示资料可按 [文件说明](docs/FORMATS.md) 生成，并仅把生成的 `files` 目录作为测试范围。
