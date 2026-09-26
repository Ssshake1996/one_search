# DeepSeek Harness one_search bundle

This is a real DSH bundle (`dsh.bundle.patch` + Cordis plugin), using the official
`@deepseek-ai/dsh-mcp-client` bridge. It is separate from the Codex plugin shell.

## Install from an extracted one_search release

Keep the complete release directory until first activation finishes. In its root:

```powershell
$env:ONE_SEARCH_RELEASE_DIR = (Get-Location).Path
# Locate the actual global npm installation; set this manually for another installer.
$dshPackage = Join-Path ((npm root -g).Trim()) '@deepseek-ai\dsh'
$request = @{dshPackage=$dshPackage; profile='web'} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path (Get-Location) 'dsh-register.json'), $request, [Text.UTF8Encoding]::new($false))
node ./plugins/deepseek-harness/register.mjs ./dsh-register.json
dsh --profile web
```

```bash
export ONE_SEARCH_RELEASE_DIR="$PWD"
export ONE_SEARCH_DSH_PACKAGE="$(npm root -g)/@deepseek-ai/dsh"
node -e 'require("fs").writeFileSync("dsh-register.json",JSON.stringify({dshPackage:process.env.ONE_SEARCH_DSH_PACKAGE,profile:"web"}))'
node ./plugins/deepseek-harness/register.mjs ./dsh-register.json
dsh --profile web
```

The helper stages a checksummed copy under `$DSH_HOME/one-search-bundles` (default
`~/.dsh/one-search-bundles`), then invokes `dsh plugin add` with install scripts
disabled. Staging on the same volume works around DSH/pnpm's cross-drive file
dependency resolution. It does not modify existing profile overrides. Repeated
registration reuses identical staged content and rejects unexpected changes.
Request fields `dshHome` and `offline: true` optionally select a different home
or cached-only dependencies; public bundle dependencies use the official npm
registry. The returned `registered=true` does not claim an MCP connection.

`dsh plugin add` installs and registers the bundle. **The first profile activation**
installs the local backend, starts its background service, then connects MCP and
waits for tool discovery. Package managers can block install scripts; this bundle
does not rely on `postinstall`. Registration without starting that profile does
not start the service. Dependencies need npm network access or a populated pnpm
cache. The Windows native release needs no separate Python; Linux bootstrap
releases need the matching Python described in that release.

First installation defaults to the accessible local machine. It uses the same
per-user app/data directories and login startup as the normal installer. Semantic
model weights prepare in a detached background job after basic search starts,
unless disabled or an existing model directory is supplied. Download failure is
shown by `model-status` and does not block filename/keyword searches.
Later activations reuse the executable/configuration and
start the service idempotently. They preserve search roots and model settings.
After installation, the release directory environment variable is optional.

The daemon remains available when DSH exits. Unloading this bundle disconnects
MCP and removes its tools; use the installed one_search uninstaller to remove the
background service. Updating the npm bundle alone does not upgrade the backend.
Use the newer release installer and the migration rules below.

## Upgrade and reinstall

For the first migration from a v0.5.0 or older bundle, stop every DSH server/profile
connected to that backend before running the v0.5.1 installer. Closing its browser
tab is insufficient; a terminal-launched DSH can exit normally with `Ctrl+C`.
Disconnect other MCP hosts from this server and close native `Settings.vbs`
windows too. `data-search stop` only stops the daemon, leaving MCP processes and
the host's reconnect policy active.

With **all connected DSH profiles running v0.5.1 or a newer compatible bundle**,
Windows native backend upgrades can keep those profiles and Web pages open. The
Node-owned controller registers before any runtime startup. The external updater
sets a durable marker, requests maintenance, and waits for the MCP scope and
active management children to close before replacing the runtime. The MCP scope
is recreated after successful activation or rollback. The Web route stays alive
and reports maintenance; runtime operations return `upgrade_in_progress`.
An older bundle or an unrelated MCP client does not gain this capability simply
because another connected profile supports it.

Verify and extract `one-search-0.5.1-windows-amd64-native.zip` outside the existing
application and data directories. From the new extracted directory, for a default
installation:

```powershell
.\scripts\install.ps1 `
  -InstallDir (Join-Path $env:LOCALAPPDATA 'data-search\app') `
  -DataDir (Join-Path $env:LOCALAPPDATA 'data-search\data')
if ($LASTEXITCODE -ne 0) { throw 'Inspect this upgrade failure before retrying' }
```

Use the original manifest's paths for a custom installation. If this release also
updates the DSH bundle, run `register.mjs` above for each affected profile and
restart those profiles to load its new code. Backend maintenance coordination is
not a guarantee of live DSH plugin-code replacement. Verify `index_status` and
`search` from the actual profile afterward.

