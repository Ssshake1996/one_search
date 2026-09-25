# DeepSeek Harness one_search bundle

This is a real DSH bundle (`dsh.bundle.patch` + Cordis plugin), using the official
`@deepseek-ai/dsh-mcp-client` bridge. It is separate from the Codex plugin shell.

## Install from an extracted one_search release

Keep the complete release directory until first activation finishes. In its root:

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
dsh plugin --profile web add ./plugins/deepseek-harness
dsh --profile web
```

```bash
export ONE_SEARCH_RELEASE_DIR="$PWD"
dsh plugin --profile web add ./plugins/deepseek-harness
dsh --profile web
```

`dsh plugin add` installs and registers the bundle. **The first profile activation**
installs the local backend, starts its background service, then connects MCP and
waits for tool discovery. Package managers can block install scripts; this bundle
does not rely on `postinstall`. Registration without starting that profile does
not start the service. Dependencies need npm network access or a populated pnpm
cache. The Windows native release needs no separate Python; Linux bootstrap
releases need the matching Python described in that release.

First installation defaults to the accessible local machine. It uses the same
per-user app/data directories and login startup as the normal installer. Semantic
model weights download on first installation unless disabled or an existing model
directory is supplied. Later activations reuse the executable/configuration and
start the service idempotently. They preserve search roots and model settings.
After installation, the release directory environment variable is optional.

The daemon remains available when DSH exits. Unloading this bundle disconnects
MCP and removes its tools; use the installed one_search uninstaller to remove the
background service. Updating the npm bundle alone does not upgrade the backend:
run the newer release installer for a transactional backend upgrade.

## Configure scope or reuse an existing backend

Add an override to the selected profile's `cordis.patch.yml` (DSH profiles live
under `$DSH_HOME/profiles/<name>`, normally `~/.dsh/profiles/<name>`):

```yaml
- id: one-search
  config:
    releaseDir: 'D:\Downloads\one-search-release'
    roots: ['D:\Documents', 'D:\Projects']
    skipModel: true
    noAutostart: false
```

These installation options apply only when provisioning a missing backend.
Change an already installed scope through the one_search settings window.
Available first-install fields: `installDir`, `dataDir`, `releaseDir`, `roots`,
`skipModel`, `modelDir`, `noAutostart`. Empty `roots` means whole machine.

For an existing installation, supply an absolute executable and configuration:

```yaml
- id: one-search
  config:
    command: 'D:\Apps\data-search\runtime\data-search.exe'
    configPath: 'D:\SearchData\config.json'
```

For a source environment, `command` can point to the venv Python and
`commandArgs: ['-m', 'data_search']`. This mode requires an existing configuration
and never installs or rewrites it. Optional `serverName` defaults to `one_search`;
`timeoutMs` bounds each setup command (default 15 minutes).

DSH tools are named `mcp__one_search__search`, `mcp__one_search__fetch`,
`mcp__one_search__inspect_source`, `mcp__one_search__query_database`, and
`mcp__one_search__index_status`. Ask DSH to call `index_status` first, then search.
These are MCP tools; no DSH model credentials are needed for local indexing.

Compatibility target: installed DSH CLI `0.1.5-rc.1`, official MCP client
`0.1.5-rc.2`, Cordis `4.0.2`. Keep the runtime and host smoke-test evidence with
the release; different DSH versions require revalidation.
