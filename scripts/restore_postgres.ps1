param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,
    [string]$ComposeFile = ".\deploy\docker-compose.production.yml",
    [string]$EnvFile = ".\.env.production",
    [string]$ProjectName = "evalrag-production",
    [string]$DatabaseName = "evalrag"
)

$ErrorActionPreference = "Stop"
$resolvedBackup = [System.IO.Path]::GetFullPath($BackupFile)
if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Leaf)) {
    throw "Backup file not found: $resolvedBackup"
}

Get-Content -Raw -LiteralPath $resolvedBackup | docker compose `
    --project-name $ProjectName `
    --env-file $EnvFile `
    -f $ComposeFile `
    exec -T postgres psql -U evalrag -d $DatabaseName

if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL restore failed"
}

Write-Output "Restore completed from $resolvedBackup"
