<#
.SYNOPSIS
  Install / update the Z2Labs EMS plugin for KiCad 10 on Windows.
  Idempotent: safe to re-run. Does not touch any board file.

.WHAT IT DOES
  1. git clone (or pull) the repo to -Src (default %USERPROFILE%\src\z2ems-kicad)
  2. junction  Documents\KiCad\10.0\plugins\kicad-z2ems  -> repo
  3. kicad_common.json: api.enable_server = true, interpreter_path = KiCad 10 pythonw.exe (backup kept)
  4. global sym-lib-table / fp-lib-table: add "z2ems" (backups kept)
  5. pip install kicad-python (+pytest) into KiCad 10's python
  6. run the unit tests

.USAGE
  powershell -ExecutionPolicy Bypass -File install-windows.ps1 [-Src <folder>] [-Repo https://github.com/z2labs/z2ems-kicad]
  KiCad should be closed while the settings files are patched.
#>
param(
  [string]$Repo = "https://github.com/z2labs/z2ems-kicad",
  [string]$Src = "",
  [string]$KiCad = "C:\Program Files\KiCad\10.0"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path "$KiCad\bin\kicad.exe")) { throw "KiCad 10 not found at $KiCad" }
if (Get-Process kicad,pcbnew -ErrorAction SilentlyContinue) { Write-Warning "KiCad is running - settings patch may be overwritten; close KiCad and re-run if the toolbar button does not appear." }
if (-not $Src) { $Src = "$env:USERPROFILE\src\z2ems-kicad" }

# 1. repo
New-Item -ItemType Directory -Force (Split-Path $Src) | Out-Null
if (Test-Path "$Src\.git") { git -C $Src pull --ff-only } else { git clone $Repo $Src }

# 2. junction
$plug = "$env:USERPROFILE\Documents\KiCad\10.0\plugins"
New-Item -ItemType Directory -Force $plug | Out-Null
if (-not (Test-Path "$plug\kicad-z2ems")) { cmd /c mklink /J "$plug\kicad-z2ems" $Src | Out-Null }

# 3. API server
$cfgdir = "$env:APPDATA\kicad\10.0"
New-Item -ItemType Directory -Force $cfgdir | Out-Null
$c = "$cfgdir\kicad_common.json"
if (Test-Path $c) {
  $j = Get-Content $c -Raw | ConvertFrom-Json
  if (-not $j.api) { $j | Add-Member -NotePropertyName api -NotePropertyValue ([pscustomobject]@{}) }
  $j.api | Add-Member -Force -NotePropertyName enable_server -NotePropertyValue $true
  $j.api | Add-Member -Force -NotePropertyName interpreter_path -NotePropertyValue "$KiCad\bin\pythonw.exe"
  Copy-Item $c "$c.bak-z2ems" -Force
  $j | ConvertTo-Json -Depth 20 | Set-Content $c -Encoding UTF8
} else {
  Write-Warning "kicad_common.json not found (KiCad never started?) - enable Preferences > Plugins > IPC API server manually."
}

# 4. libraries
$srcFwd = $Src -replace '\\','/'
foreach ($t in @("sym-lib-table","fp-lib-table")) {
  $f = "$cfgdir\$t"
  if (-not (Test-Path $f)) { "($($t -replace '-lib-table','_lib_table')`n  (version 7)`n)`n" | Set-Content $f -Encoding UTF8 }
  $txt = [IO.File]::ReadAllText($f)
  if ($txt -notmatch '"z2ems"') {
    $uri = if ($t -eq "sym-lib-table") { "$srcFwd/library/z2ems.kicad_sym" } else { "$srcFwd/library/z2ems.pretty" }
    $line = "  (lib (name ""z2ems"")(type ""KiCad"")(uri ""$uri"")(options """")(descr ""Z2Labs EMS simulation ports""))"
    Copy-Item $f "$f.bak-z2ems" -Force
    $idx = $txt.LastIndexOf(")")
    [IO.File]::WriteAllText($f, $txt.Substring(0,$idx) + $line + "`n)`n", (New-Object Text.UTF8Encoding $false))
  }
}

# 5. python deps
& "$KiCad\bin\python.exe" -m pip install --quiet --no-warn-script-location kicad-python pytest

# 6. tests
Push-Location $Src
& "$KiCad\bin\python.exe" -m pytest -q tests
Pop-Location
Write-Host "Z2Labs EMS installed: $Src -> $plug\kicad-z2ems (restart KiCad)"
