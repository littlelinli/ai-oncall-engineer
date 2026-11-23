# kezekshi-ml: нейросети в AWS

Lambda `kezekshi-ml` собирается из Docker-образа (этот каталог) и запускает модели в формате ONNX.
Собирает и выкладывает её `terraform apply` (terraform/ml.tf): образ пересобирается сам, когда
меняется любой файл здесь. Нужен Docker (в AWS CloudShell он есть).

Модели лежат в `models/`: `<имя>.onnx` и рядом `<имя>.json` с описанием. Нет файла = модели нет,
и система работает на своей простой модели (Holt + сезонность) и правилах.

## Как сдать модель (Алихан)

Обучаешь как удобно (PyTorch, Keras, sklearn), экспортируешь в ONNX и кладёшь два файла в `models/`.

### Прогноз: `forecast.onnx` + `forecast.json`

1. Вход: `x`, float32, форма `[1, window, F]`: последние `window` минут, признаки по порядку `features`,
   уже нормализованные: `(значение − mean) / std`.
2. Выход: float32, форма `[1, len(horizons), F]`: прогноз в той же нормализации, на каждый горизонт.
3. `forecast.json`:
```json
{"version": "gru-1", "window": 120, "features": ["cpu", "mem", "disk", "errors"],
 "horizons": [15, 30, 60], "mean": [..4 числа..], "std": [..4 числа..]}
```
Признаки, которые есть у агента: `cpu`, `mem`, `disk` (проценты), `errors` (ошибок в минуту),
`log_lines`, `auth`, `not_found`, `latency` (мс). Модель может брать любые из них.

### Причина: `cause.onnx` + `cause.json`

1. Вход: как у прогноза, окно обычно 10–15 минут.
2. Выход: float32 `[1, число классов]`: логиты (или вероятности, тогда `"outputs_probabilities": true`).
3. `cause.json`: `version`, `window`, `features`, `mean`, `std`, `classes` (например
   `["none", "payment_slow", "bad_deploy", "memory_leak", "log_storm", "cleanup_broken", "bot_attack", "cpu_hog", "app_down", "traffic_surge"]`).
   Анализатор берёт ответ, если вероятность не ниже 0,6.

### Аномалии: `anomaly.onnx` + `anomaly.json`

Автоэнкодер: вход и выход одной формы `[1, window, F]`. `anomaly.json`: `version`, `window`, `features`,
`mean`, `std`, `threshold` (ошибка восстановления, выше которой минута считается аномальной).

## Как проверить локально

```
pip install numpy onnxruntime
python3 -c "import handler, json; print(handler.handler({'task': 'info'}))"
```

`tools/make_test_model.py` делает тестовую модель прогноза «будет как сейчас». Модели с версией,
начинающейся на `test-`, не заменяют рабочий прогноз: их ответ только сохраняется рядом для сравнения.
