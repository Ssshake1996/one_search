# 资源控制和维护

安装入口和参数以 [README](../README.md) 为准。下面的命令均使用安装后得到的 `data-search` 可执行文件；`--config` 指向已有配置。维护操作不会修改检索到的原始文件或数据库。

## 调整后台资源

`preset low`、`preset balanced`、`preset fast` 先显示配置预览；加 `--apply` 才保存，命令会停止现有服务、保存配置并重新启动，失败时恢复原配置。预设只调整资源、批次和本地模型线程，保留整机/目录范围、正文/语义范围、排除项、数据库配置和磁盘配额。

| 参数 | low | balanced | fast |
| --- | ---: | ---: | ---: |
| 进程树 RSS 预算 MiB | 768 | 1024 | 2048 |
| 单个工作进程内存限制 MiB | 512 | 512 | 768 |
| 工作进程 CPU 上限请求 | 15% | 25% | 50% |
| 正文文件/批次 | 8 | 32 | 64 |
| 语义批次/轮 | 1 | 4 | 8 |
| 本地模型线程 | 1 | 1 | 2 |
| 模型空闲卸载秒数 | 60 | 120 | 180 |

这些值是调度预算和限制请求，不是实测占用或端到端响应时间保证。`status` 的 `resources` 与 `worker_controls` 提供采样占用和实际生效的限制。大文件、索引规模、操作系统缓存和同机应用都会影响运行表现。8GB 机器建议先使用 low，通过真实工作负载验证；切换预设不会偷偷缩小检索范围。

```powershell
data-search preset low --config C:\one-search\data\config.json
data-search preset low --apply --config C:\one-search\data\config.json
```

暂停可以指定时长，也可以持续到手动恢复。暂停状态写在实例数据目录，服务重启后仍有效；定时暂停到期后自动恢复。暂停只控制后台索引，已有数据仍可检索。

```powershell
data-search pause --seconds 1800 --config C:\one-search\data\config.json
data-search resume --config C:\one-search\data\config.json
```

后台默认在系统 CPU 连续 3 秒超过 85% 时等待，下降到 65% 后恢复；使用电池时降低后台频率，电量不高于 20% 时等待。查询优先安排，但连续轮询最多推迟后台 3 秒，避免用户一边等待新文件一边搜索时永远无法完成索引。

配置中的 `runtime_policy.idle_only` 和 `runtime_policy.on_ac_only` 默认均为 `false`，可显式启用；`idle_seconds` 默认 120。Windows 使用当前会话最近输入时间判断空闲。无法获取输入空闲时间的平台会报告 `idle_detection_unavailable_policy_not_enforced`，继续执行 CPU/电池限制；它不会声称已遵守“仅空闲运行”。`status` 区分 `user_paused`、`pause_until`、`automatic_wait` 和具体 `reason`。没有电池的普通台式机/服务器不会因此永久等待电池信息。

## 查看空间与清理备份

```powershell
data-search space --config C:\one-search\data\config.json --install-dir C:\one-search\app
```

结果区分索引、模型、日志、配置及其他文件，并列出保留的升级/迁移备份。统计不跟随目录链接；达到统计上限时 `incomplete` 为 `true`，不能把该结果当作完整磁盘盘点。`relocations` 提供迁移阶段、已复制字节和恢复动作。

`cleanup-backup` 只接受 `space` 返回的具体备份 ID。先停止服务再执行：

```powershell
data-search stop --config C:\one-search\data\config.json
data-search cleanup-backup BACKUP_ID --config C:\one-search\data\config.json --install-dir C:\one-search\app
data-search start --config C:\one-search\data\config.json
```

未完成/需恢复的升级事务不会被清理；当前索引不会被清理。迁移后旧索引若发生变化，校验和不再匹配时拒绝删除，保留数据供检查。清理升级备份会失去该次事务保留的旧运行时与迁移前快照，命令结果明确列出被清理的 ID。

## 迁移索引位置

迁移只改变 `index_dir`，配置入口、实例身份、模型、凭据、服务文件和 DSH 注册保持原位置，避免每个宿主重新接入。目标需要是独立的专用目录，并有足够空间容纳完整索引副本及校验余量。

```powershell
data-search stop --config C:\one-search\data\config.json
data-search relocate-index D:\one-search-index --config C:\one-search\data\config.json
data-search start --config C:\one-search\data\config.json
```

