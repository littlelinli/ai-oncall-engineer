# Starts the demo server. The IP does not change (Elastic IP); agent and logs are kept.
# Usage:  .\scripts\demo-up.ps1               (Terraform asks for yes)
#         .\scripts\demo-up.ps1 -auto-approve (no prompt)
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..\terraform")
try {
    if (-not (Test-Path .terraform)) {
        terraform init
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }

    terraform apply "-var-file=envs/dev.tfvars" "-var=app_running=true" @args
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    $url = terraform output -raw app_url
    Write-Host "Waiting for the app to answer on $url/health ..."
    for ($i = 0; $i -lt 30; $i++) {
        try {
            Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
            Write-Host "Ready: $url"
            exit 0
        } catch {
            Start-Sleep -Seconds 10
        }
    }
    Write-Host "The server is running but the app has not answered for 5 minutes. Check: terraform output instance_id, then aws ssm start-session --target <id>"
    exit 1
} finally {
    Pop-Location
}
