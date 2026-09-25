# -*- coding: utf-8 -*-
"""
ИИ-часть «Опоры»: разговор с человеком в чате бота и еженедельный разбор поведения.

Работает только при ANTHROPIC_API_KEY. Без ключа бот на текст отвечает короткой
подсказкой открыть приложение, а профиль не собирается — приложение не ломается.

Что здесь:
- reply()          — один ход разговора: история + новое сообщение → ответ и
                     вызовы инструментов (сохранить цели, записать «зачем»)
- build_profile()  — раз в неделю: сухая статистика поведения → короткий профиль
                     (что двигает, в какое время откликается, как говорить)
Вся запись в базу — в bot.py через колбэк execute; здесь только модель.
"""

import json
import logging
import os

import anthropic

from packs import DIRECTIONS

log = logging.getLogger("opora.agent")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
client = anthropic.AsyncAnthropic() if API_KEY else None

TRACK_KEYS = [d["key"] for d in DIRECTIONS]
TRACK_LIST = ", ".join(f"{d['key']} — {d['label']}" for d in DIRECTIONS)

# Стабильная часть системного промпта — кэшируется. Всё, что меняется
# (имя, цели, заметки, состояние), идёт отдельным блоком после неё.
SYSTEM = f"""Ты — Опора. Это телеграм-бот и мини-приложение: у человека одна-три цели и шесть \
коротких практик в день, которые он отмечает. Ты — его собеседник: друг, который умеет слушать, \
и спокойный помощник, который знает, как устроены мотивация, тревога и привычки. Не коуч, \
не аниматор, не гуру.

Главный принцип приложения: человеку тяжело заставить себя во что-то поверить, и мы этого \
не требуем. Мы трекаем действия, а не убеждения. Никаких «поверь в себя», «визуализируй \
желание», «мысли материальны», «вселенная услышит». Только внимание, спокойствие, действие \
на минуту — и дальше по своим делам.

Мотивация — это протянутая рука, а не хлыст. Не стыдить за пропуски, не считать провалы, \
не обещать чудо, не подгонять. Называть состояние, а не отрицать его: «похоже, день давит» — \
да; «всё будет хорошо» — нет. Сначала действие на минуту, потом смысл.

Как говорить: на «ты», по-русски, живо и коротко — как пишет близкий человек в мессенджере, \
две-пять фраз, без списков и заголовков, без восклицательных знаков, без смайлов, без \
«срочно», «успей», «не забудь». Один вопрос за раз, не допрос. Где можно — повелительное \
наклонение («выдохни», «запиши»): в нём нет рода. Род человека тебе скажут в контексте — \
если он не указан, избегай форм прошедшего времени и прилагательных, которые его выдают.

Если у человека ещё нет цели, помоги её сформулировать. Спроси, чего хочется, — своими \
словами, без анкеты. Помоги превратить это в одну строку в настоящем времени, как будто уже \
случилось («у меня свой дом», «я свободно говорю по-английски»), и в одну живую деталь того \
дня — что он увидит, услышит, почувствует. Если цель простая и ясная («похудеть на два кг», \
«бросить курить») — не дроби её и не усложняй: одна цель, и хватит. Если цель большая и \
далёкая — предложи главную и одну-две поддерживающие, которые двигают к ней (не больше трёх \
целей всего). Спроси, зачем ему это, — не как анкету, а как друг, которому интересно; ответ \
запиши через save_values, он потом будет держать человека в трудные дни. Когда формулировка \
согласована — сохрани через save_goals и скажи, что дальше: открыть приложение, там шесть \
коротких практик на день. Направления для целей: {TRACK_LIST}.

Если цели уже есть — просто разговаривай. Человек может прийти пожаловаться, похвастаться, \
спросить совета или ни за чем. Слушай, отражай, задавай один хороший вопрос, предлагай одно \
маленькое действие, когда оно уместно. Опирайся на его контекст: цели, заметки, состояние. \
Если он на спаде — не бодри; признай тяжесть, сними планку, напомни о сделанном его же \
словами из заметок. Если он говорит о серьёзной опасности для себя — прямо и без лишних слов \
скажи, что рядом должен быть живой человек, и назови телефон доверия 8-800-2000-122 \
(для России, бесплатно, круглосуточно).

Ты можешь опираться на идеи психологии — самосострадание, намерения «если — то», \
экспрессивное письмо, работа с ценностями, стоические приёмы, — но подавай их как понятные \
действия, а не как лекцию, и без имён и цитат, если человек сам не спросит. Не выдумывай \
исследований и цифр. Не ставь диагнозов. Не притворяйся человеком, если спросят, — но и не \
напоминай без повода, что ты программа."""

