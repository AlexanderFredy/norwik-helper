# Одиночная запись цены через set-prices — чтобы поймать ошибку 1С с голым запросом.
#
# ВАЖНО: файл сохранён в UTF-8 с BOM — Windows PowerShell 5.1 иначе читает кириллицу
# как ANSI. Подписи в выводе намеренно на латинице по той же причине.
#
# Токен и адрес берутся из .env, вставлять руками не нужно.
#
# БЕЗ ПАРАМЕТРОВ ЦЕНЫ СКРИПТ НИЧЕГО НЕ МЕНЯЕТ: он читает текущую цену позиции и
# отправляет её же. Эндпоинт отрабатывает полностью, данные остаются прежними.
#
#   # безопасно: записать позиции её же текущую закупку
#   powershell -File tests\probe_set_prices.ps1
#
#   # только показать запрос, не отправляя
#   powershell -File tests\probe_set_prices.ps1 -DryRun
#
#   # другая позиция
#   powershell -File tests\probe_set_prices.ps1 -Ref YO-00036117 -Tm 000000303
#
#   # ИЗМЕНИТ ЦЕНУ В 1С
#   powershell -File tests\probe_set_prices.ps1 -Purchase 1500
#
#   # форма «а» — по коллекции целиком (ИЗМЕНИТ ЦЕНЫ ВСЕЙ КОЛЛЕКЦИИ)
#   powershell -File tests\probe_set_prices.ps1 -Tm 000000104 -CollectionRef YO-00074810 -Purchase 980

param(
    [string]$Ref = "YO-00036116",      # AGT Effect Premium Алтай, закупка 1490
    [string]$Tm  = "000000303",        # AGT
    [string]$CollectionRef,            # задан — уходит форма «а» вместо «б»
    [double]$Purchase,
    [double]$Rrc,
    [double]$Retail,
    [switch]$DryRun,
    [string]$EnvFile
)

$ErrorActionPreference = "Stop"

if (-not $EnvFile) {
    # $PSScriptRoot в блоке param() ещё пуст, поэтому считаем путь здесь
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    $EnvFile = Join-Path (Split-Path -Parent $root) ".env"
}
if (-not (Test-Path $EnvFile)) { throw ".env not found: $EnvFile" }

function Get-EnvValue([string]$name) {
    $line = (Get-Content $EnvFile) | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { throw "$name not found in $EnvFile" }
    return ($line -split '=', 2)[1].Trim()
}

$headers = @{ "X-API-Token" = (Get-EnvValue "ONEC_TOKEN") }
$baseUrl = (Get-EnvValue "ONEC_BASE_URL").TrimEnd('/')

# --- ответы 1С приходят с BOM, а PS 5.1 сам их декодирует неверно: читаем байтами
function Read-Utf8([byte[]]$bytes) {
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    return $text.TrimStart([char]0xFEFF)
}

function Invoke-Onec([string]$url, [string]$method, [string]$json) {
    $body = $null
    if ($json) { $body = [System.Text.Encoding]::UTF8.GetBytes($json) }
    try {
        $resp = Invoke-WebRequest -Uri $url -Method $method -Headers $headers -Body $body `
            -ContentType "application/json; charset=utf-8" -TimeoutSec 300 -UseBasicParsing
        return @{ ok = $true; code = [int]$resp.StatusCode
                  text = (Read-Utf8 $resp.RawContentStream.ToArray()) }
    } catch [System.Net.WebException] {
        # Самое ценное при отладке 1С лежит в ТЕЛЕ ошибки, а не в статусе.
        # Invoke-WebRequest его выбрасывает, поэтому читаем поток вручную.
        $r = $_.Exception.Response
        $code = 0; $text = $_.Exception.Message
        if ($r) {
            $code = [int]$r.StatusCode
            $reader = New-Object System.IO.StreamReader($r.GetResponseStream(),
                                                        [System.Text.Encoding]::UTF8)
            $text = $reader.ReadToEnd().TrimStart([char]0xFEFF)
            $reader.Close()
        }
        return @{ ok = $false; code = $code; text = $text }
    }
}

# --- цены: чего не задали, то и не трогаем
$prices = @{}
if ($PSBoundParameters.ContainsKey('Purchase')) { $prices["purchase"] = $Purchase }
if ($PSBoundParameters.ContainsKey('Rrc'))      { $prices["rrc"]      = $Rrc }
if ($PSBoundParameters.ContainsKey('Retail'))   { $prices["retail"]   = $Retail }

if ($prices.Count -eq 0) {
    if ($CollectionRef) {
        throw "Dlya formy 'a' ukazhite tsenu yavno: -Purchase <znachenie>"
    }
    Write-Host "Tsena ne zadana - chitayu tekushchuyu iz by-tm (zapis budet holostoy)..."
    $found = $null
    for ($page = 1; $page -le 20 -and -not $found; $page++) {
        $r = Invoke-Onec "$baseUrl/get-products/by-tm?tm=$Tm&page=$page&size=200" "GET" $null
        if (-not $r.ok) { throw "by-tm HTTP $($r.code): $($r.text)" }
        $data = $r.text | ConvertFrom-Json
        if (-not $data.items -or $data.items.Count -eq 0) { break }
        $found = $data.items | Where-Object { $_.ref -eq $Ref } | Select-Object -First 1
    }
    if (-not $found) { throw "Pozitsiya $Ref ne naydena u TM $Tm" }
    if (-not $found.prices.purchase) { throw "U $Ref net zakupochnoy tseny - zadayte -Purchase" }
    $prices["purchase"] = [double]$found.prices.purchase.value
    Write-Host ("  {0} | {1} | tekushchaya zakupka {2}" -f $found.ref, $found.name, $prices["purchase"])
}

# --- тело запроса: форма «б» (по товару) либо «а» (по коллекции)
if ($CollectionRef) {
    $item = @{ tm = $Tm; collection_ref = $CollectionRef; prices = $prices }
} else {
    $item = @{ tm = $Tm; ref = $Ref; prices = $prices }
}
$json = @{ items = @($item) } | ConvertTo-Json -Depth 6 -Compress:$false

$url = "$baseUrl/get-products/set-prices"
Write-Host ""
Write-Host "POST $url"
Write-Host "--- request body:"
Write-Host $json

if ($DryRun) { Write-Host ""; Write-Host "DryRun - nichego ne otpravleno."; return }

$res = Invoke-Onec $url "POST" $json
Write-Host ""
Write-Host ("--- HTTP {0} {1}" -f $res.code, $(if ($res.ok) { "OK" } else { "ERROR" }))
Write-Host "--- raw response:"
Write-Host $res.text

# Разобранный ответ читать удобнее, но сырой текст выше — то, что реально прислала 1С
try {
    $parsed = $res.text | ConvertFrom-Json
    if ($parsed.errors -and $parsed.errors.Count -gt 0) {
        Write-Host ""
        Write-Host "--- errors:"
        foreach ($e in $parsed.errors) {
            Write-Host ("  code={0} ref={1} message={2}" -f $e.code, $e.ref, $e.message)
        }
    }
} catch {
    Write-Host ""
    Write-Host "(otvet ne JSON - smotrite syroy tekst vyshe)"
}
