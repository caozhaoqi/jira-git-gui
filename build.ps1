# 跨平台构建脚本的 Windows (PowerShell) 薄包装：切到项目根目录后调用 Python 编排器。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path $ScriptDir
Set-Location $Root

$Py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $Py build\build.py @args
exit $LASTEXITCODE
