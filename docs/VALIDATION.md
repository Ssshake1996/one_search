# v0.5.1 升级协调验证报告

v0.5.1 处理 MCP 宿主自动重连导致 Windows 运行时无法替换的问题：支持协调协议的 DSH profiles 在维护期间释放 MCP 与管理进程，安装器完成事务后恢复连接。本文截至 2026-09-26，区分代码回归、源码宿主实测和发行包验收；**固定原生候选包的升级、回滚、真实进程中断恢复、三个 DSH profiles 重连检索及空目录自动安装均已通过 Windows Server 2022 云端验收**。具体范围与仍未覆盖场景见下文。

本轮回归对应运行时代码提交 `efeff0ff123209b89d056fa41719737d08294992`。实测使用隔离的合成文件、数据库和 DSH profiles，未扫描用户整台电脑，未修改默认 DSH profile。v0.5 的界面与安装实测保留在 [v0.5 历史报告](VALIDATION-v0.5.md)，更早的数据库、检索质量、资源和性能测量保留在 [v0.4 历史报告](VALIDATION-v0.4.md)，均不作为本轮的新测量。

## 回归与双平台 CI

[本机测试记录](validation/tests-v0.5.1.json) 与 [CI 记录](validation/ci-v0.5.1.json) 对应同一运行时代码提交。GitHub Actions [运行 36219474264](https://github.com/Ssshake1996/one_search/actions/runs/36219474264) 已完成，Windows 和 Ubuntu 两个任务均成功。

| 环境 | Python | Python 耗时 | Node |
|---|---|---|---|
| 本机 Windows | 526 passed / 5 skipped / 0 failed | 136.166 秒 | 62 passed / 0 skipped / 0 failed |
| CI Windows Server 2025 | 525 passed / 6 skipped / 0 failed | 309.31 秒 | 62 passed / 0 skipped / 0 failed |
| CI Ubuntu 24.04 | 499 passed / 32 skipped / 0 failed | 42.79 秒 | 59 passed / 3 skipped / 0 failed |

本机 Python 跳过一个非 Windows 分支和四项未配置隔离真实 MySQL/PostgreSQL 实例的测试；Windows CI 另跳过缺少已安装控制台入口的一项测试。Ubuntu 主要跳过 Windows 进程/安装功能、需要显示环境的 Tk 界面及上述真实数据库测试；三个 Node 跳过项均为 Windows 安装器相关路径。逐项原因见 CI JSON，不将跳过计为通过。

新增回归覆盖运行时进程归属与真实 Windows EXE 锁、多 profile 维护准入与登记竞争、管理命令实际退出前保持占用、超时保存、中断激活及重复快照恢复、安装/数据身份文件缺失或截断、损坏维护标记、首次安装模型工作进程取消，以及 bootstrap 遇到占用时在修改前拒绝。恢复单元测试和故障注入不替代真实发行包进程中断验收。

Ubuntu 的无界面安装 smoke 实际通过 10 项：安装、无模型基础检索、重复安装保留配置、诊断、暂停时刷新、导出、索引迁移、保留数据卸载、重装和仅删除受管数据。没有读取用户文件；没有验证 systemd 自启动或 Linux 语义模型。CI 不等同于 Windows 原生发行包验收。

## 源码运行时与真实 DSH 多 profile

[DSH 生命周期记录](validation/dsh-lifecycle-v0.5.1.json) 使用真实宿主和源码运行时，未调用聊天模型。两个已连接 profiles 确认进入维护并释放 MCP 工具；Web 状态报告维护，写入被拒绝。维护标记存在时，即使收到恢复请求也没有启动 runtime；此时加入的第三个 profile 保持待机。原共享 daemon 保持不变。

移除维护标记后，三个 profiles 各恢复 11 个 MCP 工具，并分别完成实际检索。退出其中一个 profile 不影响共享后台和其他连接，配置保持不变；测试结束后清理宿主登记并停止合成后台。

该记录保存了当次插件哈希，时间早于最终安全错误日志和新安装器最低版本检查调整。它证明所记录版本的源码生命周期路径，**不证明最终 native ZIP 的完整验收**。最终代码的相关行为另有上述回归测试覆盖。

## Windows bootstrap 发行包

[bootstrap 验收记录](validation/bootstrap-v0.5.1.json) 的结论是 **`passed_with_async_retry`**。Windows PowerShell 5.1 下实际核对 ZIP 完整性与逐文件校验和，约 36 秒完成隔离基础安装，执行关键词检索；daemon 活动和仅 MCP 桥接活动两种情况下，重装均在停止服务或修改配置前被拒绝。正常关闭客户端和后台后重装成功，保留配置、文档与实例身份，并再次检索成功。

首轮异步离线模型导入被中断。后续诊断还观察到 CLI 非零退出但无输出，以及 PowerShell CLR `HRESULT 80004005`；**尚未确认这些现象的根因**。在新的隔离配置中，使用未修改的普通 CLI 和原有隐藏进程参数进行有界重试后，异步导入完成并达到 `ready`，随后语义检索通过。前台工作进程仅用于诊断，不作为异步成功的替代证据。

该验收还实际调用 11 个 MCP 工具，完成合成 SQLite 记录与文件的检索和内容读取，并验证清理受管安装/数据后保留原始文档。没有接触用户的 DSH 或检索实例。

JSON 中 ZIP 哈希属于文档及通用插件版本元数据最终刷新前的候选包；已验收 wheel 的 SHA-256 为 `dc9d4bb6f798b5a39c8485bf62f99c7e985036d6bad88e696275f30deecd3d6c`。最终发行 ZIP 应以 Release 的 `SHA256SUMS.txt` 为准，不能把候选 ZIP 哈希当作最终归档哈希。

## Windows 原生发行包：升级、中断恢复与 DSH 自动安装通过

GitHub Actions [运行 36227757603](https://github.com/Ssshake1996/one_search/actions/runs/36227757603) 已于 2026-09-26 07:49:59 UTC 成功完成。独立的 Windows Server 2022 runner（`windows-2022`，镜像 `20260920.314.1`）使用固定的候选包；[原生升级报告](validation/upgrade-v0.5.1.json) 与 [DSH 自动安装报告](validation/dsh-auto-v0.5.1.json) 的 `success` 均为 `true`。测试脚本与工作流提交为 `ad4f1946160b2ee56f92e023df01100a639afa2f`，运行时仍由 `efeff0ff123209b89d056fa41719737d08294992` 构建，未为本次验收重编译。同一测试提交的[标准回归运行 36227757600](https://github.com/Ssshake1996/one_search/actions/runs/36227757600) 中，Windows 与 Ubuntu 任务也再次通过；前文计数表仍引用运行时提交对应的基线运行 36219474264，不合并不同运行的数量。

| 验收项 | 实际结果 |
|---|---|
| 旧 DSH MCP 桥接及原生设置窗口占用 | 分别以 `runtime_in_use` 拒绝升级；原服务身份、配置、运行时哈希不变，原查询可用 |
| 新版本激活失败回滚 | 事务达到 `rolled_back`；恢复旧运行时，配置和索引行保留；三个 profiles 均重新发现 11 个 MCP 工具并完成检索 |
| 真实中断升级器后恢复 | 终止经进程树、可执行文件、fixture 参数和创建时间校验的 4 个升级器进程；维护标记一直保留到正常安装器重试；旧事务 `rolled_back`、新事务 `complete`，三个 profiles 均重连检索成功 |
| 正常升级 | 事务达到 `complete`，运行时为 v0.5.1 且字节哈希匹配候选；配置、索引行保留，三个 profiles 均重连检索成功 |
| 维护准入与共享后台生命周期 | 回滚、中断恢复和成功升级三轮均验证维护期间 Web 状态与写入限制、带标记时拒绝恢复、新加入 profile 待机；退出一个 profile 后共享 daemon 与另一个 profile 的查询仍可用 |
| 空目录 DSH 托管安装 | 未提供显式 command 或 configPath，由插件在空安装/数据目录安装 v0.5.1；安装器自行恢复完成、仅登记一个宿主、11 个 MCP 工具与关键词检索通过，Web 状态为 running |

两套验收只使用合成文件与独立 DSH homes，没有修改默认 profile、扫描用户整机或请求聊天模型；自动安装保留指定目录范围并关闭语义模型。两套合成受管安装与数据均已清理。原生升级步骤耗时 128 秒，首次托管安装步骤耗时 20 秒。

[云端环境与归档记录](validation/upgrade-ci-v0.5.1.json) 保存四份原始报告的 SHA-256、下载归档信息，以及完整的实际 DSH 组件解析版本。宿主为 DSH CLI `0.1.5-rc.1`、Node `v24.11.1`、pnpm `10.24.0`；MCP client、scope 和 launch-environment 均为 `0.1.5-rc.2`。DSH 的 DeepSeek 组件按参考宿主精确锁定，第三方依赖由 npm 解析，以报告中的 `package_lock_sha256` 标识实际安装图；隔离 profile 包由 DSH 在线安装。初次云端尝试曾因传递 peer 漂移产生 npm `ERESOLVE`，本次精确锁定后安装及完整验收通过。

被验收的原生候选 ZIP SHA-256 为 `240de589cbf5aa6947bd3733f08ba1efc907f6e557d91a2fd53345ef1fd4730f`，其中 `data-search.exe` 的 SHA-256 为 `cacff5988147dd4e2081658462ab06fabb53d1677bd2e83f77380630ce366bd6`；升级起点为实际发布的 v0.5.0 ZIP，其 SHA-256 为 `b938f14af308a533d75e3c6da006ad55e870eca5d470d9a9d0e7a64883e024ae`。候选归档早于最终文档及版本元数据刷新，最终发行 ZIP 的校验和应以 Release `SHA256SUMS.txt` 为准；最终打包需核对运行时字节仍与已验收哈希一致，不能把候选 ZIP 哈希当作最终归档哈希。

## 验收机器的资源限制

早期多轮本机原生验收未完成，部分日志明确出现 V8 `Committing semi space failed` 或内存分配失败；另有一次 DSH 注册退出码为 `134`，以及一次测试 shell 尚未启动的失败。期间记录的系统可用提交额度约为 0.7–1.1 GiB，同时可用物理内存约为 15.8 GiB。提交额度与物理空闲内存是不同约束；这些记录说明验收环境存在资源压力，但**不能据此为所有非零退出、CLR 错误或异步中断确定同一个根因**。本机重试停止后，上述独立 Windows Server 2022 云端运行完成了两套发行包验收；这些早期失败记录仍作为环境历史保留。

隔离验收子进程设置了 `NODE_OPTIONS=--max-old-space-size=192 --max-semi-space-size=4` 以限制测试宿主的 V8 堆；本次通过的原生与自动安装报告均记录了该设置。原生 suite 在启动运行时之前预先注册 profile 包，每轮保留两个常驻宿主并临时加入第三个。上述设置仅属于测试环境，没有修改产品默认参数、全局环境或无关进程，也不代表产品已经在物理 8GB 电脑上通过验收。

## 仍未覆盖的产品场景

- 物理 8GB/16GB 电脑、真实数百 GB 文件、小时或天级运行；本轮没有新的全机扫描耗时或常驻资源结论。
- LAN/SSH 远程浏览器、多机接入与联合检索。
- 本轮 DSH Web 页面连接真实 MySQL/PostgreSQL，以及通过完整页面执行模型下载/导入；CLI、适配器或历史界面测试不能替代这些页面路径。
- 所有浏览器、DPI、目标服务器和 Linux 桌面密钥环/systemd 用户会话；Linux 原生包与自动事务升级也不在本轮通过范围内。

Windows bootstrap 与 Linux 仍须停止相关宿主和后台后按原方式重装，没有原生 Windows 的自动替换和快照恢复事务。首次从旧 DSH bundle 迁移、错误处理和保留维护标记的恢复步骤见 [升级说明](UPGRADE.md)；安装入口见 [README](../README.md)，Web 使用方法见 [DSH Web](DSH-WEB.md)。
