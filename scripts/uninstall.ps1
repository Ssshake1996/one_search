[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'data-search\app'),
    [switch]$DeleteData
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
function Assert-ExactTarget([string]$Path, [string]$Expected) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($resolved -ne $Expected -or $resolved -eq [IO.Path]::GetPathRoot($resolved) -or $resolved.TrimEnd('\') -eq $env:USERPROFILE.TrimEnd('\')) {
        throw "Refusing unsafe removal: $resolved"
    }
    $current = $resolved
    while ($current) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing removal through junction or symlink: $current"
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
    return $resolved
}
if (-not (Test-Path -LiteralPath $InstallDir)) { Write-Host 'Already uninstalled.'; exit 0 }
$manifestPath = Join-Path $InstallDir 'install-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'No data-search install manifest; refusing deletion.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.product -ne 'data-search' -or $manifest.schema_version -ne 1) { throw 'Invalid install manifest.' }
$checkedInstall = Assert-ExactTarget $InstallDir $manifest.install_dir
$checkedData = Assert-ExactTarget $manifest.data_dir $manifest.data_dir
if ($checkedData -eq $checkedInstall -or $checkedInstall.StartsWith($checkedData.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or $checkedData.StartsWith($checkedInstall.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Manifest contains overlapping directories; refusing deletion.'
}
$cli = Join-Path $checkedInstall 'venv\Scripts\data-search.exe'
if ($manifest.PSObject.Properties['cli']) { $cli = $manifest.cli }
if (-not ([IO.Path]::GetFullPath($cli)).StartsWith($checkedInstall.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'CLI path must be within the managed installation.' }
function Lock-ManagedFile([string]$Path) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $handle = [IO.File]::Open($Path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, $share)
    try {
        if ($handle.Length -eq 0) { $handle.WriteByte(0); $handle.Flush() }
        $handle.Lock(0, 1)
        return $handle
    } catch { $handle.Dispose(); throw 'The instance or model job is busy; installation and data retained.' }
}
if ($DeleteData -and (Test-Path -LiteralPath $checkedData)) {
    $markerPath = Join-Path $checkedData '.data-search-data.json'
    if (-not (Test-Path -LiteralPath $markerPath)) { throw 'Data directory marker missing; data retained.' }
    $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($marker.product -ne 'data-search' -or $marker.data_dir -ne $checkedData) { throw 'Invalid data marker; data retained.' }
}
$locks = [Collections.Generic.List[IO.FileStream]]::new()
try {
    # Admission remains blocked until replacement/deletion is complete. Merely
    # stopping the daemon does not stop a detached semantic model download.
    $locks.Add((Lock-ManagedFile (Join-Path $checkedData 'model-job\manager.lock')))
    if (Test-Path -LiteralPath $cli) {
        & $cli model-quiesce --config $manifest.config
        if ($LASTEXITCODE -ne 0) { throw 'Model job did not stop; installation and data retained.' }
    }
    $locks.Add((Lock-ManagedFile (Join-Path $checkedData 'model-job\worker.lock')))
    if (Test-Path -LiteralPath $cli) {
        & $cli stop --config $manifest.config
        if ($LASTEXITCODE -ne 0) { throw 'Daemon did not stop; installation and data retained.' }
    }
    $locks.Add((Lock-ManagedFile (Join-Path $checkedData 'service.lock')))
    if ($DeleteData -and (Test-Path -LiteralPath $cli)) {
        & $cli purge-external-index --config $manifest.config
        if ($LASTEXITCODE -ne 0) { throw 'External index could not be safely cleaned; installation and data retained.' }
    }
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (Test-Path -LiteralPath $runKey) { Remove-ItemProperty -LiteralPath $runKey -Name $manifest.startup_name -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $checkedInstall -Recurse -Force
    if ($DeleteData -and (Test-Path -LiteralPath $checkedData)) {
        Remove-Item -LiteralPath $checkedData -Recurse -Force
        Write-Host 'Uninstalled and removed the configured data directory and managed external index files.'
    } else { Write-Host "Uninstalled. Configuration, models and indexes preserved at $checkedData" }
} finally {
    for ($i = $locks.Count - 1; $i -ge 0; $i--) { $locks[$i].Dispose() }
}
