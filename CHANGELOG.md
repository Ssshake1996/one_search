# Changelog

## 0.4.0

### Installation and first usable search

- README is the Agent installation contract, with platform prerequisites, release verification, exact parameters, exit/status interpretation and recovery steps. New installs default to the whole accessible local machine; optional exclusions and low/balanced/fast presets are applied only on first configuration, and repeated installation preserves saved settings.
- Basic daemon search starts before detached model preparation. Download/import jobs expose progress, verification, cancellation, interruption and retry; completed pinned assets are reused, failures do not block filename/keyword search, and model changes after verification can be repaired. Installation reports distinguish runtime, service, basic search, semantic readiness and DSH connection checks.
- DSH registration stages the bundle on the DSH-home volume to fix the observed Windows cross-drive dependency failure. Activation checks backend compatibility, upgrades older managed backends only through a supplied compatible release installer, and clearly identifies explicit backends requiring manual update. Shared-client registrations and an isolated 11-tool connection check are included.

### Search, evidence and recovery

- Directory/date/size/category/multiple-extension filters, effective-filter output, exact-filename preference and lexical sorting help users narrow results. Optional duplicate folding compares complete extracted text among returned candidates and does not infer an approved version.
- The MCP surface expands from five to 11 tools with path diagnosis, bounded context/citations, explicit refresh/priority and persistent timed pause/resume. Source opening is an explicit settings/CLI action; source text remains untrusted evidence.
- Filesystem identity checks reject replaced or unverified legacy files. v0.3 files are re-associated and parsed under existing budgets; old IDs cannot silently redirect to replacement content. Miss diagnostics distinguish unseen, excluded, stale, pending, unsupported, encrypted, partial and resource-limited cases.
- Settings expose search/preview, exclusion impact, model jobs, resource presets, database table/column selection and operational status. CPU/battery/idle/AC policies defer background work, while user pause and automatic waits remain separate and retrieval stays available.

### Databases and maintenance

- Bounded metadata discovery proposes explicit table/column selections, stable key/watermark candidates, realtime-only versus indexed text and business metadata. Read-only preflight still validates the exact pending configuration before activation.
- Windows Credential Manager and optional Linux Secret Service store database passwords outside configuration. Rotation and unavailable credentials produce structured diagnostics; no plaintext fallback or credential export is introduced.
- Index relocation uses ownership markers, held leases, copy verification, atomic configuration switch and retained originals. Native upgrade snapshots include external indexes and reacquire locks before rollback. Install/upgrade/uninstall coordination includes detached model jobs.
- Space breakdown, recognized backup cleanup, configuration export/restore without credentials, shared-client impact, autostart control and explicit preserve/delete-data removal complete the local maintenance workflow. Source files are outside generated-index cleanup.

### Verification boundaries

The release remains a preview. A real installed DSH headless model session executed status/search/fetch calls on synthetic files, cited approved versus draft values and limited its no-answer claim to indexed scope; see [answer evidence](docs/validation/dsh-answer-v04.json). This is one prompted scenario, not broad answer-quality acceptance. Final test/build/install evidence, actual hardware/corpus sizes and remaining Linux/physical low-memory/large-real-corpus limits are recorded in [validation](docs/VALIDATION.md). Multi-machine transport and conditional OCR/legacy Office/archive-body expansion remain outside this release.

## 0.3.0

### Durable indexing and retrieval

- File-name discovery and body parsing use separate persistent work queues. Interrupted batches resume after restart, and known coverage, pending work and bounded errors are visible in status.
- Windows can consume an existing accessible NTFS USN journal for change discovery. Missing permissions, journal resets and unresolved events fall back to reconciliation; first discovery still walks the selected scope and does not enumerate the MFT.
- ANN updates publish immutable segments incrementally and merge them under resource limits, retaining the previous usable publication until the replacement is ready.
- Short Chinese filename matching, structural text chunks and filtered vector scoring preserve source locations. Existing file chunks migrate through bounded persistent jobs; database text receives one full paged refresh. Quality and scale claims remain tied to the recorded evaluation corpus.

### Installation and day-to-day operation

