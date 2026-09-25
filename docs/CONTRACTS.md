# Implementation contracts · v0.2

This document describes implemented single-node behavior, not a promise of validation on every platform. See [validation](VALIDATION.md) for executed tests and [roadmap](roadmap/README.md) for outstanding acceptance work. Source runtime requires Python 3.11+; the Windows native distribution bundles its own runtime.

## File scope and indexing tiers

`defaults(data_dir, roots=None)` creates `scope: "machine"`, `roots: []`. An explicit roots list creates `scope: "directories"`; a saved legacy configuration without `scope` retains its old roots and does not expand to machine scope. Machine scope and nonempty configured roots are mutually exclusive.

Windows machine discovery includes fixed local volumes readable by the current user, without elevation. Network/removable/optical volumes are skipped. Linux discovers local mounts, excluding known remote/virtual filesystems and `/proc`, `/sys`, `/dev`, `/run`; Linux hardware behavior remains unverified. Scope refreshes before full scans. Effective roots, excluded paths/names, skipped/unavailable volumes and discovery failures are observable through `file_scope`.

Directory aliases, symlinks/reparse directories, program/runtime trees, the data directory, the model directory, `exclude_paths` and `exclude_names` are excluded. Configuration revocation removes cached evidence in bounded batches; query/fetch still validate current authorization. This is not privileged filesystem access or NTFS MFT/USN indexing.

`indexing` contains two independently selectable tiers:

```json
{
  "content_scope": "all",
  "content_roots": [],
  "content_extensions": [],
  "semantic_scope": "all",
  "semantic_roots": [],
  "semantic_extensions": []
}
```

Each scope accepts `all|directories|none`. Nonempty extension lists contain dot-prefixed extensions. Filename metadata covers the effective file scope; content/semantic settings can only narrow processing. Semantic indexing requires extracted content. Database text is selected by its own allowlisted `index` configuration and is unaffected by file root/extension filters; `semantic_scope=none` excludes database embeddings too. Scope changes reconcile stored chunks and embedding eligibility.

Whole-machine scans and Linux use periodic traversal. Selected Windows directories use recursive native watchers only when effective root count is 1–32; watcher failures fall back to periodic traversal. No watcher-per-directory setup across all disks. A scheduling interval is not a guaranteed freshness SLA: scanning and backlog time are additional.

## Extraction

`extract(path: str, max_chars: int = 2_000_000) -> dict` returns:

```json
{"status":"ready|unsupported|encrypted|error|partial","reason":null,"chunks":[{"text":"...","locator":{}}],"truncated":false}
```

Extractor chunks are at most 1,200 characters, with JSON locators such as line range, page, paragraph, sheet/cells, slide/notes or record/columns. Engine groups compatible context and creates search chunks of at most 350 characters with a 50-character overlap. It must not invent unsupported locators such as Word page numbers.

Extraction does not execute document code or follow external references. Input size, characters, archive expansion and parser duration are bounded. Isolated workers are subject to the controls below; their limits do not guarantee an exact process-tree RSS ceiling. File format coverage is defined in [FORMATS.md](FORMATS.md).

## Store and vectors

`documents` and `chunks` are canonical content; external-content FTS5 tables index tokenized chunks and file paths without storing another content copy. Triggers maintain postings. Opening an older store migrates the FTS structure as necessary. Chunk and document IDs are monotonic SQLite AUTOINCREMENT identities so a removed evidence ID cannot silently identify a new record.

Embeddings are finite 512-dimensional vectors keyed by text hash/model. New SQLite blobs use float16; the reader also accepts old float32 blobs. `compact` is explicit offline maintenance: with the daemon instance lock held, convert old blobs, optimize FTS, checkpoint and VACUUM. It requires additional disk budget and does not run during active service use.

USearch HNSW uses float16 and immutable versioned snapshot files. Production synchronization runs in a monitored disposable worker, separate from query and SQLite writer locks. It reads a consistent SQLite snapshot, applies changes in bounded batches and publishes a small manifest atomically only after saving the complete ANN snapshot. The prior published snapshot stays queryable while a replacement is built; failed/cancelled builds do not publish partial indexes. Readers validate manifest/schema/model/file metadata before mapping a snapshot. Cache validation is not a cryptographic authenticity guarantee.

