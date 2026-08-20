# Jira Git 通用拉取工具 —— Windows 一键启动（PowerShell）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir '..')
Set-Location $Root

if (Test-Path "$Root\venv\Scripts\python.exe") {
    $Py = "$Root\venv\Scripts\python.exe"
} elseif (Test-Path "$Root\venv\bin\python") {
    $Py = "$Root\venv\bin\python"
} elseif ($env:PYTHON) {
    $Py = $env:PYTHON
} else {
    $Py = "python"
}

& $Py main.py @args
exit $LASTEXITCODE
