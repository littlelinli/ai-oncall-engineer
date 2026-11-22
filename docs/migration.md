# Переезд на новый AWS-аккаунт

Вся инфраструктура описана в `terraform/`. Состояние Terraform хранится в S3 (`terraform/bootstrap`).

## Шаги

1. В новом аккаунте создать бакет для состояния: `cd terraform/bootstrap && terraform init && terraform apply`.
2. В `terraform/providers.tf` вписать имя нового бакета состояния (backend "s3").
3. Удалить `terraform/imports.tf`: он нужен только старому аккаунту, где часть ресурсов сначала создали руками.
4. В `terraform/envs/dev.tfvars` (не в git) задать:
   1. `site_bucket_name`: новое имя, имена бакетов S3 уникальны во всём мире;
   2. `telegram_bot_token`, `telegram_chat_id`, `alert_emails`;
   3. `demo_server_id` пока пустой.
5. `cd terraform && terraform init && terraform apply -var-file=envs/dev.tfvars`.
6. На созданный демо-сервер поставить ShopSim (`sim/install_sim.sh`) и агента (команда со страницы «Подключить сервер» на сайте). ServerId агента вписать в `demo_server_id` и повторить apply.
7. Перенести датасет: `aws s3 sync s3://<старый бакет датасета> s3://<новый>` (нужен доступ к обоим аккаунтам) или скачать и загрузить заново.

Адреса API, /register, личного кабинета и настройки входа (Cognito) в `assets/config.js` сайта
и адрес /ingest в установщике агента Terraform подставляет сам. Вебхук Telegram-бота тоже
перенастраивается при apply.

Аккаунты пользователей (Cognito) не переносятся: на новом аккаунте все регистрируются заново
и заново подключают свои серверы и Telegram.
