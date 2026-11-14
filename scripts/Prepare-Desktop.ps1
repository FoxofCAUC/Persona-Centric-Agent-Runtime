param([string]$RuntimeHome, [string]$BasePython, [string]$LocalModelSource,
      [switch]$Diagnostics, [switch]$RegisterExisting, [switch]$NoRegister, [switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pcar-location.ps1')
$RuntimeHome = Get-PcarRuntimeHome -RuntimeHome $RuntimeHome -Interactive:(-not $NonInteractive)
if (-not $RuntimeHome) { exit 0 }
Set-PcarProcessEnvironment -RuntimeHome $RuntimeHome
$python = Get-PcarBasePython -BasePython $BasePython
$arguments = @('-B','-m','persona_runtime','--home',$RuntimeHome,'prepare')
if ($BasePython) { $arguments += @('--base-python',$BasePython) }
if ($LocalModelSource) { $arguments += @('--local-model-source',$LocalModelSource) }
if ($Diagnostics) { $arguments += '--diagnostics' }
if ($RegisterExisting) { $arguments += '--register-existing' }
if ($NoRegister) { $arguments += '--no-register' }
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try { & $python @arguments; exit $LASTEXITCODE } finally { Pop-Location }
