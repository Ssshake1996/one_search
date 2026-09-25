# 数据库接入与范围

首版适配器：SQLite 3（普通未加密文件）、MySQL 8.4（PyMySQL）、PostgreSQL 16/17/18（psycopg 3）。版本兼容声明与实际验证分开：SQLite、MySQL 8.4.11、PostgreSQL 17.11 已完成本机真实数据库测试；PostgreSQL 16/18 尚未实测。MariaDB、SQLCipher、SQL Server 等暂不声明兼容。

数据库连接在当前单机节点执行。远程数据库连接并不等于多节点联邦检索；后者仍为后续工作。

## 用户需要提供什么

| 信息 | SQLite | MySQL / PostgreSQL |
|---|---|---|
| 数据源标识 | 唯一 `id` | 唯一 `id` |
| 类型 | `sqlite` | `mysql` 或 `postgres` |
| 地址 | 本地文件绝对路径 `path` | `host`、`port`、`database` |
| 认证 | 后台进程对文件的读取权限 | `user`、`password_env` 指向保存密码的环境变量 |
| 可见范围 | `allowed_tables`、可选 `allowed_columns` | 同左；PostgreSQL 表名必须写 `schema.table` |
| 连接条件 | 文件可读，必要时旁边的 WAL/SHM 文件也可访问 | 网络可达、相应只读权限，必要时提供 TLS 设置 |
| 语义索引 | 可选 `index` 列表 | 同左 |

`allowed_tables` 缺省为空，不会自动暴露全部数据库。只要在 `allowed_columns` 中配置了某表，该表仅开放列出的字段；空列表表示不开放任何字段。未配置该表的列清单时，允许读取该授权表的所有可发现列。因此有敏感列的表应显式配置字段清单。

不接收明文 `password` 或任意连接字符串 `dsn`；密码使用环境变量传递。配置文件不要存储密码，MCP 结果不会返回连接参数或原始数据库异常。后台服务必须能继承对应环境变量，启动后修改变量需要重启服务。

账号应由数据库管理员授予所选对象的 `SELECT` 和相应元数据可见权限，不能依靠应用内只读设置替代数据库账号权限。程序不会创建账号、修改业务数据或接受任意 SQL。

SQLite 示例（加入主配置的 `databases` 列表）：

```json
{
  "id": "local-orders",
  "kind": "sqlite",
  "path": "F:/sample-data/orders.db",
  "allowed_tables": ["orders"],
  "allowed_columns": {"orders": ["id", "description", "amount", "updated_at"]},
  "query_timeout_seconds": 5,
  "max_rows": 200,
  "max_result_chars": 200000,
  "sync": {"page_size": 250, "max_pages_per_tick": 4, "reconcile_interval_seconds": 3600},
  "index": [
    {"table": "orders", "id_column": "id", "text_columns": ["description"], "updated_column": "updated_at"}
  ]
}
```

MySQL 示例：

```json
{
  "id": "business-mysql",
  "kind": "mysql",
  "host": "127.0.0.1",
  "port": 3306,
  "database": "business",
  "user": "data_search_reader",
  "password_env": "DATA_SEARCH_MYSQL_PASSWORD",
  "ssl": {"ca": "F:/certs/ca.pem", "check_hostname": true},
  "allowed_tables": ["tickets"],
  "allowed_columns": {"tickets": ["id", "subject", "description"]},
  "index": [{"table": "tickets", "id_column": "id", "text_columns": ["subject", "description"]}]
}
```

PostgreSQL 示例：

```json
{
  "id": "business-postgres",
  "kind": "postgres",
  "host": "db.example.internal",
  "port": 5432,
  "database": "business",
  "user": "data_search_reader",
  "password_env": "DATA_SEARCH_PG_PASSWORD",
  "ssl": {"sslmode": "verify-full", "sslrootcert": "F:/certs/ca.pem"},
  "allowed_tables": ["public.tickets"],
  "allowed_columns": {"public.tickets": ["id", "subject", "description"]},
  "index": [{"table": "public.tickets", "id_column": "id", "text_columns": ["subject", "description"]}]
}
```

TLS 证书和网络通路由用户配置；示例主机名与路径需要替换。SQLite `.db` 以外的扩展名也可使用，只要它确实是 SQLite 数据库。`.sql`、`.bak`、`.mdf`、`.ibd` 不会被当成数据库服务打开。

## 受控查询

`query_database` / `DatabaseSource.query()` 仅接受结构化请求，筛选值全部使用数据库参数绑定。每个选择、筛选、排序、分组、关联及聚合字段均经过同一白名单校验。未限定表名的字段只指向主表；其他表的字段必须完整写成 `table.column`，PostgreSQL 则为 `schema.table.column`。

```json
{
  "table": "orders",
  "columns": ["id", "description", "amount"],
  "filters": [{"column": "amount", "op": "gte", "value": 100}],
  "order_by": [{"column": "amount", "direction": "desc"}],
  "limit": 20,
  "offset": 0
}
```

