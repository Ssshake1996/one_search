# one_search 升级与重装

安装和接入的执行入口是 [README](../README.md#升级或重装)，可让 Agent 根据该文档操作。升级不需要先卸载，也不需要重新初始化配置。本文说明 v0.5.1 的连接协调、首次迁移和故障恢复。

## 先判断当前连接

`data-search.exe daemon` 是后台服务，`data-search.exe mcp` 是 MCP 桥接进程。宿主会在桥接断开后尝试重连，因而只执行 `stop`、关闭浏览器标签页或反复结束 MCP 进程，都不能保证运行时已经释放。

| 当前情况 | 升级前需要做什么 |
|---|---|
| Windows 原生安装，全部 DSH profiles 已使用 v0.5.1 或更新兼容 bundle | 使用 v0.5.1 或更新兼容安装器，DSH 服务端与 Web 页面可以保持打开；安装器自动协调临时断开与恢复 |
| 任一 DSH profile 仍使用 v0.5.0 或更旧 bundle | 先正常停止这个 DSH 服务端/profile，再升级；终端启动的可用 `Ctrl+C` 退出 |
| 还有标准 stdio MCP 配置或其他未参与协调的宿主 | 在该宿主中断开对应 server；若没有单独控制，正常退出宿主 |
| 打开了 `Settings.vbs` / 原生设置窗口，或还有其他命令在运行 | 关闭设置窗口，等待相关命令结束；DSH Web 面板本身不属于原生窗口 |
| Windows bootstrap 或 Linux 安装 | 先停止所有相关宿主与设置窗口，再停止服务、备份配置和数据，按原安装方式重装 |

不同 profiles 可以连接同一后台。必须逐个确认实际运行的 bundle 已更新；更新某个 profile 不代表其他 profiles 已支持协调。新 bundle 保持对 v0.5.0 后台的兼容，保证本次升级失败回滚后仍可连接，但 v0.5.0 安装器不会执行新的协调协议。

## 首次从 v0.5.0 迁移

1. 在现有 `install-manifest.json` 确认程序目录和数据目录，记录相关 DSH profiles。自定义安装继续使用原路径。
2. 正常停止所有旧 DSH 服务端/profile，断开其他 MCP 宿主，关闭原生设置窗口。不要只关闭网页或只停止 daemon。
3. 下载同一版本的 `one-search-0.5.1-windows-amd64-native.zip` 与 `SHA256SUMS.txt`，验证完整 SHA-256。完整解压到现有程序、数据目录之外。
4. 在独立 PowerShell 进入新解压目录，运行下面的安装命令。首次迁移不应依赖仍持有旧 MCP 连接的 DSH 会话执行覆盖升级。
5. 安装成功后，用新解压包的注册脚本更新每个相关 DSH profile，然后重新启动它们，实际调用 `index_status` 和 `search`。

默认安装的后台升级命令：

```powershell
Set-Location 'D:\Downloads\one-search-0.5.1-windows-amd64-native'
$searchApp = Join-Path $env:LOCALAPPDATA 'data-search\app'
$searchData = Join-Path $env:LOCALAPPDATA 'data-search\data'
.\scripts\install.ps1 -InstallDir $searchApp -DataDir $searchData
if ($LASTEXITCODE -ne 0) { throw 'Upgrade failed; inspect this run before retrying' }
$searchConfig = Join-Path $searchData 'config.json'
$searchCli = Join-Path $searchApp 'runtime\data-search.exe'
& $searchCli version --config $searchConfig
& $searchCli installation-status --config $searchConfig --install-dir $searchApp
Get-Content (Join-Path $searchData 'install-result.json') -Raw
```

自定义安装将 `$searchApp`、`$searchData` 改成原清单中的绝对路径。安装失败时读取本次错误输出，不把以前留下的成功 `install-result.json` 当成本次结果。搜索范围、数据库授权、预算与语义开关继续沿用旧配置。

在同一新解压目录注册 `web` profile：

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
$dshPackage = Join-Path ((npm root -g).Trim()) '@deepseek-ai\dsh'
if (-not (Test-Path (Join-Path $dshPackage 'package.json'))) {
  throw 'Set $dshPackage to the actual installed DSH package directory'
}
$registration = @{dshPackage=$dshPackage; profile='web'} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path (Get-Location) 'dsh-register.json'), $registration, [Text.UTF8Encoding]::new($false))
node ./plugins/deepseek-harness/register.mjs ./dsh-register.json
if ($LASTEXITCODE -ne 0) { throw 'DSH registration failed' }
dsh --profile web
```

其他 profile 改用其真实名称；使用自定义 DSH home 的安装应在注册请求中指定原来的 `dshHome`。注册参数、Linux 命令和现有 profile 配置见 [DSH 接入说明](../plugins/deepseek-harness/README.md)。`registered=true` 仅表示注册成功，随后真实工具调用成功才证明连接恢复。

## 后续 Windows 原生升级

完成首次迁移后，仍使用新包的相同安装命令。支持协议的 DSH profiles 可以保持运行，流程为：

1. 安装器在数据目录建立持久维护标记，通知每个活动 DSH 插件实例暂停 runtime 启动。
2. 宿主停止 MCP 重连，注销对应工具并关闭桥接进程；已经接受的管理命令需要实际退出。Web 页面继续提供维护状态。
3. 安装器检查残留运行时占用，暂存新运行时，停止后台并保存配置、数据和适用的外部索引快照。
4. 安装器替换运行时并进行启动健康检查；本次激活失败时尝试恢复旧运行时及快照。
5. 成功激活或完成安全回滚后，安装器移除维护标记，宿主恢复 MCP 连接。若通知丢失，宿主会通过低频检查标记恢复。

安装中新增的 profile 会先登记再检查维护标记；标记存在时进入待机，不加载待替换的 runtime。`host-clients` 是每个活动插件实例的临时注册，正常卸载插件时移除，也不进入升级快照；它与记录历史客户端用途的持久 `clients` 登记不同。

后台升级自动协调不保证 DSH 插件代码热替换。本次同时更新 bundle 时，安装后台后重新注册新 bundle，再重启对应 profile 使新代码生效。升级结束后检查后台版本、服务状态及真实工具调用；首次扫描或语义积压不必全部完成。

## 失败与中断恢复

先读取本次安装器退出码及 `event=installation_result, ok=false` 记录中的 `error.code`。PowerShell 安装脚本错误通常附带 `stage`，原生事务错误不保证有该字段；已建立事务时，`<InstallDir>/.upgrade-<ID>/transaction.json` 的 `phase` 记录事务阶段。前置拒绝可能尚未创建事务目录。不要将旧的成功 `install-result.json` 当成本次结果。

| 本次错误/状态 | 处理方式 |
|---|---|
| `runtime_in_use` | 查看返回的 PID、进程角色，正常关闭对应旧 MCP 宿主/原生设置窗口或等待命令结束，再重试；不强杀进程，不删除运行时文件 |
| `host_prepare_failed` | 某个 DSH 宿主未能确认释放连接；正常退出对应服务端/profile 后重试。不要只关闭它的浏览器页 |
| `upgrade_in_progress` | 另一个安装器或维护事务占用实例；等待该次操作完成，避免并行重装 |
| `upgrade_recovery_required` | 保留维护标记和事务目录，按本次报错排除占用或路径问题后重跑同一安装器；无法确认快照与运行时身份时保持维护 |
| Web 显示维护，安装器已经意外退出 | 重新运行同一解压包、相同程序/数据路径的安装命令，由安装器处理恢复 |

`runtime_in_use` 的前置检查拒绝时，原服务和配置保持不变。检查不会主动结束其他进程；没有协议的客户端仍需用户先停止，不能把“当前没有发现进程”理解成它以后不会重新启动。

`<DataDir>/upgrade-state.json` 覆盖升级及回滚。安装器意外中断后，宿主不会因等待超时而自行加载 runtime；标记损坏或暂时不可读也保持维护。**不要手工删除该标记来强行重连。** 重新运行同一安装器会核对保留的事务、恢复可确认的旧快照，再尝试升级；缺少恢复材料或无法确认身份时拒绝继续。

`<InstallDir>/.upgrade-<ID>` 中保存事务记录、运行时及索引/配置快照，成功后也会保留。恢复受阻时不要删除这些目录。该机制只处理安装当次失败或中断，不会自动降级已经投入使用的新版本；旧快照不含之后的新索引更新。空间需求、备份清理及其他维护命令见 [安装说明](INSTALL.md) 和 [运维说明](OPERATIONS.md)。

## Windows bootstrap 与 Linux

这两种形式尚未实现上述自动替换/快照回滚事务。依次停止宿主连接、关闭设置窗口、停止后台，再备份和重装。仅停止后台仍会被 MCP 宿主重新启动。

默认 Windows bootstrap 后台停止命令：

```powershell
& "$env:LOCALAPPDATA\data-search\app\venv\Scripts\data-search.exe" stop `
  --config "$env:LOCALAPPDATA\data-search\data\config.json"
```

默认 Linux 后台停止命令：

```bash
search_base="${XDG_DATA_HOME:-$HOME/.local/share}/data-search"
"$search_base/app/venv/bin/data-search" stop --config "$search_base/data/config.json"
```

使用了自启动或外部进程守护时，升级期间也要通过实际部署方式暂停它，避免服务再次被拉起。随后按 [README](../README.md) 对应平台命令安装到原路径、重新注册需要更新的 bundle，并启动宿主验证。平台与真实升级测试的完成范围以 [验证报告](VALIDATION.md) 为准。
