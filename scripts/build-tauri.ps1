# 构建 Tauri 桌面版（Windows PowerShell）
# 用法：
#   .\build-tauri.ps1              # 默认 release 构建
#   .\build-tauri.ps1 --debug      # debug 构建
#   .\build-tauri.ps1 --no-bundle  # 只编译二进制，不打包
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir '..')
$TauriDir = Join-Path $Root 'tauri'
Set-Location $TauriDir

# Rust / cargo 环境
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    $Cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
    if (Test-Path $Cargo) {
        $env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
    } else {
        Write-Error "未找到 cargo，请先安装 Rust: https://rustup.rs"
        exit 1
    }
}

$ArgsList = @()
if ($args.Count -gt 0) { $ArgsList = $args }

cargo tauri build @ArgsList
exit $LASTEXITCODE