`runtime_in_use` identifies a remaining process by safe PID/role information. The
installer rejects before replacing the runtime and does not kill it. After an
updater crash, retain `DataDir/upgrade-state.json` and the `.upgrade-*` snapshots;
rerun the same extracted installer to recover before retrying. Never remove the
marker manually to force reconnection. An existing or unreadable marker keeps
runtime admission closed; missing resume notifications can recover through polling
after the installer removes the marker.

Windows Python bootstrap and Linux upgrades do not implement the native upgrade
transaction: stop MCP hosts and settings windows, stop the service, back up
configuration/data, and reinstall using the original paths. See the complete
[upgrade guide](../../docs/UPGRADE.md) and [README installation contract](../../README.md).

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
In DSH Web, click **one_search** in the sidebar to open the indexing overview,
scope, resource and database settings. The page uses the authenticated DSH
connection; it does not expose the daemon's loopback token. Visible status polling
does not launch a Python process. See [the Web panel guide](../../docs/DSH-WEB.md).
Available first-install fields: `installDir`, `dataDir`, `releaseDir`, `roots`,
`excludePaths`, `preset` (`low/balanced/fast`), `skipModel`, `modelDir`,
`noAutostart`. Empty `roots` means whole machine. Presets change resource budgets,
not file/content/semantic scope. Existing configuration is preserved.

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

The v0.5.1 bundle retains compatibility with backend v0.5.0 so a failed upgrade can
restore its MCP connection after rollback. Prefer matching v0.5.1 components;
automatic maintenance requires the v0.5.1 (or newer compatible) incoming installer
and bundle in every connected DSH profile. When an older incompatible managed installation is
found and `ONE_SEARCH_RELEASE_DIR`/`releaseDir` points to a compatible extracted
release, activation runs its verified installer and preserves existing settings.
Otherwise it reports `backend_update_required` with the README action. An
explicit command/configuration is never upgraded automatically.
Set `clientId: 'dsh-web'` and `clientLabel: 'DSH web'` in each profile
override to distinguish profiles in shared-service maintenance reports. Without
an explicit ID, one stable registration represents the DSH home/server name;
the plugin does not invent a profile identity. Registrations record clients that
use the service, not currently live connections. Unloading keeps registration;
`data-search remove-client ID --config CONFIG` removes an obsolete registration.

Upgrade coordination also writes a separate, live registration with a random
instance ID for **each active plugin instance**, so profiles sharing a persistent
client ID still drain independently. These transient `DataDir/host-clients` records
are removed on normal unload and excluded from upgrade snapshots. Their local
control tokens stay in the host/installer and are never returned to the browser.

DSH tool names use `mcp__one_search__` followed by `search`, `fetch`,
`inspect_source`, `query_database`, `index_status`, `diagnose_path`, `read_context`,
`refresh_path`, `prioritize_path`, `pause_indexing`, or `resume_indexing`.
Ask DSH to call `index_status` first, then search. Refresh, priority, pause and
resume are explicit user-requested indexing operations.
These are MCP tools; no DSH model credentials are needed for local indexing.

Compatibility target: installed DSH CLI `0.1.5-rc.1`, official MCP client
`0.1.5-rc.2`, Cordis `4.0.2`. Keep the runtime and host smoke-test evidence with
the release; different DSH versions require revalidation.

## Executable connection check

`node plugins/deepseek-harness/verify.mjs REQUEST.json` uses the installed DSH CLI
to register this bundle in a new diagnostic profile, boots that profile, checks
all 11 MCP tools and calls `index_status`/`search`. It uses an existing backend;
it does not provision one or call a chat model. Existing user profiles are not
changed. The backend keeps running afterward. Its temporary client registration
is removed, and the isolated diagnostic home is retained at the reported path
for troubleshooting. A populated package cache is required; the checker uses
offline dependency resolution instead of changing registry credentials.

Create the request as UTF-8 JSON (absolute paths, no passwords):

```json
{
  "dshPackage": "E:/Dev/npm-global/node_modules/@deepseek-ai/dsh",
  "command": "C:/Users/YOU/AppData/Local/data-search/app/runtime/data-search.exe",
  "configPath": "C:/Users/YOU/AppData/Local/data-search/data/config.json",
  "report": "C:/Users/YOU/Downloads/one-search-dsh-check.json"
}
```

Locate the actual installed DSH package rather than copying the example path.
For source execution, `command` may be the absolute venv Python path with
`"commandArgs": ["-m", "data_search"]`. Exit `0` and `ok=true, connected=true`
certify the diagnostic profile's connection at the recorded time. The checker
reports `chat_model_called=false, answer_quality_tested=false`; a real DSH answer
with citations remains a separate acceptance test. For the normal user profile,
also call the two tools from that profile to verify its own configuration.
