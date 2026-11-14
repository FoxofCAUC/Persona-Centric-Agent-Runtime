param([ValidateSet('Export','Preview','Import','ConfigureMain','ConfigureJev','ConfigureOrganization','ConfigureSelector','ActivatePreview','Activate','RollbackPreview','Rollback','Doctor')][string]$Action = 'Doctor',
      [string]$RuntimeHome, [string]$Package, [string]$BaseUrl, [string]$Model,
      [switch]$AuthorizeVisible, [switch]$ResumeBackground, [switch]$Network)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pcar-location.ps1')
$RuntimeHome = Get-PcarRuntimeHome -RuntimeHome $RuntimeHome
Set-PcarProcessEnvironment -RuntimeHome $RuntimeHome
$python = Get-PcarPython -RuntimeHome $RuntimeHome
if (-not (Test-Path -LiteralPath $python)) { throw '请先准备所选实例的桌面环境。' }
$arguments = @('-B','-m','persona_runtime','--home',$RuntimeHome)
switch ($Action) {
    'Export' { $arguments += @('data','migration-export') }
    'Preview' { $arguments += @('data','migration-import','--file',$Package) }
    'Import' { $arguments += @('data','migration-import','--file',$Package,'--apply') }
    'ActivatePreview' { $arguments += @('data','migration-activate') }
    'Activate' {
        $arguments += @('data','migration-activate','--apply')
        if ($AuthorizeVisible) { $arguments += '--authorize-visible' }
        if ($ResumeBackground) { $arguments += '--resume-background' }
    }
    'RollbackPreview' { $arguments += @('data','migration-rollback') }
    'Rollback' { $arguments += @('data','migration-rollback','--apply') }
    'Doctor' { $arguments += 'doctor'; if ($Network) { $arguments += '--network' } }
    default {
        $roles = @{ ConfigureMain='main'; ConfigureJev='jev'; ConfigureOrganization='organization'; ConfigureSelector='selector' }
        $arguments += @('configure-model','--role',$roles[$Action])
        if ($BaseUrl) { $arguments += @('--base-url',$BaseUrl) }
        if ($Model) { $arguments += @('--model',$Model) }
    }
}
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try { & $python @arguments; exit $LASTEXITCODE } finally { Pop-Location }
