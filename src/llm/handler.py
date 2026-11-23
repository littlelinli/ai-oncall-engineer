"""kezekshi-llm: своя языковая модель Kezekshi. Пишет «Вероятную причину» инцидента
живым языком на русском и английском по фактам, которые собрал анализатор.

Модель: открытая Qwen2.5-1.5B-Instruct (Apache 2.0) в формате GGUF, работает через llama.cpp
прямо в Lambda, без внешнего API. Файл модели лежит в S3 (MODEL_BUCKET/MODEL_KEY) и при
холодном старте копируется в /tmp. Позже модель можно заменить дообученной: тот же формат GGUF,
другой ключ в S3 (переменная llm_model_key в Terraform).

Вход:  {"facts": {...}, "draft": {"ru": "...", "en": "..."}, "langs": ["ru", "en"]}
Выход: {"available": true, "model": "...", "texts": {"ru": "...", "en": "..."}, "seconds": 12.3}
Текст проходит проверку (язык, длина, нет чисел, которых нет в фактах). Не прошёл: язык
пропускается, и анализатор оставляет шаблонный текст.
"""
import json
import os
import re
import time

import boto3

MODEL_BUCKET = os.environ.get("MODEL_BUCKET", "")
MODEL_KEY = os.environ.get("MODEL_KEY", "")
MODEL_NAME = os.environ.get("MODEL_NAME", MODEL_KEY.rsplit("/", 1)[-1])
MODEL_PATH = "/tmp/model.gguf"
MAX_CHARS = 600
_llm = None

SYSTEM = {
    "ru": ("Ты дежурный инженер. По фактам об инциденте на сервере объясни вероятную причину "
           "простым языком, 2–3 коротких предложения, по-русски. Используй только факты ниже. "
           "Не придумывай чисел, сервисов и событий, которых нет в фактах. Не пиши шаги и советы, "
           "только причину. Без приветствий, без списков, без markdown."),
    "en": ("You are an on-call engineer. From the facts about an incident on a server, explain the likely "
           "cause in plain English, 2–3 short sentences. Use only the facts below. Do not invent numbers, "
           "services or events that are not in the facts. Do not give steps or advice, only the cause. "
           "No greetings, no lists, no markdown."),
}


def get_llm():
    global _llm
    if _llm is None:
        from llama_cpp import Llama  # тяжёлый импорт: только при первом вызове
        if not os.path.exists(MODEL_PATH):
            t = time.time()
            boto3.client("s3").download_file(MODEL_BUCKET, MODEL_KEY, MODEL_PATH)
            print(f"model downloaded in {time.time() - t:.1f}s")
        _llm = Llama(model_path=MODEL_PATH, n_ctx=2048, n_threads=os.cpu_count() or 2, verbose=False)
    return _llm


NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numbers(text):
    out = set()
    for n in NUM.findall(text or ""):
        v = float(n.replace(",", "."))
        out.add(round(v, 1))
        out.add(float(round(v)))
    return out


def valid(text, lang, facts_text):
    """Причина отказа или None, если текст можно отправлять."""
    if not text or len(text) < 20:
        return "empty"
    if len(text) > MAX_CHARS:
        return "too long"
    letters = [c for c in text if c.isalpha()]
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    share = cyr / max(len(letters), 1)
    if lang == "ru" and share < 0.6:
        return "not russian"
    if lang == "en" and share > 0.05:
        return "not english"
    allowed = numbers(facts_text)
    extra = [n for n in numbers(text) if n not in allowed]
    if extra:
        return f"invented numbers {sorted(extra)[:5]}"
    if any(m in text for m in ("```", "**", "##")):
        return "markdown"
    return None


def clean(text):
    text = (text or "").strip().strip('"').strip()
    text = re.sub(r"^(Вероятная причина|Причина|Likely cause|Cause)\s*:\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text)


def handler(event, context=None):
    facts = (event or {}).get("facts") or {}
    draft = (event or {}).get("draft") or {}
    langs = [l for l in (event or {}).get("langs") or ["ru", "en"] if l in SYSTEM]
    if not MODEL_BUCKET or not MODEL_KEY:
        return {"available": False, "error": "model is not configured"}
    t0 = time.time()
    try:
        llm = get_llm()
    except Exception as e:
        print(f"ERROR load: {e!r}")
        return {"available": False, "error": str(e)}
    facts_text = json.dumps(facts, ensure_ascii=False)
    texts, rejected = {}, {}
    for lang in langs:
        user = (f"Факты (JSON):\n{facts_text}\n\nЧерновик объяснения:\n{draft.get(lang, '')}\n\n"
                "Перепиши черновик лучше, сохранив смысл и числа." if lang == "ru" else
                f"Facts (JSON):\n{facts_text}\n\nDraft explanation:\n{draft.get(lang, '')}\n\n"
                "Rewrite the draft better, keeping its meaning and numbers.")
        try:
            out = llm.create_chat_completion(
                messages=[{"role": "system", "content": SYSTEM[lang]}, {"role": "user", "content": user}],
                max_tokens=160, temperature=0.3, top_p=0.9)
            text = clean(out["choices"][0]["message"]["content"])
        except Exception as e:
            rejected[lang] = f"error {e!r}"
            continue
        why = valid(text, lang, facts_text + " " + draft.get(lang, ""))
        if why:
            rejected[lang] = why
            print(f"rejected {lang}: {why}: {text[:200]}")
        else:
            texts[lang] = text
    return {"available": True, "model": MODEL_NAME, "texts": texts, "rejected": rejected,
            "seconds": round(time.time() - t0, 1)}