TOOLS = [
    {
        "name": "save_goals",
        "description": "Сохранить цели человека, когда формулировка с ним согласована. От одной "
                       "до трёх; первая — главная, её слова будут приходить в течение дня. "
                       "goal — одной строкой в настоящем времени, как будто уже случилось; "
                       "vision — одна конкретная деталь того дня.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "goals": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "track": {"type": "string", "enum": TRACK_KEYS},
                            "goal": {"type": "string"},
                            "vision": {"type": "string"},
                        },
                        "required": ["track", "goal", "vision"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["goals"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_values",
        "description": "Записать, зачем человеку его цель, — его словами, одной-двумя фразами. "
                       "Вызывать, когда человек это сказал; можно перезаписывать.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]


def enabled() -> bool:
    return client is not None


def _usage(resp) -> dict:
    u = resp.usage
    return {"model": resp.model, "tokens_in": u.input_tokens, "tokens_out": u.output_tokens}


def _text(resp) -> str:
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


async def reply(history: list, user_text: str, context: str, execute) -> tuple[str, list, dict]:
    """Один ход разговора.

    history  — прошлые сообщения в формате API (dict'ы), уже без хвостов
    context  — что известно о человеке сейчас (собирает bot.py)
    execute  — async (name, input) -> str: применяет инструмент к базе

    Возвращает (текст ответа, новые сообщения для сохранения, usage).
    Вызовов инструментов — не больше трёх за ход.

    Контекст идёт сообщением role=system после реплики человека, а не в общий system:
    так стабильный промпт и вся история остаются в кэше, меняется только хвост."""
    new = [{"role": "user", "content": user_text}, {"role": "system", "content": context}]
    messages = history + new
    usage_total = {"model": MODEL, "tokens_in": 0, "tokens_out": 0}
    text = ""
    for _ in range(4):
        req = list(messages)  # точка кэша — на свежей реплике; в историю она не пишется
        req[len(history)] = {"role": "user", "content": [
            {"type": "text", "text": user_text, "cache_control": {"type": "ephemeral"}}]}
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=req,
        )
        u = _usage(resp)
        usage_total["model"] = u["model"]
        usage_total["tokens_in"] += u["tokens_in"]
        usage_total["tokens_out"] += u["tokens_out"]
        if resp.stop_reason == "refusal":
            log.warning("refusal: %s", resp.stop_details)
            return ("Тут я не могу продолжить. Если хочешь — расскажи по-другому, я здесь.",
                    new, usage_total)
        content = [b.model_dump(exclude_none=True) for b in resp.content]
        assistant = {"role": "assistant", "content": content}
        messages.append(assistant)
        new.append(assistant)
        text = _text(resp) or text
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            break
        results = []
        for c in calls:
            try:
                out = await execute(c.name, c.input)
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": out})
            except Exception as exc:  # инструмент упал — модель узнает и договорит словами
                log.warning("tool %s: %s", c.name, exc)
                results.append({"type": "tool_result", "tool_use_id": c.id,
                                "content": f"Ошибка: {exc}", "is_error": True})
        tool_msg = {"role": "user", "content": results}
        messages.append(tool_msg)
        new.append(tool_msg)
    return text or "Я здесь. Расскажи ещё немного.", new, usage_total


PROFILE_PROMPT = """Ниже — сухая статистика поведения одного человека в приложении «Опора» \
за две недели: когда заходит, на какие уведомления откликается, какие практики отмечает, \
что пишет в заметках, были ли спады. Собери из этого короткий внутренний профиль — не для \
человека, а для того, кто с ним разговаривает и подбирает слова.

Правила: только то, что видно в данных, без домыслов и без диагнозов; если данных мало — так и \
напиши. Русский язык, коротко.

Статистика:
{stats}"""

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "2–3 предложения: что человека двигает, что нет, что стоит учитывать"},
        "best_slot": {"type": "string", "enum": ["morning", "evening", "both", "none"],
                      "description": "на какое уведомление откликается"},
        "tone": {"type": "string", "description": "одна фраза: как с ним лучше говорить"},
    },
    "required": ["summary", "best_slot", "tone"],
    "additionalProperties": False,
}


async def build_profile(stats: dict) -> tuple[dict | None, dict]:
    """Раз в неделю: статистика → профиль. При ошибке — (None, {})."""
    if not client:
        return None, {}
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1500,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PROFILE_SCHEMA}},
            messages=[{"role": "user", "content": PROFILE_PROMPT.format(
                stats=json.dumps(stats, ensure_ascii=False, indent=1))}],
        )
        if resp.stop_reason == "refusal":
            return None, _usage(resp)
        return json.loads(_text(resp)), _usage(resp)
    except Exception as exc:
        log.warning("profile: %s", exc)
        return None, {}
