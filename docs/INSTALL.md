# data_search 单机安装与接入

本版先验证单机。每台机器拥有独立 `node_id`，搜索结果保留节点标识；远程节点传输、认证和跨机汇总尚未实现。填写远程节点 ID 不会自动访问其他机器。

## 发行形式与前提

当前提供 **Python bootstrap 安装器**，要求 Windows 或 Linux 上已有 Python 3.11+、venv 和 pip。Linux 发行版可能需要单独安装 `python3-venv`。本项目尚未交付免 Python 的原生安装包，也未验证 Codex/DSH 插件商店的安装钩子。

安装器执行同一套流程：创建隔离环境、安装服务依赖、初始化明确选定的搜索目录、准备本地模型、配置用户自启动、启动服务、生成 MCP 配置。默认需要网络下载 Python 依赖和固定版本模型。模型推理在本机运行。

软件目录与数据目录必须独立且不互相嵌套。首次安装拒绝非空且没有管理标识的目录。安装器不扫描全盘，不改写 AI 宿主配置，也不把插件注册进全局 marketplace。

Windows 默认目录：

- 程序：`%LOCALAPPDATA%\data-search\app`
- 配置、模型、索引：`%LOCALAPPDATA%\data-search\data`

Linux 默认目录：

- 程序：`${XDG_DATA_HOME:-~/.local/share}/data-search/app`
- 配置、模型、索引：`${XDG_DATA_HOME:-~/.local/share}/data-search/data`

## Windows

在仓库根目录打开 PowerShell，指定真实资料目录：

```powershell
.\scripts\install.ps1 -Root 'C:\Users\you\Documents'
```

需要多个目录时：

```powershell
.\scripts\install.ps1 -Root @('D:\资料', 'D:\projects') -InstallDir 'D:\apps\data-search' -DataDir 'D:\app-data\data-search'
```

安装器注册当前用户登录时运行的启动项，通过隐藏窗口启动后台服务，不需要管理员权限。启动项仅对当前用户生效；没有用户登录时，不保证服务运行。如果操作系统策略禁用了 Windows Script Host 或用户启动项，使用 `-NoAutostart` 并手工启动；这不是已注册的系统级服务。

若本机执行策略阻止脚本，按组织或设备策略允许运行经过审阅的本地脚本；安装器不修改全局执行策略。

开发或烟雾测试可不下载模型、不注册启动项：

```powershell
.\scripts\install.ps1 -Root 'D:\samples' -InstallDir 'D:\test-app' -DataDir 'D:\test-data' -SkipModel -NoAutostart
```

`-SkipModel` 仅在首次创建配置时关闭语义功能。之后重复安装会保留现有配置；显式修改 `config.json` 的 `semantic.enabled` 才能改变原设置。关键词和文件名检索无需模型。

## Linux

```bash
bash scripts/install.sh --root "$HOME/Documents"
```

```bash
bash scripts/install.sh --root /srv/docs --root /srv/project \
  --install-dir "$HOME/.local/share/data-search/app" \
  --data-dir "$HOME/.local/share/data-search/data"
```

默认注册并启动 **systemd 用户服务**。运行安装命令的用户必须有正常的 systemd 用户会话；否则安装器会明确失败，要求使用 `--no-autostart`。自动启动保证的是用户会话生命周期，未自动启用 linger，也未注册 root 系统服务。

```bash
bash scripts/install.sh --root /srv/docs --skip-model --no-autostart
```

日志可通过 `journalctl --user -u <安装清单中的 unit_name>` 查看；后台程序也在数据目录保留自身状态信息。无 systemd 场景下由 CLI 分离后台运行，下次开机需手工 `start`。

## 离线模型与依赖

模型目录需要由相同版本的 `data-search model-download --config <配置路径>` 准备，包含固定版本的模型、分词器、配置和校验清单。不要把任意聊天模型目录当成 embedding 模型目录。

```powershell
.\scripts\install.ps1 -Root 'D:\docs' -ModelDir 'D:\models\bge-small-zh-v1.5'
```

```bash
bash scripts/install.sh --root /srv/docs --model-dir /srv/models/bge-small-zh-v1.5
```

`ModelDir` 采用已有模型的绝对路径，不复制或删除该外部目录。该选项仅对首次配置生效；重装不覆盖已有模型路径。完全离线时，还需本平台和 Python 版本兼容的 dependency wheelhouse：

```powershell
.\scripts\install.ps1 -Root 'D:\docs' -PackagePath '.\wheelhouse\data_search-0.1.0-py3-none-any.whl' -Wheelhouse '.\wheelhouse' -ModelDir 'D:\models\bge-small-zh-v1.5'
```

构建发行包：

```bash
python scripts/build_release.py --output dist
```

