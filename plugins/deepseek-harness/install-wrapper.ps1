param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$parameters = @{
    InstallDir = [string]$request.installDir
    DataDir = [string]$request.dataDir
    Root = [string[]]@($request.roots)
    Exclude = [string[]]@($request.excludePaths)
    Preset = [string]$request.preset
    SkipModel = [bool]$request.skipModel
    NoAutostart = [bool]$request.noAutostart
}
if ($request.modelDir) { $parameters.ModelDir = [string]$request.modelDir }
& (Join-Path $request.releaseDir 'scripts\install.ps1') @parameters
if (-not $?) { throw 'one_search installer failed' }
