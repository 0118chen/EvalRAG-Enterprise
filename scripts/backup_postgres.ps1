param(
    [string]$OutputDirectory = ".\backups",
    [string]$ComposeFile = ".\deploy\docker-compose.production.yml",
    [string]$EnvFile = ".\.env.production",
    [string]$ProjectName = "evalrag-production",
    [string]$DatabaseName = "evalrag"
)

$ErrorActionPreference = "Stop"
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $resolvedOutput "evalrag-$timestamp.sql"

docker compose --project-name $ProjectName --env-file $EnvFile -f $ComposeFile exec -T postgres `
    pg_dump -U evalrag -d $DatabaseName | Set-Content -LiteralPath $target -Encoding utf8

if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed"
}

Write-Output "Backup written to $target"
