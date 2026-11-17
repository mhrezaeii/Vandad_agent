[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ((Test-Path ".env") -and -not $Force) {
    throw ".env already exists. Keep it, or rerun with -Force to replace it."
}
if (-not (Test-Path ".env.example")) {
    throw ".env.example was not found."
}

function New-RandomBase64([int]$Length = 32) {
    $bytes = [byte[]]::new($Length)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}
function New-RandomHex([int]$Length = 32) {
    $bytes = [byte[]]::new($Length)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}
function Set-EnvLine([string]$Name, [string]$Value) {
    $script:content = [regex]::Replace(
        $script:content,
        "(?m)^$([regex]::Escape($Name))=.*$",
        "$Name=$Value"
    )
}

$content = Get-Content ".env.example" -Raw
Set-EnvLine "POSTGRES_PASSWORD" (New-RandomHex 24)
Set-EnvLine "REDIS_PASSWORD" (New-RandomHex 24)
Set-EnvLine "N8N_ENCRYPTION_KEY" (New-RandomHex 32)
Set-EnvLine "CREDENTIAL_MASTER_KEYS" ('{"1":"' + (New-RandomBase64 32) + '"}')
Set-Content -Path ".env" -Value $content -Encoding utf8

Write-Host ""
Write-Host "Created .env with unique local secrets." -ForegroundColor Green
Write-Host "Next: add your LLM/video provider credentials and configure the first company."
Write-Host "Do not regenerate CREDENTIAL_MASTER_KEYS after storing encrypted provider credentials."
Write-Host ""
Write-Host "Start infrastructure with: docker compose up -d --build"
Write-Host "Start UI with: docker compose --profile ui up -d --build web"
