# 单机验证报告 · 0.3.0

日期：2026-09-25。本轮八项改进已实现：持久化分片调度、批量目录与资源计量、Windows USN 增量及回退、分段 ANN、真实 DeepSeek Harness bundle、图形资源预算/数据库预检、原生升级回滚、短中文文件名与结构化正文检索。仍以单机为交付范围，多机仅预留接口。

测试使用指定合成目录和隔离数据库，没有索引用户整台电脑的私人资料。历史报告保留在 [0.2.0](VALIDATION-v0.2.md) 和 [0.1.0](VALIDATION-v0.1.md)。物理 8GB/16GB 机器、几百 GB 真实正文和 Linux 目标主机仍待验收。

## 回归与正确性

环境：Windows 11（内核 10.0.22631）、Python 3.11.3、i7-13700、24 逻辑核、31.814GiB 实际内存。未核实磁盘介质型号。完整回归 **349 passed、3 skipped、0 warnings，33.70 秒**；跳过项是一个非 Windows 分支及两个单独配置的数据库测试。详细计数及跳过原因见 [tests-v0.3.json](validation/tests-v0.3.json)。另行启动隔离 MySQL 8.4.11 / PostgreSQL 17.11，61 项 adapter 测试通过，真实分页、水位微秒/并列、重启、更新、新增和完整核对后删除通过；使用只读检索账号，测试服务器已停止，见 [databases-v0.3.json](validation/databases-v0.3.json)。

| 领域 | 已验证行为 |
|---|---|
| 调度 | 目录/解析/事件游标持久化、未完目录重开、重启续传、文件/数据库/嵌入不因单个阶段失败而饿死 |
| 缺失清理 | 根目录完整成功后才核对删除；保留扫描中后来创建的文件；权限错误不授权删除 |
| 失败与预算 | 暂时解析失败和解析后空间不足都持久化退避；队列溢出留下核对请求；已无日志权限不会连续重启全盘扫描 |
| 升级索引 | 旧表增加切分版本；未变化文件分批重新解析；旧正文在解析与暂时失败期间可查，成功后替换；数据库旧水位之外的未变行做一次分页全量升级 |
| 空闲开销 | 持久化 embedding/orphan 队列避免每轮扫描全量 chunks；短期缓存资源采样，磁盘按写入估算并每十秒校准 |
| ANN | 小段有界构建、旧段复用、删除压缩、schema-3 迁移、失败原子发布、读者与回收竞争、中文路径 |
| 检索 | 短中文 FTS 优先查询计划、来源/扩展名过滤评分、准确来源偏移、标题/句子切分、相邻页不混合 |
| 设置与安装 | 显式只读数据库预检、预算/队列/错误显示、保存失败恢复、升级快照和持锁数据保护 |

阶段时间限额限制“是否启动下一项”，不会在期限到达的瞬间中断正在执行的解析或数据库页；这些操作有单独超时。内存是采样进程树预算，加 Windows 单 worker 的提交内存限额，不能当成整个应用 RSS 的绝对硬上限。磁盘计量也不是文件系统硬配额。

## 5,000 个实际合成文件的目录基准

生成 5,000 个文件，总正文 135,000 字节，只登记文件名/元数据，关闭正文及嵌入。使用同一脚本分别加载 v0.2 源码与 v0.3 源码。两轮都启用 SQLite trace 计数；测试立即驱动下一轮，排除产品的等待间隔。详见 [旧版](validation/catalog-v0.2-baseline.json)、[本版](validation/catalog-v0.3.json)。

| 阶段 | v0.2 时间 / 事务提交 | v0.3 时间 / 事务提交 |
|---|---:|---:|
| 首次登记 | 165.857 秒 / 15,002 | 1.632 秒 / 161 |
| 无变化复查 | 80.660 秒 / 5,002 | 1.078 秒 / 161 |

本版两次均用三个调度轮次，采样峰值约 31.19MiB。批量写入、跳过不需正文的解析任务和资源计量缓存减少了本次工作量。主机还有其他任务、缓存状态未严格隔离，单次结果不能推导所有机器的固定加速倍数。这里没有文档解析、模型推理、MCP 或几百 GB 资料，不是正文首建耗时承诺。

## 10 万段分段 ANN 基准

10 万文档记录/片段，27,026,395 字节合成正文；向量是固定随机种子的 512 维数据，没有真实模型推理或十万个物理源文件。每类 100 次暖查询。详见 [benchmark-v0.3.json](validation/benchmark-v0.3.json)。

| 指标 | v0.2 历史 | v0.3 本次 |
|---|---:|---:|
| ANN 首建 | 739.306 秒 | 547.086 秒 |
| 1,000 删除 + 1,000 新增 ANN 同步 | 13.435 秒 | 4.572 秒 |
| 初建进程树峰值 RSS | 230.61MiB | 135.93MiB |
| 文件名查询内核 P95 | 2.210ms | 2.053ms |
| FTS 关键词内核 P95 | 105.115ms | 108.439ms |
| 随机向量 ANN 查询 P95 | 10.021ms | 68.985ms |
| 最终索引空间 | 506.51MiB | 424.30MiB |

