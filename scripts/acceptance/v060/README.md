# v0.6.0 real DSH service lifecycle acceptance

Run on Windows with the installed official DeepSeek Harness package and the repository venv. These checks use real DSH profiles, authenticated Web RPC, real MCP tool registration/search, and a real daemon. They do not call an LLM or substitute mocked backend responses.

```powershell
.venv/Scripts/python.exe scripts/acceptance/v060/verify_service_controls_v060.py `
  --work .packaging-smoke/service-v060-source-new `
  --dsh E:/Dev/npm-global/node_modules/@deepseek-ai/dsh --offline
```

Every run requires a fresh `service-v060-` directory directly under `.packaging-smoke`. Profiles, configuration, index and a single synthetic Markdown file stay inside that fixture. Semantic models are disabled. The user's default DSH home and existing service are not changed.

To test an extracted native release, add `--native-bundle PATH`. The helper copies only that package's runtime into the new fixture and registers its packaged DSH bundle. Runtime and data subdirectories contain Chinese characters and spaces to verify command quoting. Keep the fixture directory itself free of spaces: the installed DSH `0.1.5-rc.1` registration command splits its staged `file:` package dependency when its DSH home contains spaces. `--bundle PATH` can explicitly override the plugin source. Without `--offline`, DSH may download its public package dependencies during isolated registration.

Pass `--runtime-source-commit COMMIT` for a native run to record the exact build source independently from the current acceptance-script checkout. The report also records the actual executable SHA-256 and hashes of the plugin files being registered.

The checks cover:

- Two DSH profiles search through the same backend.
- Web normal stop unregisters MCP tools in both profiles and disables automatic retry.
- Continued status polling and a newly opened profile do not restart a stopped backend.
- Web manual start restores tools and actual search in both profiles.
- Killing only the verified synthetic daemon produces a real reconnect wait and automatic recovery.
- Web force stop removes tools and keeps both profiles stopped.
- While stopped, Web schedule create/edit/delete works across profiles, initially with disabled tasks.
- An enabled once task really fires through Windows Task Scheduler, clears the saved stop, starts the daemon and restores both MCP profiles. The task is deleted immediately afterward; the final cleanup also removes all fixture tasks.

Full exponential delay/jitter bounds, task validation, weekly calendar timing and once-task timezone conversion are covered separately by unit tests. This harness records the first actual retry and recovery and a real enabled once-task execution.

The worker is adapted from the existing v0.5.1 helper. The Python driver reuses that helper's bounded process protocol and cleanup support. Each run writes `report.json`, including checks, process identity evidence, hashes, cleanup outcome, and any failure. Preserve failed reports when fixes lead to a later successful run.
