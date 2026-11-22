"""Модель прогноза для ML-агента Kezekshi.

Это базовая модель временных рядов без нейросетей:
1. Holt (сглаживание уровня и тренда) по последним 2 часам поминутных данных.
   Ловит то, что происходит прямо сейчас: нагрузка растёт, память утекает.
2. Суточная и недельная сезонность по истории за 7 дней с шагом 5 минут.
   Знает, что в 20:00 нагрузка обычно выше, чем в 19:30.
Итоговый прогноз = среднее двух, если история есть, иначе только Holt.

Эту модель потом заменяет модель Алихана (LSTM/GRU и т.д.).
Главное правило замены: сохранить функцию forecast() с тем же входом и выходом,
тогда lambda_function.py менять не нужно.
"""
from statistics import median

MODEL_VERSION = "baseline-holt-seasonal-v1"
HORIZONS = (15, 30, 60)  # минут вперёд
LIMITS = {"cpu": (0.0, 100.0), "mem": (0.0, 100.0), "disk": (0.0, 100.0), "errors": (0.0, None)}


def clip(x, lo, hi):
    x = max(lo, x)
    return min(hi, x) if hi is not None else x


def holt(values, alpha=0.5, beta=0.1, phi=0.9):
    """Затухающий тренд Холта. Возвращает функцию h -> прогноз через h шагов."""
    level, trend = values[0], 0.0
    for v in values[1:]:
        prev = level
        level = alpha * v + (1 - alpha) * (level + phi * trend)
        trend = beta * (level - prev) + (1 - beta) * phi * trend
    return lambda h: level + trend * sum(phi ** i for i in range(1, h + 1))


def slope_per_hour(points):
    """Наклон прямой по точкам [(ts_сек, значение)], в единицах в час."""
    if len(points) < 3:
        return 0.0
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    den = sum((t - mx) ** 2 for t, _ in points)
    if den == 0:
        return 0.0
    return sum((t - mx) * (v - my) for t, v in points) / den * 3600


def seasonal_delta(history, now_ts, h_min, days=7):
    """Насколько в прошлые дни менялось значение от «сейчас» до «сейчас + h».
    history: {ts_5мин: значение}. Медиана по дням устойчива к разовым всплескам."""
    t = int(now_ts // 300 * 300)
    h = int(round(h_min / 5.0)) * 300
    deltas = []
    for d in range(1, days + 1):
        a = history.get(t - d * 86400)
        b = history.get(t + h - d * 86400)
        if a is not None and b is not None:
            deltas.append(b - a)
    return median(deltas) if deltas else None


def forecast(series, now_ts):
    """series: {"cpu"|"mem"|"disk"|"errors": {"recent": [(ts, v)] поминутно за 2–3 часа,
                                               "history": [(ts, v)] по 5 минут за 7 дней}}
    Возвращает {метрика: {"now", "f15", "f30", "f60", "trend_per_hour", "method"}}.
    Метрики без данных пропускаются."""
    out = {}
    for name, s in series.items():
        recent = s.get("recent") or []
        if len(recent) < 5:
            continue
        lo, hi = LIMITS.get(name, (0.0, None))
        values = [v for _, v in recent[-120:]]
        now_value = sum(values[-3:]) / len(values[-3:])  # сглаживаем шум последней минуты
        # память и диск меняются плавно и долго: тренд почти не затухает
        h_fn = holt(values, phi=0.99) if name in ("mem", "disk") else holt(values)
        history = {int(ts // 300 * 300): v for ts, v in (s.get("history") or [])}
        res = {"now": round(now_value, 2)}
        used_season = False
        for h in HORIZONS:
            p = h_fn(h)
            delta = seasonal_delta(history, now_ts, h)
            if delta is not None:
                p = 0.5 * p + 0.5 * (now_value + delta)
                used_season = True
            res[f"f{h}"] = round(clip(p, lo, hi), 2)
        # тренд: диск меняется медленно, смотрим 6 часов; остальное за последний час
        if name == "disk":
            pts = [(ts, v) for ts, v in (s.get("history") or []) if ts >= now_ts - 6 * 3600]
        elif name == "mem":
            pts = recent[-120:]
        else:
            pts = recent[-60:]
        res["trend_per_hour"] = round(slope_per_hour(pts), 3)
        res["method"] = "holt+seasonal" if used_season else "holt"
        out[name] = res
    return out
