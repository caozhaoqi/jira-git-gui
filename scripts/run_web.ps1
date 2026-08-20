# Electron / Web 版启动脚本（Windows PowerShell）
# 用法：
#   .\run_web.ps1              # 启动 API + Web 前端（自动打开浏览器）
#   .\run_web.ps1 --electron   # 启动 Electron 桌面应用
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir '..')
Set-Location $Root

if (Test-Path "$Root\venv\Scripts\python.exe") {
    $Py = "$Root\venv\Scripts\python.exe"
} elseif ($env:PYTHON) {
    $Py = $env:PYTHON
} else {
    $Py = "python"
}

if ($args.Count -gt 0 -and $args[0] -eq '--electron') {
    if (-not (Test-Path "$Root\electron\node_modules")) {
        Write-Host "首次运行，安装 Electron 依赖…"
        Set-Location "$Root\electron"
        npm install
        Set-Location $Root
    }
    Set-Location "$Root\electron"
    npx electron . ($args | Select-Object -Skip 1)
    exit $LASTEXITCODE
} else {
    Write-Host "启动 API 服务器…"
    Write-Host "浏览器访问 http://127.0.0.1:8787"
    Start-Process "http://127.0.0.1:8787"
    & $Py -m api.server @args
    exit $LASTEXITCODE
}
