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

## 数据库正文索引及快照协议

`index` 每个表只允许配置一项，必须包含：

- `table`：授权的表或视图。
- `id_column`：允许读取的、非空且唯一的稳定标识字段。首版不支持组合键；可提供业务侧已有的唯一字段或经过授权的视图字段。
- `text_columns`：1–50 个授权字段，建议只选正文类型。
- `updated_column`：可选且必须授权。首版仍使用有界完整快照与内容哈希核对，不会因为填了该字段就宣称已经启用 CDC 或增量 SQL。

每次在同一个只读事务中顺序读取配置的表，按唯一标识排序。适配器直接调用默认最多 10,000 条；后台服务为节省内存，默认每个数据源最多 1,000 条，可通过该数据源的 `index_max_rows` 设置为 1–10,000。超限显示覆盖不完整，不会自动翻页遍历全部业务数据。每个正文单元格在数据库端最多读取 12,001 字符，每条记录最多生成 12,000 字符正文；超出时 `locator.truncated=true`。变化检测对实际保存的正文计算哈希，超出截断范围的变化不会触发正文更新。

协议示例：

```json
{"kind":"document","key":"db:<sha256>","table":"orders","text":"description: 服务器费用优化","version":"<sha256>","locator":{"source_id":"local-orders","table":"orders","id_column":"id","id":1,"columns":["description"],"truncated":false}}
{"kind":"snapshot","complete":true,"tables":["orders"],"row_count":1,"reason":null}
```

稳定 key 由数据源 ID、表名、标识列名和 JSON 序列化后的标识值计算。`version` 是保存文本的 SHA-256。最后一个 `snapshot` 标记是完整性的唯一凭据：

- `complete=true`：配置范围的所有结果已读取，可以在该范围内删除本轮未出现的旧索引记录。
- `complete=false`：达到上限、超时、权限错误、重复/null 标识或连接失败。可以更新已读取记录，但不得根据缺席清理旧记录。
- 没有最终标记（消费者提前停止、进程退出等）：视为不完整。
- 零条记录仍会发出完整性标记，允许确认空表后清理旧记录。

限额以内的快照每轮会读取完整配置范围；它不会把任意规模的表伪装成廉价增量同步。较大的表应使用数据库侧已有的范围视图、减少正文索引范围，或等待后续游标/CDC 适配。MySQL 一致快照只对支持事务快照的存储引擎成立；生产建议使用 InnoDB。查询结果的内容保持原始数据含义，索引文本只是检索材料。

## 验证记录

执行命令：`.venv/Scripts/python.exe -m pytest tests/test_databases.py -q`。

SQLite 真实临时数据库测试覆盖：结构发现、列/表权限、参数化输入、筛选语义、关联与聚合、分页、只读实际执行、慢查询超时、结果容量、稳定 key、更新/删除、完整与不完整快照、空表、重复/null 标识、标识符引用及异常脱敏。测试只使用自造数据。含两个服务端集成用例共 42 项通过（1.19 秒，仅测试执行时间，不包含数据库下载和初始化）。

2026-09-25，在 Windows 本机下载官方便携包 MySQL 8.4.11 与 PostgreSQL 17.11，仅绑定 loopback 随机端口运行合成测试库，创建只读测试账号。两种真实服务端集成用例验证结构发现、白名单、筛选中的文字通配符转义、关联聚合、分页、实际写操作拒绝，以及完整/截断正文快照。进程在测试后关闭；未创建系统服务。

可重跑的服务端用例为 `test_service_database_integration`：仅在显式指定 `DATA_SEARCH_TEST_MYSQL_CONFIG` / `DATA_SEARCH_TEST_POSTGRES_CONFIG` 时运行，否则显示 skipped。配置指向专门的合成测试库，绝不默认连接实际业务数据库。测试工具及详细输出放在工作区忽略目录 `.test-databases/`，不进入产品安装包。

尚未验证的项目不能视为已通过：PostgreSQL 16/18、真实 TLS/证书认证、VPN/SSH 通路、业务库权限差异、长时间高并发、CDC 和多机联邦检索。

官方机制参考：[SQLite URI 只读模式](https://www.sqlite.org/uri.html)、[SQLite 进度中断](https://www.sqlite.org/c3ref/progress_handler.html)、[MySQL 系统变量](https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html)、[PostgreSQL 连接参数](https://www.postgresql.org/docs/17/libpq-connect.html)。
