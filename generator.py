# -*- coding: utf-8 -*-
"""
Разовая генерация персонального набора фраз под конкретную цель.

Вызывается один раз при постановке цели. Результат кладётся в базу и дальше
раздаётся бесплатно. Если ключа нет или запрос не прошёл — работает запасной
пак из content.py, приложение при этом не ломается.
"""

import json
import logging
import os
import re

import aiohttp

import content as C

log = logging.getLogger("opora.gen")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
URL = "https://api.anthropic.com/v1/messages"

# цена за миллион токенов, доллары — чтобы /lyudi показывала расход
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = next((v for k, v in PRICES.items() if model.startswith(k)), (5.0, 25.0))
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000

PROMPT = """Ты пишешь короткие тексты для приложения-трекера «Опора». Человек идёт к своей цели \
и получает не более двух уведомлений в день. Тексты должны снижать напряжение, а не подгонять.

Цель человека, его словами: {goal}
Как он описал момент, когда цель достигнута: {vision}
Грамматический род, в котором к нему обращаться: {gender}

Правила:
— обращение на «ты», по-русски, спокойно и по-человечески
— не более 90 символов в строке, одна мысль в строке
— без восклицательных знаков, без «срочно», «успей», «не забудь», без смайлов
— не стыдить за пропуски, не считать провалы, не обещать чудо
— не утверждать, что мысли сами меняют внешние события; писать о внимании, спокойствии и действии
— не цитировать книги и не упоминать авторов
— опираться на конкретику цели человека: его слова, его детали, его обстановку
— где возможно, использовать повелительное наклонение (в нём нет рода)

Верни ТОЛЬКО JSON, без пояснений и без markdown-заборов, вида:
{{
  "daily": [40 строк — мысль дня: поддержка, снижение важности, возврат внимания к действию],
  "low": [10 строк — для дней, когда человек опустил руки: признать тяжесть, снять планку, \
напомнить о сделанном; в двух-трёх строках можно использовать плейсхолдер {{count}} — вместо него подставится «23 отметки» с правильным склонением],
  "sos_close": [5 строк — завершение минутной практики успокоения: вернуть чувство опоры и контроля],
  "morning": [8 строк — утреннее уведомление, очень короткое, до 60 символов],
  "evening": [8 строк — вечернее уведомление, очень короткое, до 60 символов],
  "affirm": "одна фраза в настоящем времени, которую человеку предлагается говорить вслух"
}}"""


def _clean(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end != -1 else text


def _validate(data: dict) -> dict:
    """Оставляем только то, что пришло в нужном виде; чего не хватило — берём из запасного пака."""
    out = {}
    for key in ("daily", "low", "sos_close", "morning", "evening"):
        items = [
            s.strip() for s in data.get(key, [])
            if isinstance(s, str) and 3 < len(s.strip()) <= 140
        ]
        out[key] = items if len(items) >= 3 else list(C.FALLBACK_PACK[key])
    affirm = data.get("affirm", "")
    out["affirm"] = affirm.strip() if isinstance(affirm, str) and len(affirm.strip()) <= 140 else ""
    return out


async def build_pack(goal: str, vision: str, gender: str) -> tuple[dict, dict]:
    """Возвращает (персональный пак, usage). При любой ошибке — запасной пак и пустой usage.
    usage: {"model", "tokens_in", "tokens_out"} — бот пишет это в базу для учёта расходов."""
    if not API_KEY:
        log.info("нет ANTHROPIC_API_KEY, работаем на запасном паке")
        return dict(C.FALLBACK_PACK), {}

    body = {
        "model": MODEL,
        "max_tokens": 12000,   # модель думает перед ответом, это тоже входит в лимит
        "messages": [{
            "role": "user",
            "content": PROMPT.format(
                goal=goal or "не указана",
                vision=vision or "не описан",
                gender=C.GENDER_LABEL.get(gender, "не указан"),
            ),
        }],
    }
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(URL, json=body, headers=headers) as r:
                if r.status != 200:
                    log.warning("api %s: %s", r.status, (await r.text())[:200])
                    return dict(C.FALLBACK_PACK), {}
                data = await r.json()
        u = data.get("usage", {})
        usage = {"model": data.get("model", MODEL),
                 "tokens_in": int(u.get("input_tokens", 0)),
                 "tokens_out": int(u.get("output_tokens", 0))}
        if data.get("stop_reason") == "refusal":
            log.warning("модель отказалась отвечать: %s", data.get("stop_details"))
            return dict(C.FALLBACK_PACK), usage
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        pack = _validate(json.loads(_clean(text)))
        log.info("пак собран: %s дневных фраз, %s/%s токенов",
                 len(pack["daily"]), usage["tokens_in"], usage["tokens_out"])
        return pack, usage
    except Exception as exc:
        log.warning("генерация не удалась: %s", exc)
        return dict(C.FALLBACK_PACK), {}
