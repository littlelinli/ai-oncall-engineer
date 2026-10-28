# Останавливает демо-сервер после показа: EC2 больше не тарифицируется.
# Остаются диск, агент, IP, API, таблицы и ссылка на install.sh. Включить обратно: .\scripts\demo-up.ps1
# Запуск:  .\scripts\demo-down.ps1               (Terraform спросит yes)
#          .\scripts\demo-down.ps1 -auto-approve (без вопроса)
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..\terraform")
try {
    if (-not (Test-Path .terraform)) {
        terraform init
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }

    terraform apply "-var-file=envs/dev.tfvars" "-var=app_running=false" @args
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    Write-Host "Сервер остановлен. Важно: обычный terraform apply без -var=app_running=false снова его включит."
} finally {
    Pop-Location
}