本次首建分两轮发布：80,000（pending）→100,000（complete）；增量复用五个旧段，只增加一个新段。查询阶段 RSS 峰值 183.38MiB，增量阶段 116.75MiB。主机负载不是受控实验，上表为观察值。

**分段没有带来全面提速：本次随机向量查询明显慢于历史版本。** 实现的收益是较小构建工作集、限制每轮构建量和复用旧段，代价是多段查询合并。带来源/扩展名过滤时使用每批 256 向量的精确评分，改善过滤召回，但遍历所有匹配向量，耗时随数量增长。查询内核不包含文件权限/过期检查、模型编码、MCP 或回答生成。

## 小语料质量与实际服务资源

[检索报告](validation/retrieval-v0.3.json) 保留原 35 题及新增 4 题。原 16 文件/8 数据库记录语料的 22 个可回答语义问题，Recall@5 = 21/22，Recall@10 = 22/22；关键词 5/5、文件名 1/1、精确数据库 3/3，与历史指标一致。新增四题均首位命中，两个正文问题的第二章标题与来源位置核验通过。

扩展为 18 个文件后，23 个语义题 Recall@5、Recall@10 均为 22/23：唯一中文到英文跨语言问题从原第 10 名掉出前十。未隐藏这项退步；小语料文件召回不能当作一般回答正确率。四个无答案题只检查候选证据，没有测试聊天回答层。完整扩展结果见 [39 题原始记录](validation/retrieval-v0.3-expanded.json)。

另行用真实后台、本地固定 BGE 模型、16 个合成文件和 SQLite 8 行进行 [资源测量](validation/resources-v0.3.json)。每 100ms 采样 daemon 及 worker 后代；索引和 ANN 完成后才开始查询：

- 首建采样峰值 RSS **329.77MiB**。
- 20 次本地 RPC 语义查询：中位数 **17.581ms**、P95 **43.508ms**、最大 **65.231ms**。
- 模型卸载后静置十秒：RSS **44.969–52.809MiB**；该窗口 CPU 增量低于系统计时分辨率，不等于承诺零 CPU。

这只代表小语料。测试模型闲置释放为两秒，产品默认 120 秒；不含 DSH/MCP 桥、系统文件缓存和其他程序。RSS 相加可能重复共享页，采样可能遗漏短峰值。32GB 主机上的配置限额不等于物理 8GB 机器验收。

## NTFS 与 DeepSeek Harness

[journal-v0.3.json](validation/journal-v0.3.json) 记录假日志及真实系统结果。当前账号对工作盘的卷句柄访问被拒绝，实际读取零条 USN 记录；服务正确回到周期扫描。游标重置、改名/删除、溢出、重启与核对原子性由合成日志测试验证。**本轮没有真实 USN 变更重放的实机证据，也没有实现 MFT 首次枚举。**

[dsh-v0.3.json](validation/dsh-v0.3.json) 使用机器上已安装的 DeepSeek Harness：CLI 0.1.5-rc.1、官方 MCP client 0.1.5-rc.2、Cordis 4.0.2、Node 24.11.1。实际 CLI 在隔离 profile 注册 bundle，即使禁用 npm 安装脚本也可在首次 profile 激活时自动安装原生后台、启动、发现五个 MCP 工具并检索合成文件。卸载插件上下文移除工具，后台继续可用；第二次激活保留原配置，测试最终停止后台，未修改用户默认 profile 或调用聊天模型。

DSH 的 `plugin add` 只注册插件；首次激活负责安装/启动。不能说 package 注册但不激活时服务也已经启动。后端升级仍用新 release 安装器，更新 npm bundle 本身不自动升级后端。

## 发行与复现

Windows x64 原生 ZIP 不要求系统 Python；另有 CPython 3.11 x64 bootstrap、wheel、清单及 SHA-256。模型仍需首次下载或指定离线目录。Windows PowerShell 5.1 下从两个 ZIP 重新解压，验证中文/空格路径、首次/重复安装、真实离线模型语义检索、SQLite 和官方 MCP 五个工具及卸载。实际原生 v0.2→v0.3 升级通过；在快照完成后注入不兼容 SQLite 和配置变更，使真实新 daemon 启动失败，确认恢复旧 runtime、旧 schema、原配置和旧服务检索，再完成正常升级。完整记录见 [install-v0.3.json](validation/install-v0.3.json)；该报告及发行清单对应最终构建的源码哈希。测试使用隔离目录并关闭自启动，没有做真实注销/重启。

```powershell
python -m pytest -q -rs
node --test plugins/deepseek-harness/test/bootstrap.test.mjs
python scripts/benchmark_catalog.py --output .bench-new-catalog --files 5000 --report catalog.json
python scripts/benchmark_search.py --output .bench-new-ann --documents 100000 --report ann.json
python scripts/evaluate_retrieval_v03.py --work .runtime/new-quality --model <已有模型目录> --output quality.json
python scripts/measure_resources.py --output resources.json
python -m pip install -e . --no-deps
python scripts/build_release.py --output dist/new-release --native
```

基准必须使用新目录，随机向量索引不能用作业务语义索引。没有验收：其他 Windows 版本/策略、真实注销重启、Linux 系统服务、物理 8GB/16GB、几百 GB 正文、长时业务数据库负载、其他 DSH 版本及多机。后续验收和接口边界见 [路线图](roadmap/README.md)。
