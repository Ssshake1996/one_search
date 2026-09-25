# data-search plugin shell

This directory is a validated Codex-format shell. It deliberately has an empty
`.mcp.json`: an uninstalled shell cannot know the machine's absolute executable
and configuration paths.

Run the repository's Windows or Linux installer. It copies this directory to
`<InstallDir>/plugin` and writes a working `.mcp.json` with absolute paths. The
same MCP entry appears in `<InstallDir>/mcp.json` for DSH or other MCP hosts.

The shell does not install software through lifecycle hooks, alter host settings,
or register a global Codex marketplace. Its companion installer provisions the
background service. New installations discover the current user-accessible local machine disks by default; optional roots select directory scope, and upgrades retain existing scope. Windows native bundles include their runtime and provide a local settings window; Python bootstrap installations require a matching interpreter. Host plugin-store installation behavior remains unverified.

See `docs/INSTALL.md` in the source or release bundle for setup and uninstall.
