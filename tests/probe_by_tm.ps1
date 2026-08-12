# Диагностика by-tm: ищет, на какой позиции 1С отдаёт 500.
#
# ВАЖНО: файл сохранён в UTF-8 с BOM — Windows PowerShell 5.1 иначе читает
# кириллицу как ANSI. Вывод намеренно на латинице.
#
#   powershell -File tests\probe_by_tm.ps1 -Tm 000000215
#   powershell -File tests\probe_by_tm.ps1 -Tm 000000215 -Scan
#
# Токен берётся из .env, вставлять руками не нужно.

param(
    [Parameter(Mandatory = $true)][string]$Tm,
    [switch]$Scan,                     # перебрать позиции по одной
    [string]$EnvFile                   # по умолчанию .env рядом с репозиторием
)

if (-not $EnvFile) {
    # $PSScriptRoot в блоке param() ещё пуст, поэтому считаем путь здесь
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    $EnvFile = Join-Path (Split-Path -Parent $root) ".env"
}

$line = ((Get-Content $EnvFile) | Where-Object { $_ -match '^ONEC_TOKEN=' })
if (-not $line) { throw "ONEC_TOKEN not found in $EnvFile" }
$headers = @{ "X-API-Token" = ($line -split '=', 2)[1].Trim() }

$base = ((Get-Content $EnvFile) | Where-Object { $_ -match '^ONEC_BASE_URL=' })
$baseUrl = (($base -split '=', 2)[1].Trim()).TrimEnd('/')
$url = "$baseUrl/get-products/by-tm"

function Get-Batch($page, $size) {
    # сервис 1С периодически не принимает соединение — один повтор, иначе разрыв связи
    # не отличить от настоящей ошибки данных
    $r = Get-BatchOnce $page $size
    if (-not $r.ok -and $r.code -eq 0) { Start-Sleep -Seconds 2; $r = Get-BatchOnce $page $size }
    return $r
}

function Get-BatchOnce($page, $size) {
    try {
        $r = Invoke-WebRequest -Uri "${url}?tm=$Tm&page=$page&size=$size" `
            -Headers $headers -UseBasicParsing -TimeoutSec 120
        $text = $r.Content -replace "^﻿", ""
        return @{ ok = $true; code = $r.StatusCode; body = $text }
    } catch {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { return @{ ok = $false; code = 0; body = $_.Exception.Message } }
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd(); $reader.Close()
        return @{ ok = $false; code = $resp.StatusCode.value__; body = $body }
    }
}

Write-Host "TM $Tm -> $url" -ForegroundColor Cyan

foreach ($size in 1, 5, 10, 25, 50, 100, 200) {
    $r = Get-Batch 1 $size
    if ($r.ok) {
        $total = if ($r.body -match '"total"\s*:\s*(\d+)') { $Matches[1] } else { "?" }
        Write-Host ("  size={0,-4} OK    (total={1})" -f $size, $total) -ForegroundColor Green
    } else {
        $tail = if ($r.body) { $r.body.Substring(0, [Math]::Min(200, $r.body.Length)) }
                else { "(empty body -> neobrabotannoe iskluchenie v 1C)" }
        Write-Host ("  size={0,-4} HTTP {1}  {2}" -f $size, $r.code, $tail) -ForegroundColor Red
    }
}

if (-not $Scan) {
    Write-Host "`nZapusti s -Scan, chtoby nayti pervuyu padayuschuyu poziciyu." -ForegroundColor Yellow
    return
}

$first = Get-Batch 1 1
$total = if ($first.body -match '"total"\s*:\s*(\d+)') { [int]$Matches[1] } else { 0 }
Write-Host "`nPozicii po odnoy (vsego $total):" -ForegroundColor Cyan
$bad = @()
for ($n = 1; $n -le $total; $n++) {
    $r = Get-Batch $n 1
    if ($r.ok) {
        if ($r.body -match '"ref"\s*:\s*"([^"]+)"') { $ref = $Matches[1] } else { $ref = "?" }
        if ($r.body -match '"name"\s*:\s*"([^"]+)"') { $name = $Matches[1] } else { $name = "" }
        Write-Host ("  {0,3}. OK    {1,-14} {2}" -f $n, $ref, $name)
    } else {
        $bad += $n
        Write-Host ("  {0,3}. HTTP {1}" -f $n, $r.code) -ForegroundColor Red
    }
}
if ($bad.Count) {
    Write-Host "`nPadayut pozicii: $($bad -join ', ')" -ForegroundColor Red
    Write-Host "Pervaya: $($bad[0]) -- pokazhi razrabotchiku 1C etu i sosednie." -ForegroundColor Yellow
} else {
    Write-Host "`nVse pozicii otdayutsya po odnoy." -ForegroundColor Green
}
