# v0.5.1 Windows native acceptance

These scripts use real native executables, real installed DSH profiles, and synthetic
files only. Each run requires a new directory directly beneath the repository's
`.packaging-smoke` directory whose name begins with `upgrade-v051-`. They never use
the default DSH home. Do not reuse an old fixture directory.

Dependencies: Windows, Node.js 24.11.1, Python 3.11 in `.venv`, `psutil`, and an
installed `@deepseek-ai/dsh` package. The reference local host uses DSH CLI
`0.1.5-rc.1` and MCP client `0.1.5-rc.2`; pin the DSH dependency graph in the runner
setup. No Python product installation, semantic model, LLM account, or browser is
required. The native settings window runs hidden.

Extract the candidate native ZIP into `dist/release-v0.5.1/candidate-1`. Download
the published old native ZIP to
`dist/release-v0.5.0/one-search-0.5.0-windows-amd64-native.zip`. The upgrade helper
requires that old archive's SHA-256 to be
`b938f14af308a533d75e3c6da006ad55e870eca5d470d9a9d0e7a64883e024ae`.
The workflow must verify the candidate archive's expected hash before extraction;
the reports also record its executable hash.

This acceptance run pins the existing candidate ZIP SHA-256
`240de589cbf5aa6947bd3733f08ba1efc907f6e557d91a2fd53345ef1fd4730f`
and native executable SHA-256
`cacff5988147dd4e2081658462ab06fabb53d1677bd2e83f77380630ce366bd6`.
The runtime was built from commit `efeff0ff123209b89d056fa41719737d08294992`.
Reports distinguish that build commit from the later commit containing the test
helpers/workflow. Do not substitute a newly built executable for this candidate.

From the repository root, run sequentially, replacing `DSH_PACKAGE` with the
absolute path to the installed `@deepseek-ai/dsh` package directory:

```powershell
.venv/Scripts/python.exe scripts/acceptance/v051/verify_upgrade_v051.py dist/release-v0.5.1/candidate-1/one-search-0.5.1-windows-amd64-native .packaging-smoke/upgrade-v051-ci-native --dsh DSH_PACKAGE --include-gui
.venv/Scripts/python.exe scripts/acceptance/v051/verify_dsh_managed_install_v051.py dist/release-v0.5.1/candidate-1/one-search-0.5.1-windows-amd64-native .packaging-smoke/upgrade-v051-ci-managed --dsh DSH_PACKAGE
```

Package registration may use the network by default. Add `--offline` only when
the runner's pnpm cache is already populated. The native upgrade helper stages
profile packages before starting runtimes, then genuinely starts a new host while
the durable maintenance marker exists. Two main DSH hosts stay alive throughout;
each additional host is closed after its reconnect/search assertions pass.

The upgrade suite checks old MCP and GUI blockers, failed activation rollback,
interruption of the exact owned updater process tree, retry with its marker and
journal retained, successful upgrade, and MCP rediscovery/search in every live
profile. The managed suite supplies no command or config path: the DSH plugin must
perform first installation itself and reconnect without a self-deadlock.

Each fixture writes `report.json`. Upload these reports even when a run fails.
Do not upload whole DSH homes or data directories, which contain local control
credentials. Cleanly completed runs uninstall their own synthetic application and
data directories; an interrupted recovery marker causes the fixture to be retained
for diagnosis rather than removed to manufacture a successful result.
