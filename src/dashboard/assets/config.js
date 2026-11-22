// Настройки сайта. Пока API_URL пустой, дашборд показывает тестовые данные из mock-data.js.
// Адреса без пробелов и без "/" в конце.
window.APP_CONFIG = {
  // API для дашборда: GET {API_URL}/incidents
  API_URL: "https://qhp5ilkxhrkp6gjup25ejjrxfq0lyahc.lambda-url.eu-north-1.on.aws",

  // Регистрация сервера для страницы «Подключить сервер» (POST, ответ с полем command)
  REGISTER_URL: "https://92kxgkp862.execute-api.eu-north-1.amazonaws.com/register",

  // Адрес демо-приложения на EC2 для кнопок «Сломать сервис».
  DEMO_APP_URL: "",

  // Как часто обновлять список инцидентов, в секундах (0 = не обновлять)
  REFRESH_SECONDS: 15
};