`vector_index` reports `building`, `published_chunks`, `pending` and `last_sync`. Searches use the latest successfully published snapshot. An older generation produces `semantic_index_updating`; no usable snapshot produces `semantic_index_pending`. Search does not start an ANN rebuild. Current document IDs, source permissions and indexing tier eligibility are rechecked before returning evidence from old snapshots. Publication and large native operations are not guaranteed to be instantaneously preemptible.

## Query selection and evidence

Filename/keyword/semantic/hybrid modes use bounded candidates. Initial count is `max(128, limit * 5)`; when candidates are exhausted by filtering or document deduplication, Engine doubles the count up to 8,192. Evidence rows are fetched in batches and revalidated, rather than one SQL lookup per candidate. Keyword source/extension restrictions apply before ranking; ANN restrictions apply after global nearest-neighbor selection. Adaptive expansion improves the opportunity to find eligible documents but cannot guarantee exhaustive filtered ANN recall. `candidate_limit_reached` makes the cap visible.

File fetch reads cached chunks and exposes staleness relative to the source. `c:<chunk>` anchors context at that fragment; `d:<document>` starts from the document beginning. Database fetch reads the current authorized row live. Cached database snippets are explicitly snapshots until fetched. Scores are ranking signals, not answer confidence; results retain `evidence_only` and coverage information.

## Database synchronization

Database configuration: `id`, `kind: sqlite|mysql|postgres`, file `path` or `host/port/database/user/password_env`, optional engine-specific TLS, `allowed_tables` and optional `allowed_columns`. PostgreSQL table names are `schema.table`. Password values and arbitrary DSNs are not accepted in configuration or returned in results. Session settings are read-only, and every selected/filter/join/order/group column is allowlisted. The public query method accepts structured requests, never raw SQL. Inspection lists only authorized objects.

`DatabaseSource(config)` exposes:

- `inspect() -> dict`: allowed structure and server version.
- `query(request) -> dict`: bounded structured results, truncation and query time.
- `index_page(entry, *, mode="full", after=None, boundary=None, watermark=None, page_size=250) -> dict`: one bounded keyset page.
- `iter_documents(max_rows=10000)`: retained legacy bounded snapshot iterator; not the Engine's continuous synchronization path.

`entry` must equal a configured `index` specification: `{table,id_column,text_columns,updated_column?}`. Continuous indexing requires a real table with a single-column primary key or non-partial UNIQUE index and non-null ordered identity values. Composite/partial keys and ordinary views cannot establish this contract. Views remain usable for structured queries. Watermarks must be authorized, non-null ordered scalar values maintained by the source. Source indexes on `(updated_column,id_column)` are recommended for large incremental scans; the read-only plugin never creates them.

Page output is `{documents,next_cursor,boundary,complete}`. A full cursor is the ID; an incremental cursor is `[updated_value,id]`. The fixed boundary contains the initial maximum ID and `[updated_value,id]`, making a cycle finite under ongoing inserts. Incremental selection replays the last equal-watermark group on the next cycle, then uses strict tuple advancement within the cycle. Per-page text transfer is SQL-truncated and each retained record is at most 12,000 characters.

Per-source `sync` defaults: `page_size=250` (1–1,000), `max_pages_per_tick=4` (1–100), `reconcile_interval_seconds=3600` (integer >=1). Legacy `index_max_rows` remains an optional 1–10,000 row budget **per tick**, not a total table cap. Tables rotate across ticks. Engine persists source configuration fingerprint, per-table generation/mode/phase, cursor, boundary, watermark, counts and errors in local settings. A configuration change invalidates checkpoints.

A page cursor is committed only after all documents/chunks are durable; version stamps are published after chunk writes so an interrupted page is replayable. Full scans set generation markers and only enter deletion reconciliation after every page succeeds. Cleanup is itself bounded and resumable. Errors, pauses and incomplete pages never authorize absence-based deletion. With a watermark, later cycles are incremental plus periodic full reconciliation; without one, the next tick after a completed full cycle starts another full cycle.

Each page uses a short read-only source transaction, not one transaction spanning the entire cycle. This is eventually consistent and not CDC. Backdated updates, deletes, equal timestamps and mutable IDs have the limits documented in [DATABASES.md](DATABASES.md). `database_sync` exposes per-source/table progress in status and inspection; no expensive total-row count or invented progress percentage is required.

## Resource controls

