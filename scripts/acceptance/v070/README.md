# v0.7.0 native service acceptance with DSH 0.2

This adapter runs the unchanged [v0.6 controller](../v060/verify_service_controls_v060.py), including its v0.5.1 process support, against the installed official DSH. The worker retains the previous fixture, path, process and synthetic-data boundaries. Its only boot change is to import the DSH 0.2 `lib/profile-boot.js` entry and verify the exported `runProfile` function instead of parsing a generated chunk name from `bin.js`.

```powershell
$dshPackage = Join-Path ((npm root -g).Trim()) '@deepseek-ai\dsh'
.venv/Scripts/python.exe scripts/acceptance/v070/verify_service_controls_v070.py `
  --work .packaging-smoke/service-v060-v070-native-02 `
  --dsh $dshPackage `
  --native-bundle dist/release-v0.7.0/candidate-1/one-search-0.7.0-windows-amd64-native `
  --runtime-source-commit c0b42eb9ee730eb5c1aafc6b0485108019071dd7 `
  --offline
```

Use a fresh fixture for each run. The retained controller requires the `service-v060-` fixture prefix. The native runtime and data paths contain spaces and Chinese characters; the DSH homes remain inside the fixture. Semantic models are disabled and no model calls or real user data are included. The installed DSH and its default home are not modified.

All lifecycle assertions remain real: authenticated Web requests, 11 registered MCP tools and actual keyword searches in two profiles, normal stop, no automatic revival during polling or new-profile startup, manual start, verified synthetic-daemon termination and automatic recovery with an observed retry, force stop, cross-profile schedule CRUD, and a real enabled Windows once task. Cleanup deletes fixture tasks, disposes profiles and force-stops only the fixture backend. See the [v0.6 README](../v060/README.md) for scope and limitations.

The first v0.7.0 candidate attempt used the old adapter and failed before profile boot with `Installed DSH boot adapter changed` on DSH `0.2.0-rc.2`. Its report remains at `.packaging-smoke/service-v060-v070-native/report.json`; it recorded no checks or OS tasks and `cleaned_up: true`. This is an acceptance adapter failure, not a successful lifecycle run. Retain that evidence alongside the later run report.

The corrected candidate-1 run passed on DSH `0.2.0-rc.2`, with successful cleanup. The [published validation report](../../../docs/validation/service-controls-native-v0.7.0.json) retains every lifecycle observation, exact runtime/plugin/adapter hashes, the earlier failure, and the final read-only verification of zero fixture processes and zero fixture OS tasks. The Windows once task requested for `2026-10-06T00:22:56+09:00` recorded a successful start at `2026-10-06T00:22:57+09:00`.
