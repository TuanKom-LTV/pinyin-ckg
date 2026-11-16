# PinYin CKG - cài đặt trên máy VPS Windows (chạy 1 lần, qua CAI_DAT_VPS.bat)
# 1) Cài Python 3.12 nếu chưa có  2) Tạo .venv + thư viện  3) Tải cloudflared  4) Tìm MT5 và ghi MT5_PATH vào .env

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "PinYin - cai dat VPS"
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = "Gray") { Write-Host ("  " + $msg) -ForegroundColor $color }
function Fail($msg) { Say $msg Red; Read-Host "  Enter để thoát"; exit 1 }

Write-Host ""
Say "=== CÀI ĐẶT PINYIN CKG TRÊN VPS ===" Cyan
Write-Host ""

# ---------- 1. Python 3.12 ----------
function Find-Python {
    $c = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        "C:\Program Files\Python312\python.exe"
    )
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    try { $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null); if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "[1/4] Chưa có Python 3.12, đang tải và cài (khoảng 1-2 phút)..." Yellow
    $inst = Join-Path $env:TEMP "python-3.12.5-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.5/python-3.12.5-amd64.exe" -OutFile $inst -UseBasicParsing
    $p = Start-Process $inst -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_launcher=1","Include_test=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) { Fail "Cài Python lỗi (mã $($p.ExitCode))." }
    $py = Find-Python
    if (-not $py) { Fail "Đã cài Python nhưng không tìm thấy python.exe." }
}
Say "[1/4] Python: $py" Green

# ---------- 2. Môi trường .venv + thư viện ----------
$vpy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Say "[2/4] Tạo môi trường .venv..." Yellow
    & $py -m venv (Join-Path $root ".venv")
    if (-not (Test-Path $vpy)) { Fail "Không tạo được .venv." }
}
Say "[2/4] Cài thư viện (khoảng 1-3 phút)..." Yellow
$ErrorActionPreference = "Continue"
& $vpy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $vpy -m pip install -r (Join-Path $root "requirements.lock.txt") --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Say "Bản khóa phiên bản lỗi, thử cài bản thường..." Yellow
    & $vpy -m pip install -r (Join-Path $root "requirements.txt") --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Fail "Cài thư viện lỗi. Chụp màn hình gửi lại." }
}
$ErrorActionPreference = "Stop"
Say "[2/4] Thư viện: xong" Green

# ---------- 3. cloudflared ----------
$tools = Join-Path $root "tools"
New-Item -ItemType Directory -Force $tools | Out-Null
$cf = Join-Path $tools "cloudflared.exe"
if (-not (Test-Path $cf)) {
    Say "[3/4] Tải cloudflared..." Yellow
    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cf -UseBasicParsing
}
Say "[3/4] cloudflared: $(& $cf --version)" Green

# ---------- 4. Tìm MT5 ----------
$mt5 = $null
$run = Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1
if ($run) { $mt5 = $run.Path }
else {
    $found = @()
    foreach ($d in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "C:\", "D:\")) {
        if ($d -and (Test-Path $d)) {
            $found += Get-ChildItem $d -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName "terminal64.exe" } | Where-Object { Test-Path $_ }
        }
    }
    $found = $found | Select-Object -Unique
    if ($found.Count -eq 1) { $mt5 = $found[0] }
    elseif ($found.Count -gt 1) {
        Say "Tìm thấy nhiều MT5:" Yellow
        for ($i = 0; $i -lt $found.Count; $i++) { Say "  $($i+1). $($found[$i])" }
        $k = Read-Host "  Chọn số của MT5 đang đăng nhập tài khoản giao dịch"
        $mt5 = $found[[int]$k - 1]
    }
}
if (-not $mt5) { $mt5 = (Read-Host "  Không tìm thấy MT5. Dán đường dẫn đầy đủ tới terminal64.exe").Trim('"') }
if (-not (Test-Path $mt5)) { Fail "Không có file: $mt5" }

$envf = Join-Path $root ".env"
$lines = @(Get-Content $envf -Encoding UTF8 | Where-Object { $_ -notlike "MT5_PATH=*" })
$lines += "MT5_PATH=$mt5"
[IO.File]::WriteAllLines($envf, $lines, (New-Object System.Text.UTF8Encoding($false)))
Say "[4/4] MT5: $mt5" Green

# ---------- Kiểm tra kết nối MT5 ----------
Say "Kiểm tra kết nối MT5..." Yellow
$ErrorActionPreference = "Continue"
$chk = & $vpy -c "import MetaTrader5 as m,sys;ok=m.initialize(path=sys.argv[1],timeout=20000);a=m.account_info() if ok else None;print('OK',a.login,a.server,a.currency) if a else print('LOI',m.last_error());m.shutdown()" $mt5 2>&1
Say "$chk" $(if ("$chk" -like "OK*") { "Green" } else { "Red" })

Write-Host ""
Say "=== XONG ===" Cyan
Say "Bước tiếp: TẮT PinYin trên máy tính ở nhà trước (đóng cửa sổ CHAY_TU_XA, chạy GO_TU_DONG.bat)," White
Say "rồi trên VPS nhấp đúp BAT_TU_DONG.bat (tự chạy khi đăng nhập) và CHAY_TU_XA.bat." White
Write-Host ""
Read-Host "  Enter để đóng"
