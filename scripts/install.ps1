[CmdletBinding()]
param(
    [string[]]$Root = @(),
    [string[]]$Exclude = @(),
    [ValidateSet('low','balanced','fast')][string]$Preset = 'balanced',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'data-search\app'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'data-search\data'),
    [string]$Python = 'python',
    [string]$PackagePath,
    [string]$Wheelhouse,
    [string]$RuntimeDir,
    [string]$ModelDir,
    [switch]$SkipModel,
    [switch]$NoAutostart,
    [Parameter(DontShow=$true)][switch]$NativeTransactionChild,
    [Parameter(DontShow=$true)][string]$UpgradeRequest
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$installStage = 'preflight'
$installLocks = [Collections.Generic.List[IO.FileStream]]::new()
trap {
    for ($i = $installLocks.Count - 1; $i -ge 0; $i--) { $installLocks[$i].Dispose() }
    $failure = @{schema_version=1; event='installation_result'; ok=$false; stage=$installStage;
        error=@{code='installation_failed'; message='Installation did not complete. Inspect stderr and retry the same command; existing native installs use transactional rollback.'}}
    $failure | ConvertTo-Json -Depth 8 -Compress | Write-Output
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}

function Full-Path([string]$Path) { return [IO.Path]::GetFullPath($Path) }
function Assert-ManagedTarget([string]$Path) {
    if ($Path -eq [IO.Path]::GetPathRoot($Path) -or $Path.TrimEnd('\') -eq $env:USERPROFILE.TrimEnd('\')) {
        throw "Refusing installation at drive root or user profile: $Path"
    }
    $current = $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Installation path must not contain a junction or symlink: $current"
            }
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
}
function Run-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')" }
}
function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 24), [Text.UTF8Encoding]::new($false))
}
function Lock-InstallFile([string]$Path) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    $handle = [IO.File]::Open($Path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        if ($handle.Length -eq 0) { $handle.WriteByte(0); $handle.Flush() }
        $handle.Lock(0, 1)
        return $handle
    } catch { $handle.Dispose(); throw 'The instance is busy; retry installation after maintenance completes.' }
}

if ($NativeTransactionChild) {
    if (-not $UpgradeRequest) { throw 'Native transaction continuation requires a request file.' }
    $continuation = Get-Content -LiteralPath $UpgradeRequest -Raw -Encoding UTF8 | ConvertFrom-Json
    $InstallDir = [string]$continuation.InstallDir
    $DataDir = [string]$continuation.DataDir
    $RuntimeDir = [string]$continuation.RuntimeDir
    $Root = [string[]]@($continuation.Root)
    if ($continuation.PSObject.Properties['Exclude']) { $Exclude = [string[]]@($continuation.Exclude) }
    if ($continuation.PSObject.Properties['Preset']) { $Preset = [string]$continuation.Preset }
    $ModelDir = [string]$continuation.ModelDir
    $SkipModel = [bool]$continuation.SkipModel
    $NoAutostart = [bool]$continuation.NoAutostart
}

