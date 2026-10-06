# 一键构建 + 自检（可选真实翻译测试）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build\build.ps1
#   powershell -ExecutionPolicy Bypass -File build\build.ps1 -ApiKey sk-xxx -TranslateTest
#
# 产物：dist\PDF中英对照翻译\  （exe + _internal + 官方离线资源包 + 合规文件）
param(
    [string]$ApiKey = "",
    [switch]$TranslateTest,
    [switch]$SkipAssets,
    [switch]$NoClean
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Step($n, $text) { Write-Host "`n=== [$n] $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

if (-not (Test-Path $py)) { Fail "virtualenv not found: $py`nRun 启动.cmd first, or create .venv and install requirements.txt" }

# 打包前结束旧进程，否则 _internal 里的文件被占用
Get-Process 'PDF中英对照翻译' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "killing old process PID $($_.Id)"
    Stop-Process -Id $_.Id -Force
}
Start-Sleep -Seconds 2

Step 1 'refresh third-party license list'
& $py build\gen_third_party_notices.py

Step 2 'PyInstaller build'
$pyArgs = @('-m', 'PyInstaller', '--noconfirm')
if (-not $NoClean) { $pyArgs += '--clean' }
$pyArgs += @('--log-level', 'ERROR', 'build\pdf-translator.spec')
Write-Host ("  cmd: python " + ($pyArgs -join ' '))
& $py $pyArgs
if (-not (Test-Path "dist\PDF中英对照翻译\PDF中英对照翻译.exe")) { Fail 'build failed: exe not found' }

Step 3 'assemble dist (trim + assets + license + launcher)'
$asmArgs = @('build\assemble_dist.py')
if ($SkipAssets) { $asmArgs += '--skip-assets' }
& $py $asmArgs
if ($LASTEXITCODE -ne 0) { Fail 'assemble failed' }

Step 4 'selftest (frozen)'
Push-Location 'dist\PDF中英对照翻译'
& '.\PDF中英对照翻译.exe' --selftest
$selfTest = $LASTEXITCODE
Pop-Location
if ($selfTest -ne 0) { Fail "selftest failed (exit $selfTest)" }

Step 5 'window selftest (native WebView2 window, auto-close)'
Push-Location 'dist\PDF中英对照翻译'
$env:WINDOW_TEST_SECONDS = '6'
$env:PORT = '8859'
& '.\PDF中英对照翻译.exe' --window-test
$winTest = $LASTEXITCODE
Pop-Location
Remove-Item Env:\WINDOW_TEST_SECONDS -ErrorAction SilentlyContinue
if ($winTest -ne 0) { Write-Host 'WARN: native window unavailable; app will fall back to browser' -ForegroundColor Yellow }

if ($TranslateTest) {
    if (-not $ApiKey) { Fail '-TranslateTest requires -ApiKey' }
    Step 6 'end-to-end translation test via packaged exe'
    $env:PORT = '8851'
    $exe = Join-Path $root 'dist\PDF中英对照翻译\PDF中英对照翻译.exe'
    $srv = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
    try {
        $ready = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Seconds 3
            try { $null = Invoke-RestMethod "http://127.0.0.1:$($env:PORT)/api/health" -TimeoutSec 5; $ready = $true; break } catch {}
        }
        if (-not $ready) { Fail 'server did not become ready' }
        & $py build\verify_exe.py $ApiKey $env:PORT 1
        if ($LASTEXITCODE -ne 0) { Fail 'end-to-end test failed' }
    } finally {
        if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue }
        Get-Process 'PDF中英对照翻译' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

$size = (Get-ChildItem 'dist\PDF中英对照翻译' -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("`nBUILD OK: dist\PDF中英对照翻译  ({0:N0} MB)" -f $size) -ForegroundColor Green
