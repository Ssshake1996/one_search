# 查找、核实与排错

DSH Web 用户可点击侧栏 **one_search** 打开设置与扫描/索引进度，具体操作见 [Web 面板说明](DSH-WEB.md)。下方保留 Agent 工具和 CLI 的使用方式。

安装与 DSH 接入先读 [项目 README](../README.md)。以下 `data-search` 表示安装结果中 `cli` 指向的实际可执行文件；每条命令均需 `--config <配置绝对路径>`。Windows PowerShell 用 `& $cli ... --config $config` 调用。设置窗口是运行设置和核实入口，安装由 README 和脚本完成。

## 在 DSH 中完成任务

- “找去年项目目录里的 PDF”：先用 `search` 的 `directory`、`extensions`、`modified_after`、`modified_before` 筛选，再用 `read_context` 核实。
- “记不清名字，内容与预算有关”：用关键词或语义检索；结果只提供候选证据，不代表回答一定存在。跨语言漏检时可分别尝试来源语言的关键词。
- “这份文件为何搜不到”：提供完整本地路径，调用 `diagnose_path`。目录诊断展示已发现的状态数量，不编造整机覆盖百分比。
- “文件已经改过，请更新这份资料”：显式调用 `refresh_path`；一个文件在现有解析预算内刷新，目录进入有界优先队列。繁忙时返回可重试状态，语义嵌入和发布继续异步完成。
- “阅读命中附近上下文并给出来源”：使用命中 `id` 调 `read_context`，引用 `path`、`locator`、索引时间和 `revision`；过期内容不能当成当前事实。
- “查订单金额/精确编号/数量”：先 `inspect_source`，再 `query_database` 结构化只读查询。语义相邻记录不能代替精确计算。

找不到结果可能是未发现、未提取、范围排除、权限不足、格式不支持、模型未就绪，也可能确实没有关键词命中。应根据状态解释，不能把无结果说成电脑上不存在资料。

## 检索条件

```text
data-search search "预算" --mode keyword --directory D:/资料/项目 --extensions .pdf .docx --modified-after 2025-01-01 --modified-before 2026-01-01 --min-size 1024 --max-size 33554432 --config CONFIG
data-search search "合同.pdf" --mode files --sort modified_desc --config CONFIG
data-search search "设备维护的步骤" --mode hybrid --fold-duplicates --config CONFIG
```

不同过滤条件按 AND 组合；多扩展名列表内部按 OR 匹配。`extension` 和 `extensions` 同时出现仍为交集。日期支持 ISO 8601，未写时区按 UTC；起点包含、终点不包含，大小单位是字节。`applied_filters` 返回实际应用的条件。

`category` 可为 `document`、`spreadsheet`、`code`、`data`、`image`、`audio`、`video`、`archive`。类别是扩展名分组，部分分组会重叠；图片、视频、压缩包命中不表示其正文已支持，正文范围见 [格式说明](FORMATS.md)。

`files` 文件名模式与 `keyword` 模式的显式排序作用于匹配范围；语义/混合模式在召回候选内排序，并返回说明。默认相关度排序中，文件名模式优先精确名称；混合模式也将精确文件名置前。语义过滤对匹配的已发布向量逐批评分，耗时随匹配向量数增加。

`--fold-duplicates` 只在本次返回候选内折叠完整且不过期的相同提取正文，列出其他位置。它不证明两个原始二进制文件相同，也不根据文件名“final”、修改时间或相似度判断哪份是正式最新版。

## 具体文件的状态与恢复

```text
data-search diagnose D:/资料/说明.pdf --query "维护" --config CONFIG
data-search prioritize D:/资料/本周项目 --config CONFIG
data-search refresh D:/资料/说明.pdf --config CONFIG
data-search context c:123 --before 2 --after 3 --config CONFIG
data-search open d:45 --folder --config CONFIG
```

| `code` | 含义与下一步 |
|---|---|
| `excluded_or_outside_scope` | 路径不在有效授权范围；检查根目录、排除项和访问权限 |
| `not_discovered` | 尚未发现；可显式优先处理该路径 |
| `missing_or_moved` | 原路径不存在；按文件名重新查找，不猜测新的对应文件 |
| `permission_denied_or_unavailable` | 当前账户无法读取或来源不可用；先恢复来源访问 |
| `legacy_identity_unverified` | 旧索引没有文件身份；等待分批迁移或刷新此文件 |
| `replaced_file` / `stale` | 同路径被替换，或同一文件已更新；刷新后重新获得引用 |
| `content_excluded` | 只登记文件名，正文范围或敏感模板将其排除 |
| `content_pending` / `extraction_failed` | 正文排队或提取失败；查看队列/原因，可目标重试 |
| `resource_or_file_limit` | 资源预算、文件大小或字符预算限制；先检查具体 reason |
| `unsupported_content` / `encrypted_document` | 当前格式没有正文能力、无可提取文字或已加密 |
| `partial_content` | 只处理了部分正文；不可声称已经检索完整文件 |
| `ready` | 已知正文可查；仍需查看单独的 semantic、query_assessment 和来源有效性 |

v0.3 及更早文件索引没有文件身份字段。升级后的第一次发现会分批重新建立这些文件的关联及正文，不把旧缓存与一个可能已替换的同名文件绑定。旧引用可能失效，请重新搜索。新增索引使用文件系统身份；没有稳定身份的文件系统会标记 `identity_verification=unavailable`，不会把快照声明为当前已核实内容。

改名或跨目录移动后，目前通过重新发现得到新位置和新引用；没有可靠身份关联时不自动重定向旧引用。删除、越出范围、链接别名或当前无权读取的文件不能继续通过旧结果读取。文件的原位编辑会保留文档身份，但引用里的 revision/索引时间会变化。

打开源文件只能由用户显式操作；CLI/设置窗口支持打开常见文档或所在目录。脚本、可执行文件、快捷方式不能作为“打开证据”启动。Linux 无显示环境返回路径和 `headless`，不尝试打开窗口。

## 范围与常用操作

设置窗口“检索范围”提供整机/目录、全局排除、正文和语义独立排除、敏感正文模板，以及有界的已知文件影响预览。敏感模板为显式选择，启用后仍保留文件名检索。保存并激活范围后，撤销的正文/向量缓存会清理；曾提供给宿主的聊天内容须在宿主管理。

“资源预算”选择 low/balanced/fast，支持仅空闲、仅插电及暂停 30 分钟。后台因为资源等待和用户主动暂停分别显示。设置窗口状态页可查看模型准备、覆盖队列和错误；保持该页显示时定期刷新。

“数据库”连接后选表、字段、实时/本地索引、唯一键、水位和业务说明，保存前执行只读预检。密码保存在系统凭据库或使用已有环境变量；详情见 [数据库说明](DATABASES.md)。

“维护与退出”展示共享连接、空间与备份，提供设置导出/恢复预览、索引位置迁移、自启动和退出，以及安装目录对应的卸载命令。迁移/恢复/清理等需要停止后台时会明确拒绝正在运行的实例。完整 CLI、升级与失败恢复步骤见 [运维说明](OPERATIONS.md)。
