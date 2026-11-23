"""kezekshi-ml: запускает нейросети Kezekshi (формат ONNX) в Lambda из Docker-образа.

Его вызывают другие Lambda:
  kezekshi-predictor  -> {"task": "forecast", "series": ...}  прогноз на 15, 30, 60 минут
  incident-analyzer   -> {"task": "cause", "series": ...}     причина инцидента
  (любая)             -> {"task": "anomaly", "series": ...}   насколько странно ведёт себя сервер
  проверка            -> {"task": "info"}                      какие модели загружены

series = {"cpu": [[unix_ts, value], ...], "mem": [...], ...} поминутно, старые точки первыми.
Если нужной модели нет в образе, ответ {"available": false}, и вызывающий берёт свою
простую модель (Holt + сезонность) или правила. Поэтому образ можно выкатывать
без моделей, а модели добавлять по одной.

Как устроены файлы моделей (кладутся в src/ml/models/), см. src/ml/README.md.
"""
import json
import math
import os

import numpy as np
import onnxruntime as ort

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
_cache = {}


def load(name):
    """(сессия ONNX, описание из <name>.json) или None, если модели нет."""
    if name not in _cache:
        path = os.path.join(MODEL_DIR, f"{name}.onnx")
        if not os.path.exists(path):
            _cache[name] = None
        else:
            with open(os.path.join(MODEL_DIR, f"{name}.json")) as f:
                meta = json.load(f)
            _cache[name] = (ort.InferenceSession(path, providers=["CPUExecutionProvider"]), meta)
    return _cache[name]


def matrix(series, features, window):
    """Последние `window` минут как матрица [window, признаки].
    Пропуски заполняются последним известным значением, в начале нулём."""
    cols = []
    for f in features:
        pts = series.get(f) or []
        by_min = {int(ts // 60): float(v) for ts, v in pts}
        if not by_min:
            cols.append([0.0] * window)
            continue
        last = max(by_min)
        col, prev = [], None
        for m in range(last - window + 1, last + 1):
            v = by_min.get(m, prev)
            col.append(0.0 if v is None else v)
            prev = v if v is not None else prev
        cols.append(col)
    return np.array(cols, dtype=np.float32).T


def normalize(x, meta):
    mean = np.array(meta.get("mean", [0.0] * x.shape[-1]), dtype=np.float32)
    std = np.array(meta.get("std", [1.0] * x.shape[-1]), dtype=np.float32)
    return (x - mean) / np.where(std == 0, 1, std), mean, std


def run(session, x):
    name = session.get_inputs()[0].name
    return session.run(None, {name: x[np.newaxis, ...]})[0][0]


def task_forecast(series):
    m = load("forecast")
    if not m:
        return {"available": False}
    session, meta = m
    feats, horizons = meta["features"], meta["horizons"]
    x, mean, std = normalize(matrix(series, feats, meta["window"]), meta)
    y = run(session, x) * std + mean                       # [горизонты, признаки]
    out = {}
    for j, f in enumerate(feats):
        hi = 100.0 if f in ("cpu", "mem", "disk") else None
        vals = {}
        for i, h in enumerate(horizons):
            v = max(0.0, float(y[i][j]))
            vals[f"f{h}"] = round(min(v, hi) if hi else v, 2)
        out[f] = vals
    return {"available": True, "version": meta.get("version", "unknown"), "forecast": out}


def task_cause(series):
    m = load("cause")
    if not m:
        return {"available": False}
    session, meta = m
    x, _, _ = normalize(matrix(series, meta["features"], meta["window"]), meta)
    logits = run(session, x).astype(np.float64)
    if not meta.get("outputs_probabilities"):
        e = np.exp(logits - logits.max())
        logits = e / e.sum()
    k = int(np.argmax(logits))
    return {"available": True, "version": meta.get("version", "unknown"),
            "cause": meta["classes"][k], "probability": round(float(logits[k]), 3),
            "all": {c: round(float(p), 3) for c, p in zip(meta["classes"], logits)}}


def task_anomaly(series):
    m = load("anomaly")
    if not m:
        return {"available": False}
    session, meta = m
    x, _, _ = normalize(matrix(series, meta["features"], meta["window"]), meta)
    recon = run(session, x)
    score = float(np.mean((recon - x) ** 2))
    thr = float(meta.get("threshold", math.inf))
    return {"available": True, "version": meta.get("version", "unknown"),
            "anomaly_score": round(score, 4), "is_anomaly": score > thr, "threshold": thr}


def handler(event, context=None):
    task = (event or {}).get("task")
    series = (event or {}).get("series") or {}
    if task == "info":
        info = {}
        for name in ("forecast", "cause", "anomaly"):
            m = load(name)
            info[name] = m[1].get("version", "unknown") if m else None
        return {"available": True, "models": info}
    try:
        if task == "forecast":
            return task_forecast(series)
        if task == "cause":
            return task_cause(series)
        if task == "anomaly":
            return task_anomaly(series)
    except Exception as e:  # плохая модель не должна ронять прогноз: вызывающий возьмёт свою
        print(f"ERROR {task}: {e!r}")
        return {"available": False, "error": str(e)}
    return {"available": False, "error": f"unknown task {task!r}"}
