// Настройки сайта. Пока API_URL пустой, дашборд показывает тестовые данные из mock-data.js.
// Когда №1 развернёт API Gateway, вставьте сюда адрес, например:
// API_URL: "https://abc123.execute-api.eu-central-1.amazonaws.com/prod"
window.APP_CONFIG = {
  API_URL: "https://qhp5ilkxhrkp6gjup25ejjrxfq0lyahc.lambda-url.eu-north-1.on.aws/",

  // Адрес демо-приложения на EC2 для кнопок «Сломать сервис».
  // Например: "http://ec2-1-2-3-4.eu-central-1.compute.amazonaws.com"
  DEMO_APP_URL: "",

  // Как часто обновлять список инцидентов, в секундах (0 = не обновлять)
  REFRESH_SECONDS: 15
};
