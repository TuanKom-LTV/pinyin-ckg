# PinYin CKG - lấy code mới nhất từ GitHub về VPS rồi khởi động lại máy chủ (link không đổi)
# Nhấp đúp CAP_NHAT_VPS.bat. Lần đầu: tự cài Git và mở trình duyệt để đăng nhập GitHub 1 lần.
# .env, tunnel\, tools\, .venv\, data\ không bị đụng tới (không nằm trên GitHub).

param([switch]$Auto)   # -Auto: chạy im lặng (dùng cho tự cập nhật), không hỏi gì

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "PinYin - cap nhat"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$repo = (Get-Content (Join-Path $root "REPO.txt") -Encoding UTF8 | Select-Object -First 1).Trim()
$branch = "main"

function Say($msg, $color = "Gray") { Write-Host ("  " + $msg) -ForegroundColor $color }
function Done($code) { if (-not $Auto) { Write-Host ""; Read-Host "  Enter để đóng" }; exit $code }

# ---------- Git ----------
function Find-Git {
    $g = Get-Command git -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    foreach ($p in @("C:\Program Files\Git\cmd\git.exe", (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"))) { if (Test-Path $p) { return $p } }
    return $null
}
$git = Find-Git
if (-not $git) {
    if ($Auto) { exit 1 }
    Say "Chưa có Git, đang tải và cài (1-2 phút)..." Yellow
    $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -UseBasicParsing
    $asset = $rel.assets | Where-Object { $_.name -match '^Git-[\d\.]+-64-bit\.exe$' } | Select-Object -First 1
    if (-not $asset) { Say "Không tìm được bộ cài Git." Red; Done 1 }
    $inst = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $inst -UseBasicParsing
    Start-Process $inst -ArgumentList "/VERYSILENT","/NORESTART","/SUPPRESSMSGBOXES" -Wait
    $git = Find-Git
    if (-not $git) { Say "Cài Git xong nhưng không tìm thấy git.exe." Red; Done 1 }
}

# ---------- Lần đầu: biến thư mục đang chạy thành bản sao của GitHub ----------
if (-not (Test-Path (Join-Path $root ".git"))) {
    if ($Auto) { exit 1 }
    Say "Lần đầu: nối thư mục này với $repo" Yellow
    Say "Nếu trình duyệt hiện trang đăng nhập GitHub thì đăng nhập và bấm Authorize." Yellow
    & $git init -q
    & $git remote add origin $repo
}

$old = (& $git rev-parse --short HEAD 2>$null)
& $git fetch -q origin $branch
if ($LASTEXITCODE -ne 0) { Say "Không lấy được code từ GitHub (chưa đăng nhập hoặc mất mạng)." Red; Done 1 }
$new = (& $git rev-parse --short "origin/$branch")
if ($old -eq $new) { Say "Đã là bản mới nhất ($new)." Green; Done 0 }

$reqOld = if (Test-Path "requirements.lock.txt") { (Get-FileHash "requirements.lock.txt").Hash } else { "" }
& $git reset -q --hard "origin/$branch"
Say "Đã cập nhật: $old -> $new" Green
& $git log --oneline -5 "origin/$branch" | ForEach-Object { Say "   $_" }

# Thư viện đổi thì cài lại
if ((Get-FileHash "requirements.lock.txt").Hash -ne $reqOld) {
    Say "Danh sách thư viện thay đổi, đang cài..." Yellow
    & (Join-Path $root ".venv\Scripts\python.exe") -m pip install -r requirements.lock.txt --quiet --disable-pip-version-check
}

# Khởi động lại máy chủ: cửa sổ CHAY_TU_XA tự bật lại sau ~10 giây, link giữ nguyên
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Say "Đã khởi động lại máy chủ PinYin. Chờ ~15 giây rồi Ctrl+F5 trên trình duyệt." Green
Say "(Nếu có sửa CHAY_TU_XA.ps1 thì đóng cửa sổ PinYin và mở lại CHAY_TU_XA.bat.)" DarkGray
Done 0