if ($SkipModel -and $ModelDir) { throw 'Choose either -SkipModel or -ModelDir.' }
$repoRoot = Split-Path $PSScriptRoot -Parent
$releaseManifestPath = Join-Path $repoRoot 'RELEASE_MANIFEST.json'
if (Test-Path -LiteralPath $releaseManifestPath) {
    $release = Get-Content -LiteralPath $releaseManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($release.kind -eq 'windows-native' -and -not $PackagePath -and -not $RuntimeDir) {
        $RuntimeDir = Join-Path $repoRoot 'runtime'
    } elseif ($release.kind -eq 'python-bootstrap' -and -not $PackagePath -and -not $RuntimeDir) {
        $PackagePath = Join-Path $repoRoot $release.package_wheel
        if (-not $Wheelhouse) { $Wheelhouse = Join-Path $repoRoot 'wheelhouse' }
    }
}
if ($RuntimeDir) {
    $RuntimeDir = (Get-Item -LiteralPath $RuntimeDir).FullName
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir 'data-search.exe') -PathType Leaf)) { throw 'RuntimeDir must contain data-search.exe.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'The native bundle requires 64-bit Windows.' }
} else {
    if (-not $PackagePath) { $PackagePath = $repoRoot }
    Run-Checked $Python @((Join-Path $PSScriptRoot 'check_runtime.py'), $repoRoot)
}
$InstallDir = Full-Path $InstallDir
$DataDir = Full-Path $DataDir
Assert-ManagedTarget $InstallDir
Assert-ManagedTarget $DataDir
if ($DataDir -eq $InstallDir -or $DataDir.StartsWith($InstallDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or $InstallDir.StartsWith($DataDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'DataDir must be outside InstallDir so uninstall can preserve data.'
}
$configPath = Join-Path $DataDir 'config.json'
$manifestPath = Join-Path $InstallDir 'install-manifest.json'
$dataMarkerPath = Join-Path $DataDir '.data-search-data.json'
if (Test-Path -LiteralPath $DataDir) {
    if (Test-Path -LiteralPath $dataMarkerPath) {
        $dataMarker = Get-Content -LiteralPath $dataMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($dataMarker.product -ne 'data-search' -or $dataMarker.data_dir -ne $DataDir) { throw 'Invalid data directory marker.' }
    } elseif (@(Get-ChildItem -LiteralPath $DataDir -Force).Count -gt 0) {
        throw 'DataDir must be empty or an existing installer-managed data-search directory.'
    }
}
if (Test-Path -LiteralPath $InstallDir) {
    if (Test-Path -LiteralPath $manifestPath) {
        $old = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($old.product -ne 'data-search' -or $old.schema_version -ne 1 -or $old.install_dir -ne $InstallDir -or $old.data_dir -ne $DataDir) {
            throw 'Existing install manifest does not match requested directories.'
        }
    } elseif (@(Get-ChildItem -LiteralPath $InstallDir -Force).Count -gt 0) {
        throw 'Refusing to install into a nonempty unmanaged directory.'
    }
}
$resolvedRoots = @($Root | ForEach-Object {
    $item = Get-Item -LiteralPath $_
    if (-not $item.PSIsContainer) { throw "Search root is not a directory: $_" }
    $item.FullName
})
if ($ModelDir) {
    $ModelDir = (Get-Item -LiteralPath $ModelDir).FullName
    foreach ($asset in @('model.onnx', 'tokenizer.json', 'config.json', 'manifest.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ModelDir $asset) -PathType Leaf)) { throw "Offline model directory is missing: $asset" }
    }
}
if ($RuntimeDir -and -not $NativeTransactionChild) {
    if (Test-Path -LiteralPath (Join-Path $DataDir 'model-job\status.json')) {
        Run-Checked (Join-Path $RuntimeDir 'data-search.exe') @('--internal-module', 'data_search.model_manager', 'quiesce', '--config', $configPath)
    }
    $requestPath = Join-Path ([IO.Path]::GetTempPath()) ('data-search-upgrade-' + [Guid]::NewGuid().ToString('N') + '.json')
    $request = @{InstallDir=$InstallDir; DataDir=$DataDir; RuntimeDir=$RuntimeDir; Root=@($resolvedRoots);
        Exclude=@($Exclude); Preset=$Preset;
        ModelDir=$ModelDir; SkipModel=[bool]$SkipModel; NoAutostart=[bool]$NoAutostart;
        Installer=$PSCommandPath; PowerShell=[Diagnostics.Process]::GetCurrentProcess().MainModule.FileName}
    try {
        Write-Json $requestPath $request
        Run-Checked (Join-Path $RuntimeDir 'data-search.exe') @('--internal-module', 'data_search.upgrade', '--request', $requestPath)
    } finally {
        if (Test-Path -LiteralPath $requestPath) { Remove-Item -LiteralPath $requestPath -Force }
    }
    return
}
New-Item -ItemType Directory -Path $InstallDir, $DataDir -Force | Out-Null
$installStage = 'runtime'
Write-Json $dataMarkerPath @{product='data-search'; data_dir=$DataDir; schema_version=1}
$hash = [Security.Cryptography.SHA256]::Create()
try { $installId = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallDir.ToLowerInvariant())))).Replace('-', '').Substring(0, 12).ToLowerInvariant() }
finally { $hash.Dispose() }
$startupName = "DataSearch-$installId"
$manifest = [ordered]@{ product='data-search'; schema_version=1; install_dir=$InstallDir; data_dir=$DataDir; config=$configPath; startup_name=$startupName; autostart='none' }
Write-Json $manifestPath $manifest
$venvDir = Join-Path $InstallDir 'venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$cli = Join-Path $venvDir 'Scripts\data-search.exe'
if (-not $RuntimeDir) {
    $installLocks.Add((Lock-InstallFile (Join-Path $DataDir 'model-job\manager.lock')))
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath (Join-Path $DataDir 'model-job\status.json'))) {
        Run-Checked $venvPython @('-m', 'data_search.model_manager', 'quiesce', '--config', $configPath)
    }
    $installLocks.Add((Lock-InstallFile (Join-Path $DataDir 'model-job\worker.lock')))
}
if ((Test-Path -LiteralPath $cli) -and (Test-Path -LiteralPath $configPath)) { Run-Checked $cli @('stop', '--config', $configPath) }
$nativeCli = Join-Path $InstallDir 'runtime\data-search.exe'
if ((Test-Path -LiteralPath $nativeCli) -and (Test-Path -LiteralPath $configPath)) { Run-Checked $nativeCli @('stop', '--config', $configPath) }
if (-not $RuntimeDir) { $installLocks.Add((Lock-InstallFile (Join-Path $DataDir 'service.lock'))) }
if ($RuntimeDir) {
    $runtimeDest = Join-Path $InstallDir 'runtime'
    if (-not $NativeTransactionChild -or (Full-Path $RuntimeDir) -ne (Full-Path $runtimeDest)) { throw 'Native runtime must be staged and verified by the upgrade transaction.' }
    $cli = $nativeCli
} else {
    if (-not (Test-Path -LiteralPath $venvPython)) { Run-Checked $Python @('-m', 'venv', $venvDir) }
    Run-Checked $venvPython @((Join-Path $PSScriptRoot 'check_runtime.py'), $repoRoot)
    Write-Host 'Installing data-search and dependencies into its isolated environment...'
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet')
    if ($Wheelhouse) {
        $Wheelhouse = (Get-Item -LiteralPath $Wheelhouse).FullName
        $pipArgs += @('--no-index', '--find-links', $Wheelhouse)
    }
    $pipArgs += (Get-Item -LiteralPath $PackagePath).FullName
    Run-Checked $venvPython $pipArgs
}
$manifest.cli = $cli
$installStage = 'configuration'
if (-not (Test-Path -LiteralPath $configPath)) {
    $initArgs = @('init', '--config', $configPath, '--data-dir', $DataDir, '--exclude', $InstallDir)
    foreach ($searchRoot in $resolvedRoots) { $initArgs += @('--root', $searchRoot) }
    foreach ($excludedPath in $Exclude) { $initArgs += @('--exclude', (Full-Path $excludedPath)) }
    Run-Checked $cli $initArgs
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $presetInfo = & $cli 'preset' $Preset '--config' $configPath
    if ($LASTEXITCODE -ne 0) { throw 'Resource preset validation failed.' }
    $presetValues = ($presetInfo -join [Environment]::NewLine) | ConvertFrom-Json
    $config.resource = $presetValues.resource
    $config.scheduler = $presetValues.scheduler
    $config.semantic = $presetValues.semantic
    if (-not $config.PSObject.Properties['runtime_policy']) { $config | Add-Member -MemberType NoteProperty -Name runtime_policy -Value ([pscustomobject]@{preset=$Preset}) }
    else { $config.runtime_policy.preset = $Preset }
    if ($SkipModel) { $config.semantic.enabled = $false }
    if ($ModelDir) { $config.semantic.model_dir = $ModelDir }
    Write-Json $configPath $config
} else { Write-Host "Preserving existing config: $configPath (roots and model options are unchanged)." }
$activeConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$excluded = @()
if ($activeConfig.PSObject.Properties['exclude_paths']) { $excluded = @($activeConfig.exclude_paths) }
if ($excluded -notcontains $InstallDir) {
    $activeConfig | Add-Member -MemberType NoteProperty -Name exclude_paths -Value @($excluded + $InstallDir) -Force
    Write-Json $configPath $activeConfig
}
$mcp = @{ mcpServers = @{ 'data-search' = @{ command=$cli; args=@('mcp', '--config', $configPath) } } }
Write-Json (Join-Path $InstallDir 'mcp.json') $mcp
$pluginSource = Join-Path $repoRoot 'plugins\data-search'
$pluginDest = Join-Path $InstallDir 'plugin'
if (Test-Path -LiteralPath $pluginSource) {
    New-Item -ItemType Directory -Path $pluginDest -Force | Out-Null
    Get-ChildItem -LiteralPath $pluginSource -Force | Copy-Item -Destination $pluginDest -Recurse -Force
    Write-Json (Join-Path $pluginDest '.mcp.json') $mcp
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination (Join-Path $InstallDir 'uninstall.ps1') -Force
if ($RuntimeDir) {
    $settingsCommand = '"' + $cli + '" setup --config "' + $configPath + '"'
    $settingsVbs = 'CreateObject("WScript.Shell").Run "' + $settingsCommand.Replace('"','""') + '", 0, False' + "`r`n"
    [IO.File]::WriteAllText((Join-Path $InstallDir 'Settings.vbs'), $settingsVbs, [Text.Encoding]::Unicode)
}
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ($NoAutostart) {
    if (Test-Path -LiteralPath $runKey) { Remove-ItemProperty -LiteralPath $runKey -Name $startupName -ErrorAction SilentlyContinue }
} else {
    # WScript launches the CLI without a visible console. CLI start detaches and deduplicates.
    $command = '"' + $cli + '" start --config "' + $configPath + '"'
    $vbs = 'CreateObject("WScript.Shell").Run "' + $command.Replace('"','""') + '", 0, False' + "`r`n"
    $vbsPath = Join-Path $InstallDir 'launch-hidden.vbs'
    [IO.File]::WriteAllText($vbsPath, $vbs, [Text.Encoding]::Unicode)
    New-Item -Path $runKey -Force | Out-Null
    New-ItemProperty -LiteralPath $runKey -Name $startupName -Value ('wscript.exe "' + $vbsPath + '"') -PropertyType String -Force | Out-Null
    $manifest.autostart = 'windows-user-run'
}
$versionInfo = & $cli 'version' '--config' $configPath
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime did not provide its version.' }
$manifest.version = (($versionInfo -join [Environment]::NewLine) | ConvertFrom-Json).version
Write-Json $manifestPath $manifest
for ($i = $installLocks.Count - 1; $i -ge 0; $i--) { $installLocks[$i].Dispose() }
$installLocks.Clear()
$installStage = 'daemon_start'
Run-Checked $cli @('start', '--config', $configPath)
$installStage = 'model_queued'
if ($activeConfig.semantic.enabled -and -not $SkipModel) {
    # This launches a detached job. Model download/network failure cannot block basic search.
    if ($ModelDir) { & $cli 'model-import' '--config' $configPath '--source' $activeConfig.semantic.model_dir }
    else { & $cli 'model-start' '--config' $configPath }
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Basic search is running; model preparation could not start. Use model-status/model-start to recover.' }
}
$installStage = 'acceptance'
$report = & $cli 'installation-status' '--config' $configPath '--install-dir' $InstallDir
if ($LASTEXITCODE -ne 0) { throw 'Basic search acceptance failed. Run start/status with the installed configuration.' }
$reportText = $report -join [Environment]::NewLine
$result = $reportText | ConvertFrom-Json
Write-Json (Join-Path $DataDir 'install-result.json') $result
$reportText | Write-Output
