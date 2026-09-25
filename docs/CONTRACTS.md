# Implementation contracts

Python 3.11; `src/data_search` package contains configuration, daemon, index, isolated workers, CLI and the official MCP SDK bridge. This document records the single-node protocol and extension boundaries.

## Extraction (extractors.py)
`extract(path: str, max_chars: int = 2_000_000) -> dict` returns
`{"status": "ready|unsupported|encrypted|error|partial", "reason": str|null, "chunks": [{"text": str, "locator": dict}], "truncated": bool}`.
Chunks <=1200 characters, preferably paragraphs/lines/rows. Locators JSON serializable: line_start/end, page, paragraph, sheet/cells, slide/notes, record/columns as appropriate.
Extraction MUST NOT execute document code or follow external references. Main monitors extraction in a disposable child process with timeout and sampled RSS budget checks; this is not an OS hard memory cap. Large individual file bytes are checked before extraction; the extractor also bounds characters and archive expansion. Optional imports are lazy.

## Database (databases.py)
Config entries have id, kind (`sqlite|mysql|postgres`), path or host/port/database/user/password_env, optional ssl options, allowed_tables (SQLite/MySQL `table`; PG `schema.table`), allowed_columns mapping. Secrets only via env/credential refs, never returned. Class `DatabaseSource(config: dict)` exposes `inspect() -> dict`, `query(request: dict) -> dict`, `iter_documents(max_rows: int = 10000) -> Iterator[dict]`.
Query is structured (table, columns, filters list {column,op,value}, order_by list {column,direction}, limit, offset, optional joins/aggregates). No untrusted raw SQL execution. Read-only engine/DB session settings and allowlist enforcement for every selected/filter/join/sort column. Inspection only lists allowed objects. Limits/timeouts enforced. iter_documents only configured `index` tables [{table, id_column, text_columns, updated_column?}], returns stable key/table/locator/text/version. Bounded snapshot comparison in core handles updates/deletes only when full snapshot completed; source marks incomplete scans.

## CLI (root)
`data-search init --config PATH --root PATH --data-dir PATH` creates config, `daemon --config PATH` foreground loopback service; `start --config PATH` detached; `stop|status|scan|pause|resume --config PATH` client; `search QUERY --mode hybrid|keyword|semantic|files --config PATH`; `mcp --config PATH` stdio SDK bridge; `model-download --config PATH` downloads pinned local ONNX assets; `inspect --source ID --config PATH`; `query --source ID --request JSON --config PATH`. Global flags accepted after subcommand.
JSON config: node_id, data_dir, roots list, scan_interval_seconds=180, resource {memory_mb=1024,min_available_mb=768,max_disk_mb=10240,min_free_disk_mb=1024,workers=1}, extraction {max_file_mb=32,max_chars=2000000,timeout_seconds=30}, semantic {enabled=true,model_dir=<data>/models/bge-small-zh-v1.5,threads=1,idle_seconds=120,batch_size=8}, databases list. Unknown node IDs return unsupported; no remote implementation this release.

## Packaging
Windows/Linux install scripts create isolated environment, install wheel/source, initialize config for EXPLICIT roots, prepare model, register user-level autostart where available and start. Model download flags and offline model path allowed. Never default to whole disk scan. `--config` is an absolute path. Installation must not overwrite existing config or start duplicate daemon. Package the plugin shell at `plugins/data-search` (folder and manifest name normalized); generic stdio MCP is usable by compatible clients including DSH.

## Reserved multi-node protocol

All five MCP methods accept optional `node_id`. Omission selects `config.node_id`; any different ID is rejected with an explicit remote-not-implemented error. File/content search evidence contains `node_id`, `source_id`, `id`, `document_id`, `locator`, `indexed_at`, `stale` and coverage. IDs are local to a node and data directory, and must not be treated as globally unique. A future coordinator must retain the tuple `(node_id, source_id, id)`.

The local transport is `dispatch(method, params) -> dict`, exposed through a small authenticated loopback `/rpc` adapter. MCP clients do not depend on SQLite internals. Future transports can implement the same request/reply boundary while preserving source identifiers, errors, completeness and freshness. Node discovery, remote credentials, TLS, federated ranking, disconnected-node behavior and cross-machine access policy remain separate future work. The current local token is not a remote authentication design.

Configuration reserves `nodes: [{id, transport: "local"}]`. A non-local transport is rejected rather than silently ignored. No port is bound to non-loopback addresses. Exact multi-node network fields are deliberately not invented before that phase is designed and verified.

## Limits carried in the interface

File fetch reads cached indexed chunks and reports whether the underlying file changed. `c:<chunk>` anchors the starting fragment; `d:<document>` starts from the document beginning. Database fetch reads the currently authorized row live. Similarity scores are ranking signals, not answer confidence. Filters on ANN results are applied after bounded nearest-neighbor selection and may underfill results; lexical filters apply before ranking.

File updates use watched paths and periodic reconciliation. Database indexing uses bounded snapshot hashes, not CDC: service default `index_max_rows=1000`, maximum 10000 per source. Only a terminal `complete=true` snapshot permits absence-based deletion. Text truncation, source failures and incomplete coverage must remain visible in MCP results.