该脚本构建项目 wheel、下载当前平台依赖 wheel、附上安装器/文档/插件壳并生成 SHA256 清单和 ZIP。模型不包含在默认 ZIP 内。请分别在 Windows/Linux 目标架构与目标 Python 小版本构建、验证，原生依赖 wheel 不可跨平台混用。最终 ZIP 仍需要 Python；不能称为独立原生程序。

## MCP 与 DSH 接入

安装后读取 `<InstallDir>/mcp.json`。该文件含有已经解析成绝对路径的 `command` 和 `args`，复制 `mcpServers.data-search` 条目到宿主支持的 MCP 配置即可。DSH 的具体配置容器按所安装版本文档处理；本项目没有改写未知的 DSH 配置文件。

示意结构：

```json
{
  "mcpServers": {
    "data-search": {
      "command": "C:/Users/you/AppData/Local/data-search/app/venv/Scripts/data-search.exe",
      "args": ["mcp", "--config", "C:/Users/you/AppData/Local/data-search/data/config.json"]
    }
  }
}
```

Codex 格式插件副本位于 `<InstallDir>/plugin`，其中 `.mcp.json` 已补全绝对路径。源码目录 `plugins/data-search` 是未绑定安装路径的 shell，不能把空 MCP 配置当成已运行的集成。当前不自动安装到 Codex marketplace。

MCP 使用 stdio，后台 API 仅监听本机回环地址；不应将其直接端口转发或开放到网络。本地解析和向量生成不调用聊天模型，但 MCP 返回的内容会进入连接的 AI 客户端，其模型部署决定后续数据流向。

## 服务控制与配置

Windows 使用 `<InstallDir>\venv\Scripts\data-search.exe`；Linux 使用 `<InstallDir>/venv/bin/data-search`。所有操作指定绝对 `--config` 路径：

```text
data-search status --config CONFIG
data-search pause --config CONFIG
data-search resume --config CONFIG
data-search scan --config CONFIG
data-search search "服务器成本" --mode hybrid --config CONFIG
data-search stop --config CONFIG
data-search start --config CONFIG
```

修改配置前先停止服务，再启动。重装默认保留配置，包括 roots、数据源、资源预算与语义设置；重新传 `Root` 不会替换原 roots。文件监听、解析状态和实际索引覆盖应通过 `status` 查看。

## 数据库需要的信息

把 `examples/databases.json` 中需要的条目调整后加入配置的 `databases`。示例未指向真实业务数据，也不会自动启用。

| 类型 | 必填信息 |
|---|---|
| SQLite | 数据库文件绝对路径、允许访问的表/视图及字段 |
| MySQL | 可达主机、端口、库名、只读用户名、密码环境变量名、允许访问的表/字段；按服务器要求配置 TLS |
| PostgreSQL | 可达主机、端口、库名、只读用户名、密码环境变量名、允许访问的 `schema.table`/字段；按服务器要求配置 TLS |

密码本身不填入 JSON。`password_env` 引用后台服务进程可读的环境变量；临时终端变量只对从该终端启动的进程有效，自启动时必须另外确保同一变量可用。本版尚未交付图形凭据管理器，不应把密码写进启动命令。

数据库用户名必须具备实际对象的读取权限。只读账号、对象/字段 allowlist、结构化查询限制共同生效。例子中的表和字段都需要替换为实际名称。数据库原始存储或备份文件不能当作已连接数据库处理。

可先生成独立演示库：`python examples/create_sample_database.py <新的绝对路径>/orders.sqlite`，把 SQLite 示例的 `path` 改成输出路径，再用 `examples/query.json` 的结构化请求验证。脚本拒绝覆盖既有文件；`examples/sample-documents` 也可直接作为首次安装的搜索目录。

语义索引还需配置 `index`：每张表的稳定唯一键 `id_column`、文本字段 `text_columns`，以及可选 `updated_column`。本版采用限额快照核对，只有完整快照完成才识别删除；超过行数预算时报告不完整，不能保证整个大表已覆盖。查询接口仍可访问允许范围内的数据。

## 卸载与数据保留

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -InstallDir 'D:\apps\data-search'
```

```bash
bash scripts/uninstall.sh
bash scripts/uninstall.sh --install-dir /home/you/.local/share/data-search/app
```

卸载先停止服务、移除本安装实例启动项，再移除程序目录；默认保留配置、模型、索引。明确加 `-DeleteData` / `--delete-data` 才删除安装清单指向且带管理标识的数据目录。外部模型和被搜索的原始文件不属于数据目录，不会被卸载器删除。

删除前校验绝对路径、安装清单、目录管理标识和符号链接；缺少标识、目录重叠或服务无法停止时拒绝危险操作。重新安装到相同程序/数据路径可以继续使用保留的配置。

## 验证边界

本机安装与检索 smoke、具体测试平台及数据库版本以主验证报告为准。Linux systemd 用户服务、其他 Windows 策略环境、目标数据库服务器版本和 8GB 实机压力测试，只有实际执行后才列为通过。脚本静态检查不能代替目标系统安装测试。多机接入将在单机验证完成后另行验证。