Defaults: one indexing task; semantic threads 1 (maximum 2), batch 8 (maximum 32); process-tree sampled RSS budget 1,024 MiB, minimum available memory 768 MiB; index/model disk budget 10,240 MiB, minimum free disk 1,024 MiB; parser input 32 MiB, 2,000,000 characters, 30 seconds; idle model release 120 seconds; batch sleep 50 ms.

Windows workers attempt below-normal CPU priority, low I/O priority, restricted CPU affinity and a Job Object. `worker_memory_mb=512` caps per-process **committed virtual memory**, not RSS. `worker_cpu_percent=25` sets a CPU rate cap when supported; it is relative to the system/enclosing Job allocation. Job attachment or CPU-policy failure is reported rather than represented as successful enforcement. Sampling of the total process tree remains a separate safeguard, not an OS-enforced aggregate memory limit.

Child environments explicitly set `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS` and `NUMEXPR_NUM_THREADS` before native-library imports. ONNX and ANN calls also receive bounded thread settings. Linux attempts nice/I/O priority and affinity, with sampled memory/disk checks only; no Linux cgroup hard memory/CPU implementation is claimed. `worker_controls` records actual settings, metric, active state and fallback errors. The service's own memory and filesystem cache are not covered by per-worker Job caps.

## CLI, daemon and settings

All normal CLI commands require `--config PATH`, accepted after the subcommand:

```text
data-search init --data-dir PATH [--root PATH ...] [--exclude PATH ...] --config PATH
data-search daemon|start|stop|status|scan|pause|resume --config PATH
data-search search QUERY --mode hybrid|keyword|semantic|files --config PATH
data-search fetch ID --offset N --limit N --config PATH
data-search inspect [--source ID] --config PATH
data-search query --source ID --request JSON_OR_@FILE --config PATH
data-search model-download|compact|mcp --config PATH
```

`init` never overwrites a saved configuration. Omitted roots select new machine scope. `daemon` runs foreground loopback RPC; `start` detaches and deduplicates under an instance lock. `stop` is idempotent. `status` exits 0 for a running service and 1 on failure/unavailable service; successful/already-stopped `stop` exits 0. `scan` acknowledges scheduling, not completion. Multiple MCP bridges share the service; ending stdio does not stop it.

Native Windows launcher `data-search.exe setup --config PATH` (or no arguments) opens the local Tk settings window. Python installations use `python -m data_search.setup_ui --config PATH`; normal Python CLI has no `setup` subcommand. The GUI validates editable configuration, preserves advanced fields, stops the existing service, atomically saves and restarts. It offers scope/tier/database settings and service/model controls, but is not a credential vault and does not certify remote database connectivity when saving.

## Packaging and lifecycle

Windows native bundle includes CPython and dependencies; bootstrap includes platform/Python-specific dependency wheels and requires the matching installed interpreter. The Windows py311 amd64 bootstrap requires CPython 3.11 x64. Source supports Python 3.11+; this release has no validated Linux native runtime. Models are downloaded at install or supplied as a verified external directory; not bundled by default.

Installers initialize new machine scope unless explicit roots are given, preserve existing configuration on upgrade, exclude installation/data/model directories, prepare the model and start once. Windows registers optional current-user Run autostart; Linux uses optional systemd user service and explicitly rejects unavailable systemd unless manual startup is requested. Native `Settings.vbs` opens the GUI. Absolute executable/config paths are generated in `mcp.json` and the installed plugin `.mcp.json`; no unknown host configuration or global marketplace is modified. Uninstall stops the service and removes its autostart/application, preserving data by default. Explicit delete-data is restricted to verified managed paths.

## Reserved multi-node protocol

The five MCP methods accept optional `node_id`. Omission selects the current node; any different ID returns an explicit remote-not-implemented error. Evidence includes `node_id`, `source_id`, `id`, `document_id`, locator, timestamps/staleness and coverage. IDs are local to a node/data directory; future coordinators must preserve `(node_id, source_id, id)`.

The boundary is `dispatch(method, params) -> dict`, currently exposed through authenticated loopback `/rpc` and the official MCP stdio SDK bridge. `nodes: [{id,transport:"local"}]` is reserved; non-local transports are rejected. Remote discovery, credentials, TLS, federation/ranking and partial-node failure handling are pending separate implementation and acceptance, not enabled by pointing an existing token at another machine.
