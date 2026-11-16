# PinYin - khởi động MT5 + máy chủ PinYin + đường link truy cập từ xa (Cloudflare Tunnel)
# Nhấp đúp CHAY_TU_XA.bat, hoặc để Windows tự chạy khi đăng nhập (lối tắt trong thư mục Startup).
# Cửa sổ này phải mở thì link mới chạy. Script tự bật lại máy chủ / link nếu bị tắt.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "PinYin - truy cap tu xa"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$port = 8000
$base = "http://127.0.0.1:$port"
function EnvVal($name) {
    $f = Join-Path $root ".env"
    if (-not (Test-Path $f)) { return $null }
    $l = Get-Content $f -Encoding UTF8 | Where-Object { $_ -like "$name=*" } | Select-Object -First 1
    if ($l) { return ($l -replace "^$name=", "").Trim() } else { return $null }
}
# cloudflared: bản đi kèm trong thư mục tools (máy VPS), không có thì bản cài sẵn
$cf = Join-Path $root "tools\cloudflared.exe"
if (-not (Test-Path $cf)) { $cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
# MT5: MT5_PATH trong .env (máy VPS), không có thì đường dẫn máy nhà
$mt5 = EnvVal "MT5_PATH"
if (-not $mt5) { $mt5 = "C:\Program Files\MetaTrader 5 - Copy\terminal64.exe" }
$log = Join-Path $root "data\cloudflared.log"
$linkFile = Join-Path $root "data\link_tu_xa.txt"
New-Item -ItemType Directory -Force (Join-Path $root "data") | Out-Null

function Say($msg, $color = "Gray") { Write-Host ("[" + (Get-Date -Format "HH:mm:ss") + "] " + $msg) -ForegroundColor $color }

function Get-Code($url, $headers) {
    try { (Invoke-WebRequest -Uri $url -Headers $headers -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0).StatusCode }
    catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
}

function Start-MT5 {
    if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue) -and (Test-Path $mt5)) {
        Say "Mở MT5..." Yellow
        Start-Process -FilePath $mt5
    }
}

function Start-Server {
    if ((Get-Code "$base/api/v1/health" @{}) -eq 200) { return $true }
    # Cổng 8000 vẫn bị chiếm nhưng không trả lời = máy chủ treo: kiểm tra lại 1 lần rồi tắt hẳn để bật mới
    $hung = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($hung) {
        Start-Sleep 3
        if ((Get-Code "$base/api/v1/health" @{}) -eq 200) { return $true }
        Say "Máy chủ PinYin bị treo, đang khởi động lại..." Yellow
        $hung | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        Start-Sleep 2
    }
    Say "Khởi động máy chủ PinYin..." Yellow
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
        -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$port" `
        -WorkingDirectory $root -WindowStyle Minimized
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep 1; if ((Get-Code "$base/api/v1/health" @{}) -eq 200) { return $true } }
    return $false
}

function Test-Guard {
    # Chốt an toàn: request đi qua tunnel bắt buộc bị chặn khi chưa đăng nhập
    return ((Get-Code "$base/api/v1/health" @{ "Cf-Connecting-IP" = "203.0.113.1" }) -eq 401)
}

# Link cố định qua tên miền riêng (Cloudflare named tunnel). Không có cấu hình thì dùng link tạm trycloudflare.
$cfConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
$fixedUrl = "https://pinyin.id.vn"
# Gói cài VPS mang theo chìa khóa đường hầm trong thư mục tunnel\: tự viết config trỏ đúng đường dẫn máy này
$tdir = Join-Path $root "tunnel"
$cred = Get-ChildItem $tdir -Filter "*.json" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cred) {
    $cfConfig = Join-Path $tdir "config.yml"
    $yml = "tunnel: $($cred.BaseName)`r`ncredentials-file: $($cred.FullName)`r`n`r`ningress:`r`n  - hostname: pinyin.id.vn`r`n    service: http://127.0.0.1:$port`r`n  - service: http_status:404`r`n"
    [IO.File]::WriteAllText($cfConfig, $yml)
}

function Start-Tunnel {
    if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
    if (Test-Path $cfConfig) {
        $p = Start-Process -FilePath $cf -ArgumentList "tunnel","--no-autoupdate","--config","`"$cfConfig`"","--logfile","`"$log`"","run" -WindowStyle Hidden -PassThru
        for ($i = 0; $i -lt 45; $i++) {
            Start-Sleep 1
            if ($p.HasExited) { break }
            $txt = Get-Content $log -Raw -ErrorAction SilentlyContinue
            if ($txt -match "Registered tunnel connection") { return @{ proc = $p; url = $fixedUrl } }
        }
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        return $null
    }
    $p = Start-Process -FilePath $cf -ArgumentList "tunnel","--no-autoupdate","--url",$base,"--logfile","`"$log`"" -WindowStyle Hidden -PassThru
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep 1
        if ($p.HasExited) { break }
        if (Test-Path $log) {
            $txt = Get-Content $log -Raw -ErrorAction SilentlyContinue
            if ($txt -match "Registered tunnel connection" -and $txt -match "https://[a-z0-9-]+\.trycloudflare\.com") {
                return @{ proc = $p; url = $Matches[0] }
            }
        }
    }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    return $null
}

function Show-Link($url) {
    Set-Content -Path $linkFile -Value $url -Encoding UTF8
    try { Set-Clipboard -Value "$url/ckg/" } catch {}
    $pw = (Get-Content (Join-Path $root ".env") | Where-Object { $_ -like "PINYIN_PASSWORD=*" }) -replace "^PINYIN_PASSWORD=", ""
    Write-Host ""
    Write-Host "  ĐƯỜNG LINK TRUY CẬP TỪ XA (đã chép vào clipboard, lưu ở data\link_tu_xa.txt):" -ForegroundColor Green
    Write-Host "    CKG        : $url/ckg/" -ForegroundColor Cyan
    Write-Host "    Mật khẩu   : $pw" -ForegroundColor Cyan
    Write-Host ""
    if ($url -eq $fixedUrl) { Write-Host "  Link cố định. Đóng cửa sổ này = tắt link." -ForegroundColor DarkGray }
    else { Write-Host "  Link đổi mỗi lần mở lại. Đóng cửa sổ này = tắt link." -ForegroundColor DarkGray }
}

if (-not (Test-Path $cf)) { Say "Chưa cài cloudflared. Chạy: winget install Cloudflare.cloudflared" Red; Read-Host "Enter để thoát"; exit 1 }

Start-MT5

# Vòng giám sát: tự bật lại máy chủ / link khi bị tắt hoặc mất mạng
$t = $null
try {
    while ($true) {
        if (-not (Start-Server)) { Say "Chưa khởi động được máy chủ PinYin, thử lại sau 15 giây." Red; Start-Sleep 15; continue }
        if (-not (Test-Guard)) {
            Say "DỪNG: máy chủ đang chạy chưa bật mật khẩu - không mở link. Tắt máy chủ cũ rồi chạy lại file này." Red
            Read-Host "Enter để thoát"; exit 1
        }
        if (-not $t -or $t.proc.HasExited) {
            if ($t) { Say "Đường link bị ngắt, đang mở lại..." Yellow } else { Say "Đang mở đường link..." Yellow }
            $t = Start-Tunnel
            if ($t) { Show-Link $t.url } else { Say "Chưa mở được link (có thể chưa có mạng), thử lại sau 15 giây." Red; Start-Sleep 15; continue }
        }
        Start-Sleep 15
    }
}
finally {
    if ($t) { Stop-Process -Id $t.proc.Id -Force -ErrorAction SilentlyContinue }
}
