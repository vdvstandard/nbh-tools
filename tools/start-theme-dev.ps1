param(
    [string]$Store = "neighbourhood-arnhem.myshopify.com",
    [string]$ThemePath,
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 9292,
    [switch]$Open,
    [string]$StorePassword
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$documentsRoot = Split-Path -Parent $projectRoot

if (-not $ThemePath) {
    $ThemePath = Join-Path $documentsRoot "neighbourhood-theme"
}

$resolvedThemePath = Resolve-Path -LiteralPath $ThemePath
$themeLiquid = Join-Path $resolvedThemePath "layout\theme.liquid"

if (-not (Test-Path -LiteralPath $themeLiquid)) {
    throw "Theme path does not look like a Shopify theme: $resolvedThemePath"
}

$shopify = Get-Command shopify.cmd -ErrorAction SilentlyContinue
if (-not $shopify) {
    throw "Shopify CLI was not found. Install it first, then rerun this script."
}

$args = @(
    "theme",
    "dev",
    "--store",
    $Store,
    "--path",
    $resolvedThemePath,
    "--host",
    $HostAddress,
    "--port",
    $Port
)

if ($Open) {
    $args += "--open"
}

if ($StorePassword) {
    $args += @("--store-password", $StorePassword)
}

Write-Host "Starting Shopify theme dev"
Write-Host "Theme: $resolvedThemePath"
Write-Host "Store: $Store"
Write-Host "Local: http://$HostAddress`:$Port"
Write-Host ""

& $shopify.Source @args