筛选操作：`eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`in`、`not_in`、`between`、`contains`、`starts_with`、`is_null`、`not_null`。多个条件使用 AND。文本搜索中的 `%`、`_`、`!` 视为普通字符，不是用户指定的通配符。排序方向只接受小写 `asc`、`desc`。

```json
{
  "table": "customers",
  "columns": ["name"],
  "joins": [{"table": "orders", "left": "customers.id", "right": "orders.customer_id", "type": "left"}],
  "aggregates": [
    {"function": "sum", "column": "orders.amount", "alias": "total"},
    {"function": "count", "column": "orders.id", "alias": "number"}
  ],
  "group_by": ["name"],
  "order_by": [{"column": "total", "direction": "desc"}],
  "limit": 20
}
```

支持最多四个 `inner`/`left` 等值关联，以及 `count/sum/avg/min/max` 聚合。每个非聚合选择列必须出现在 `group_by` 中。`count(*)` 只接受 `{"function":"count","column":"*","alias":"n"}` 表示。暂不支持自关联、子查询、窗口函数、HAVING、任意表达式及自定义 SQL。限制是为了让接口能力和权限检查保持明确。

查询结果包含 `source_id`、`columns`、`rows`、`row_count`、`truncated`、`elapsed_ms`、`queried_at`。超过行数或响应字符预算会设置 `truncated=true`。`Decimal` 和日期时间转为字符串以保留精度；二进制值表示为 `{"base64":"..."}`。offset 分页在源数据变化时不保证跨请求快照一致；建议显式提供唯一排序。

默认每次最多返回 200 行，调用方默认取 20 行；配置硬上限 10,000 行。响应字符预算默认 200,000，最高 2,000,000。单个过大记录可能使结果为空但 `truncated=true`，可缩小选择列后重新查询。MySQL 使用流式游标，PostgreSQL 使用服务器端游标、每批 16 行，避免先缓存整个结果。响应字符限制在序列化结果前检查，但它不是网络传输的硬字节上限：普通查询中的单个巨大字段仍会先进入驱动内存，因此主服务应在受限子进程中运行数据库工作。正文索引另有 SQL 端文本截断。

查询使用真实只读连接/事务：SQLite `mode=ro` 与 `query_only`；MySQL 只读事务、`MAX_EXECUTION_TIME` 及网络超时；PostgreSQL 只读事务、`statement_timeout` 和 `lock_timeout`。每个适配器实例最多一个活动连接。服务级别仍需对整个进程设置资源限制；数据库时间限制不能承诺限制扫描字节数或服务器全部资源。只读视图也可能很昂贵，应仅开放经过评估的视图。

## 数据库正文索引与持续同步

`index` 每个表只允许配置一项：

- `table`：授权的真实表。普通视图仍可结构化查询，但持续正文索引要求可验证的唯一键，暂不接受视图。
- `id_column`：已授权、非空、稳定的单列标识。必须有单列主键或非部分 UNIQUE 索引；组合键、部分唯一索引不足以证明整张表唯一。字段应为整数、短字符串、UUID 等可排序标量，字符串最多 2,048 字符。业务方应保持记录 ID 不变。
- `text_columns`：1–50 个授权字段，建议只选正文。
- `updated_column`：可选，必须授权且非空。源系统应在每次新增/更新时维护该字段，可用递增整数、日期时间，或格式统一、字典序与时间序一致的字符串。不接受布尔、二进制、JSON、数组或非有限数。建议日期时间至少使用微秒精度。

较大表应由数据库管理员预先建立 `(updated_column, id_column)` 索引。插件以只读账号运行，不会替用户创建数据库索引。只有主键但缺少水位复合索引时，SQL 仍可能在业务数据库内排序或扫描大量行；分页限制本地传输和驻留数据，不保证任意源表都低成本。

同步预算在每个数据库配置下设置：

```json
"sync": {
  "page_size": 250,
  "max_pages_per_tick": 4,
  "reconcile_interval_seconds": 3600
}
```

`page_size` 为 1–1,000，`max_pages_per_tick` 为 1–100；后台一轮默认最多读取四页、约 1,000 行，再让出资源。未完成的表在后续轮次继续，多表轮转，避免大表长期阻塞其他表。默认完整巡检间隔 3,600 秒，允许设置不小于 1 的整数；实际执行时点受后台轮次间隔、暂停和资源预算影响。完整扫描尚未完成时先续传，不会因巡检到期而从第一页重来。旧 `index_max_rows` 仍接受 1–10,000，但其含义改为**每轮行预算**，不再永久截断全表。

首次扫描按 `id_column` 做 keyset 分页，不使用 OFFSET。开始时保存本轮最大主键及最大水位，避免新增记录使一轮扫描永远无法结束。每页成功写入本地正文、块和版本后，才保存游标；中断或页面失败可重新读取该页。配置指纹变化时丢弃旧游标，重新执行完整扫描。游标、扫描代次和进度保存在本地索引库，重启继续；不是仅存内存的任务。

有 `updated_column` 时，首次完整扫描后改按 `(updated_column, id_column)` 分页读取增量，并固定本轮上界。为捕获同一时间戳内的更新，下轮重新读取最后一个水位值的全部记录，再按 ID 在页间推进。若大量记录共享同一水位值，这个相等组会重复读取；请使用实际维护、精度足够的水位字段。低于已保存水位的补写/回填由周期性完整巡检发现，不能承诺立即发现回拨时钟或历史时间更新。

没有 `updated_column` 时，每轮完整遍历结束后，下一次后台轮次开始新的完整遍历，通过正文哈希识别变化；仍然分批运行。水位增量无法证明删除，因此仅在该表完整扫描全部页成功后清理本轮未出现的旧结果，清理自身也分批保存进度。超时、权限错误、无效键、水位错误、资源暂停或进程退出期间不会根据缺席删除旧结果。空表也先确认完整扫描成功，再清理缓存。

每个页面使用独立的短只读事务，页面之间没有长事务快照。这是**最终一致性**同步，不是 CDC：源数据在扫描期间变化可能在下次增量或完整巡检中才体现。删除核对、迟到更新和没有水位的更新，其时效包括扫描整个授权表的时间，不能将一页耗时当成全表同步时效。MySQL 建议使用 InnoDB。

`index_status` 和 `inspect_source` 返回 `database_sync`。按数据源和表展示 `phase`（`scanning` / `reconciling` / `idle`）、`mode`（`full` / `incremental`）、`scanned_rows`、`pages`、`deleted_rows`、`cursor`、`watermark`、`last_full_at` 和 `last_error` 等。`scanned_rows` 是本轮累计行数；不额外执行昂贵的全表 COUNT，因而没有虚构百分比。正常的未完成分页显示进度，不记作错误。

每个正文单元格在 SQL 端最多读取 12,001 字符，每条记录最多保留 12,000 字符，超出时 `locator.truncated=true`。稳定 key 由来源 ID、表、标识列和标识值计算，内容版本对实际保留正文计算 SHA-256，截断部分的变化不会触发正文更新。只在哈希变化时重建文本块和后续嵌入。

`DatabaseSource.index_page()` 是后台分页接口。返回当前页 `documents`、`next_cursor`、固定的 `boundary`、`complete`；`complete` 只表示当前表当前周期已读完，调用方仍需提交本页和执行受限删除核对。旧 `iter_documents(max_rows=...)` 保留供兼容和有界快照调用，后台持续索引已不再使用该一次性截断接口。

## 验证记录

执行命令：`.venv/Scripts/python.exe -m pytest tests/test_databases.py -q`。

SQLite 真实临时数据库测试覆盖：结构发现、列/表权限、参数化输入、筛选语义、关联与聚合、分页、只读实际执行、慢查询超时、结果容量、稳定 key、更新/删除、完整与不完整快照、空表、重复/null 标识、标识符引用及异常脱敏。测试只使用自造数据。v0.2 数据库单元与 Engine 集成测试覆盖超过 1,000 条记录分轮扫描、重启续传、水位相等组、增量更新/新增、删除核对、清理阶段重启、配置变化、页面失败与部分页面写入后重放。精确计数见 `validation/databases-v0.2.json`。

2026-09-25，在 Windows 本机下载官方便携包 MySQL 8.4.11 与 PostgreSQL 17.11，仅绑定 loopback 随机端口运行合成测试库，创建只读测试账号。两种真实服务端集成用例验证结构发现、白名单、筛选中的文字通配符转义、关联聚合、分页、实际写操作拒绝，以及完整/截断正文快照。v0.2 还实测两者的 keyset 分页、微秒时间戳参数绑定、进程重启游标续传、同时间戳更新重放和完整巡检后的删除清理。进程在测试后关闭；未创建系统服务。

可重跑的服务端用例为 `test_service_database_integration`：仅在显式指定 `DATA_SEARCH_TEST_MYSQL_CONFIG` / `DATA_SEARCH_TEST_POSTGRES_CONFIG` 时运行，否则显示 skipped。配置指向专门的合成测试库，绝不默认连接实际业务数据库。测试工具及详细输出放在工作区忽略目录 `.test-databases/`，不进入产品安装包。

尚未验证的项目不能视为已通过：PostgreSQL 16/18、真实 TLS/证书认证、VPN/SSH 通路、业务库权限差异、长时间高并发、CDC 和多机联邦检索。

官方机制参考：[SQLite URI 只读模式](https://www.sqlite.org/uri.html)、[SQLite 进度中断](https://www.sqlite.org/c3ref/progress_handler.html)、[MySQL 系统变量](https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html)、[PostgreSQL 连接参数](https://www.postgresql.org/docs/17/libpq-connect.html)。