- Native Windows installs verify and stage the runtime before replacing it. Upgrades stop the service, snapshot the pre-migration index and configuration, and retain the old runtime. An immediate startup failure restores the snapshot only after the instance lock is available; later automatic downgrades are not attempted.
- Upgrade snapshots are retained under `.upgrade-*` and require additional space. Only verified pinned model assets and transient service files are excluded; unknown files under the model directory are backed up. Python bootstrap and Linux installs do not yet use the native runtime transaction.
- The settings window exposes memory, worker CPU and disk budgets, known document/embedding counts, pending queues and source errors. Changed database settings require an explicit successful read-only preflight of that same configuration before activation.
- Database preflight runs with bounded subprocess timeouts and checks allowed fields, stable unique keys and watermarks without returning row contents or credential values. Failed checks leave the running configuration intact.
- A DeepSeek Harness Cordis bundle registers through `dsh.bundle.patch` and uses the official MCP client. Its first profile activation installs a missing local backend; package registration alone does not start it. Existing backends and settings are preserved.
- An optional helper merges the installed MCP entry into an explicitly selected standard JSON host configuration, preserving unrelated settings and retaining a backup. DSH uses its separate Cordis integration.

### Verification boundaries

See the version-specific [validation report](docs/VALIDATION.md) for executed tests and artifact checks. Model weights remain separate from the release, Linux target-host installation and physical low-memory-machine acceptance remain pending, and multi-node transport is not implemented. Checksums detect changed bundle contents but are not a publisher signature.

## 0.2.0 — 2026-09-25

### Whole-machine scope and installation

- New installations discover accessible local fixed disks on Windows and local filesystems on Linux. Selected-directory mode remains available; legacy configurations keep their previous scope.
- File names, body text and semantic indexing can have different directory and extension ranges. Scope exclusions, unavailable roots, bounded scan error samples and actual coverage are visible in status.
- Windows native ZIP includes its Python runtime and dependencies. The installer starts the local service and registers current-user login startup. A local settings window supports directory selection, database configuration and service controls.
- The Python 3.11 x64 bootstrap ZIP, universal project wheel, release manifests and SHA-256 checksums are also provided. Models are downloaded on first installation or supplied separately.

### Indexing and retrieval

- External-content FTS avoids duplicate stored token/path text. New cached embeddings use float16, while old float32 vectors remain readable. Offline `compact` converts and compacts legacy storage under the service lock.
- ANN construction runs in a disposable worker and publishes immutable snapshots atomically. Queries keep using the previous published snapshot, checking current source permissions and document IDs.
- Interrupted publications retain the previous snapshot; orphaned files are reclaimed. Failed FTS migrations roll back to the old usable indexes.
- Chinese and spaced Windows program/data/model paths are supported: installers decode JSON as UTF-8; ANN workers publish with relative ASCII filenames and daemon readers use Unicode-safe memory maps.
- Search expands insufficient candidate sets up to 8,192, batches evidence reads and removes duplicate documents. Stale semantic candidates cannot restore revoked semantic scope.
- Database text indexing now uses keyset pages, persistent checkpoints, optional timestamp watermarks and periodic full reconciliation. A per-tick row budget no longer limits total table coverage. Deletions are applied only after a complete full scan.
- Workers use lower scheduling priority, bounded native-library threads and CPU affinity. Windows Job Objects additionally limit per-worker committed virtual memory and CPU rate where available. Sampled process-tree RSS and disk budgets remain in place.

### Verification boundaries

Windows 11, SQLite and isolated MySQL 8.4.11/PostgreSQL 17.11 fixtures have execution evidence. Linux scripts and mount discovery are implemented but target-host installation is unverified. Physical 8GB/16GB machines, hundreds of GB of real extracted content, long-running production databases, host-specific plugin-store hooks and multi-node operation remain outside completed acceptance. See [validation](docs/VALIDATION.md) and the [implementation roadmap](docs/roadmap/README.md).

## 0.1.0

Initial single-machine file, document-content and read-only database search service with local embeddings, MCP stdio, resource budgets and Python bootstrap installation scripts.
