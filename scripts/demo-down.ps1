# Stops the demo server: EC2 is no longer billed.
# Disk, agent, IP, API, tables and the install.sh link stay. Start it again: .\scripts\demo-up.ps1
# Usage:  .\scripts\demo-down.ps1               (Terraform asks for yes)
#         .\scripts\demo-down.ps1 -auto-approve (no prompt)
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..\terraform")
try {
    if (-not (Test-Path .terraform)) {
        terraform init
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }

    terraform apply "-var-file=envs/dev.tfvars" "-var=app_running=false" @args
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    Write-Host "Server stopped. Note: a plain terraform apply without -var=app_running=false starts it again."
} finally {
    Pop-Location
}
