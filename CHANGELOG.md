# Changelog

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
