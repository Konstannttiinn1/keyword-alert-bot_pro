param(
    [string]$Path = "qr_login.png"
)

$FullPath = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
if (-not $FullPath) {
    Write-Error "Файл не найден: $Path"
    exit 1
}

Start-Process $FullPath
Write-Host "Открыт файл: $FullPath"