操作同时锁定实例和源/目标索引，复制 SQLite/ANN 产物，校验文件和 SQLite 完整性后原子更新配置。旧索引保留，可通过 `space` 查看并显式清理。其他实例不能占用带有当前实例归属标记的索引目录。

复制失败时保留旧配置和旧索引。进程中断后先运行 `space` 查看 `relocations`，停止服务后用相同目标重试 `relocate-index`；已有事务记录和归属标记用于识别可恢复的临时副本。配置已切换而完成记录尚未写入时，同一命令校验已复制产物后补全事务。若目标或旧索引被其他操作改动，命令会拒绝自动清理，需要先核对状态。

## 导出与恢复设置

```powershell
data-search export-config D:\backup\one-search-settings.json --config C:\one-search\data\config.json
data-search restore-config D:\backup\one-search-settings.json --config C:\one-search\data\config.json
```

导出文件含范围、排除、数据库允许表列和资源设置。它不包含密码、凭据库条目、索引、模型、服务令牌、实例身份或宿主注册；保留的 `password_env` 只是重新连接时使用的变量名。不要把配置导出误认为完整数据备份。

恢复默认只预览。目标机器上不存在的目录或 SQLite 文件会列入 `issues`，存在问题时拒绝 `--apply`。通过 `--mappings` 提供旧路径到新路径的 JSON 映射（也可 `--mappings @文件路径`），核对预览后停止服务并加 `--apply`。运行数据目录、索引位置和当前模型位置保留目标机器自己的配置。远程数据库列于 `requires_reconnect`，需重新检查连接和最小读取权限。

## 共享实例、自启动和卸载

`clients` 列出登记过的 MCP/DSH 宿主及最近使用时间；登记不等于当前仍在线。`register-client` 对相同 ID 幂等，`remove-client` 只删除登记，不会停止公共服务，也不会替用户卸载宿主插件。`lifecycle --install-dir ...` 给出当前安装对应的启动/停止/卸载参数及所有受影响的登记客户端。

```powershell
data-search clients --config C:\one-search\data\config.json
data-search lifecycle --install-dir C:\one-search\app --config C:\one-search\data\config.json
data-search autostart disable --install-dir C:\one-search\app --config C:\one-search\data\config.json
```

`autostart enable/disable` 只改变登录启动策略，停止当前运行需要单独 `stop`。Windows 使用当前用户启动项；Linux 使用安装时对应的 systemd 用户服务。

使用安装目录内的 `uninstall.ps1`（Windows）或 `uninstall.sh`（Linux）卸载。默认保留数据；Windows 加 `-DeleteData`，Linux 加 `--delete-data` 才删除配置、模型、索引。卸载会取消并等待属于该实例的模型下载任务，在持续持有调度/服务锁的情况下删除运行时。外置索引仅删除带有归属标记的已知生成产物，目录中其他文件保留。停止、升级、卸载公共后台服务会影响全部使用该实例的客户端。

## 后端集成约定

- `RuntimePolicy.decision()` 每次准备开始后台轮次时调用；`status()` 不消耗调度轮次。文件/数据库查询入口调用 `foreground()`，后台策略不拒绝只读查询。
- `IndexDirectoryLease(config)` 由主引擎持有至关闭，独立向量工作进程不要重复申请同一把锁。
- `relocate_index()`、`restore_config(..., apply=True)`、`cleanup_backup()` 要求实例已停止；它们不从只读检索工具暗中触发。
- `MaintenanceGuard(config, stop=False)` 持有模型任务的 admission/worker 锁和实例锁。替换或删除运行时要在同一临界区完成；只先调用取消模型任务再立即释放锁不能防止新的任务启动。
- Native 升级同时快照稳定数据目录和独立 `index_dir`，失败激活恢复旧运行时、精确配置和两处索引状态。成功事务保留快照，清理必须使用具体 ID。

当前新增验证包括定时暂停跨重启、持续查询不饿死索引、CPU/电池/空闲恢复、跨实例锁冲突、复制失败与中断恢复、外部索引升级失败回滚、敏感字段不进入配置导出及 Windows 实际卸载锁行为。物理 8/16GB、Linux 实机和大规模长期运行结论仍以项目验收记录为准。
