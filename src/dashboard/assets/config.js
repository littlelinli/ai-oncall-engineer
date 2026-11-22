// Настройки сайта. Значения адресов подставляет Terraform (terraform/site.tf) при выкладке,
// поэтому здесь в репо они могут быть пустыми или старыми. Адреса без "/" в конце.
window.APP_CONFIG = {
  // Публичное API: инциденты демо-сервера для тех, кто не вошёл (GET {API_URL}/incidents)
  API_URL: "",

  // Регистрация сервера (POST, нужен вход)
  REGISTER_URL: "",

  // Личный кабинет: GET {ACCOUNT_API_URL}/me, /me/servers, /me/incidents ... (нужен вход)
  ACCOUNT_API_URL: "",

  // Вход через Amazon Cognito
  COGNITO_REGION: "",
  COGNITO_CLIENT_ID: "",

  // Как часто обновлять дашборд, в секундах (0 = не обновлять)
  REFRESH_SECONDS: 30
};
