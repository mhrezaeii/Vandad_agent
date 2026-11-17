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
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes)
}
function New-RandomHex([int]$Length = 32) {
    $bytes = [byte[]]::new($Length)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
}
function Set-EnvLine([string]$Name, [string]$Value) {
    $script:content = [regex]::Replace(
        $script:content,
        "(?m)^$([regex]::Escape($Name))=.*$",
        "$Name=$Value"
    )
}

$content = Get-Content ".env.example" -Raw
$postgresPassword = New-RandomHex 24
$redisPassword = New-RandomHex 24
Set-EnvLine "POSTGRES_PASSWORD" $postgresPassword
Set-EnvLine "DATABASE_URL" "postgresql+asyncpg://vandad:$postgresPassword@localhost:15432/vandad_agent"
Set-EnvLine "REDIS_PASSWORD" $redisPassword
Set-EnvLine "REDIS_URL" "redis://:$redisPassword@localhost:6379/0"
Set-EnvLine "CREDENTIAL_MASTER_KEYS" ('{"1":"' + (New-RandomBase64 32) + '"}')
[System.IO.File]::WriteAllText((Join-Path $root ".env"), $content, [System.Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "Created .env with unique local secrets." -ForegroundColor Green
Write-Host "Next: add SCENE_VIDEO_KIE_API_KEY to .env, configure GPT from UI Settings, then bootstrap the first company."
Write-Host "Do not regenerate CREDENTIAL_MASTER_KEYS after storing encrypted provider credentials."
Write-Host ""
Write-Host "Start infrastructure with: docker compose up -d --build"
Write-Host "Start UI with: docker compose --profile ui up -d --build web"
