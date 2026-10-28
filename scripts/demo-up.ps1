# Включает демо-сервер перед показом. IP не меняется (Elastic IP), агент и логи на месте.
# Запуск:  .\scripts\demo-up.ps1               (Terraform спросит yes)
#          .\scripts\demo-up.ps1 -auto-approve (без вопроса)
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
    Write-Host "Жду, пока приложение ответит на $url/health ..."
    for ($i = 0; $i -lt 30; $i++) {
        try {
            Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
            Write-Host "Готово: $url"
            exit 0
        } catch {
            Start-Sleep -Seconds 10
        }
    }
    Write-Host "Сервер запущен, но приложение не отвечает 5 минут. Проверьте: terraform output instance_id, затем aws ssm start-session --target <id>"
    exit 1
} finally {
    Pop-Location
}
