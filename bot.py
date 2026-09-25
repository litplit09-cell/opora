import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    MenuButtonWebApp,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import content as C
from generator import build_pack, cost_usd
from packs import match_pack, pack_by_key, DIRECTIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("opora")

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)  # кому доступна /lyudi
DB_PATH = os.environ.get("DB_PATH") or ("/data/opora.db" if Path("/data").is_dir() else "opora.db")

DAY_GOAL = int(os.environ.get("DAY_GOAL", "3"))
MIN_GOAL = 1  # планка в дни спада

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
PRACTICE_KEYS = {p["key"] for p in C.PRACTICES}
NOTE_KEYS = {p["key"] for p in C.PRACTICES if p.get("note")}


# ---------- база ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                name       TEXT DEFAULT '',
                gender     TEXT DEFAULT 'n',
                track      TEXT DEFAULT '',
                goal       TEXT DEFAULT '',
                vision     TEXT DEFAULT '',
                pack       TEXT DEFAULT '',
                started_at TEXT DEFAULT '',
                last_seen  TEXT DEFAULT '',
                runs       INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS entries (
                user_id INTEGER, day TEXT, key TEXT, value TEXT,
                PRIMARY KEY (user_id, day, key)
            );
            CREATE TABLE IF NOT EXISTS events (
                user_id INTEGER, day TEXT, kind TEXT, ts TEXT
            );
            CREATE TABLE IF NOT EXISTS archive (
                user_id INTEGER, goal TEXT, started_at TEXT,
                finished_at TEXT, days INTEGER, marks INTEGER
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER, day TEXT, kind TEXT, model TEXT,
                tokens_in INTEGER, tokens_out INTEGER, usd REAL
            );
            """
        )
        have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        for col, decl in (("gender", "TEXT DEFAULT 'n'"), ("pack", "TEXT DEFAULT ''"),
                          ("track", "TEXT DEFAULT ''"),
                          ("vision", "TEXT DEFAULT ''"), ("runs", "INTEGER DEFAULT 0")):
            if col not in have:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    log.info("db ready at %s", DB_PATH)


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def user_row(uid: int):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def ensure_user(uid: int, name: str = ""):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, name) VALUES (?,?)", (uid, name))
        conn.execute("UPDATE users SET last_seen=? WHERE user_id=?", (today(), uid))


def start_goal(uid: int, track: str, goal: str, vision: str, gender: str):
    with db() as conn:
        conn.execute(
            "UPDATE users SET track=?, goal=?, vision=?, gender=?, pack='', started_at=?, "
            "runs=runs+1 WHERE user_id=?",
            (track, goal[:200], vision[:200], gender, today(), uid),
        )


def save_pack(uid: int, pack: dict):
    with db() as conn:
        conn.execute(
            "UPDATE users SET pack=? WHERE user_id=?",
            (json.dumps(pack, ensure_ascii=False), uid),
        )


def get_pack(u) -> dict:
    if u and u["pack"]:
        try:
            return json.loads(u["pack"])
        except json.JSONDecodeError:
            pass
    return dict(C.FALLBACK_PACK)


def day_entries(uid: int, day: str) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM entries WHERE user_id=? AND day=?", (uid, day)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def marks_count(uid: int, day: str) -> int:
    return sum(1 for k, v in day_entries(uid, day).items() if k in PRACTICE_KEYS and v == "1")


def toggle_mark(uid: int, day: str, key: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM entries WHERE user_id=? AND day=? AND key=?", (uid, day, key)
        ).fetchone()
        if row and row["value"] == "1":
            conn.execute("DELETE FROM entries WHERE user_id=? AND day=? AND key=?", (uid, day, key))
            return False
        conn.execute(
            "INSERT OR REPLACE INTO entries (user_id, day, key, value) VALUES (?,?,?,'1')",
            (uid, day, key),
        )
        return True


def save_note(uid: int, day: str, key: str, text: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entries (user_id, day, key, value) VALUES (?,?,?,?)",
            (uid, day, f"note:{key}", text[:1000]),
        )


def add_event(uid: int, kind: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO events (user_id, day, kind, ts) VALUES (?,?,?,?)",
            (uid, today(), kind, datetime.now(TZ).isoformat(timespec="seconds")),
        )


def has_event(uid: int, kind: str, days_back: int = 1) -> bool:
    since = (datetime.now(TZ).date() - timedelta(days=days_back)).isoformat()
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE user_id=? AND kind=? AND day>=? LIMIT 1",
            (uid, kind, since),
        ).fetchone() is not None


def count_events(uid: int, *kinds) -> int:
    with db() as conn:
        q = ",".join("?" * len(kinds))
        return conn.execute(
            f"SELECT COUNT(*) c FROM events WHERE user_id=? AND kind IN ({q})", (uid, *kinds)
        ).fetchone()["c"]


def save_usage(uid: int, kind: str, usage: dict):
    """Каждое обращение к Claude — строка в базе: потом видно, сколько стоит человек."""
    if not usage:
        return
    usd = cost_usd(usage["model"], usage["tokens_in"], usage["tokens_out"])
    with db() as conn:
        conn.execute(
            "INSERT INTO usage (user_id, day, kind, model, tokens_in, tokens_out, usd) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, today(), kind, usage["model"], usage["tokens_in"], usage["tokens_out"], usd),
        )


def total_marks(uid: int) -> int:
    with db() as conn:
        rows = conn.execute("SELECT key FROM entries WHERE user_id=? AND value='1'", (uid,)).fetchall()
    return sum(1 for r in rows if r["key"] in PRACTICE_KEYS)


def history(uid: int, days: int = 14) -> list:
    d0 = datetime.now(TZ).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (d0 - timedelta(days=i)).isoformat()
        out.append({"day": d, "count": marks_count(uid, d)})
    return out


def streak(uid: int, goal_n: int) -> int:
    d0 = datetime.now(TZ).date()
    i = 0 if marks_count(uid, d0.isoformat()) >= goal_n else 1
    n = 0
    while i < 400:
        if marks_count(uid, (d0 - timedelta(days=i)).isoformat()) >= goal_n:
            n += 1
            i += 1
        else:
            break
    return n


# ---------- рубеж отчаяния ----------

def in_low(uid: int) -> bool:
    """Сам сказал «тяжело», нажимал SOS или два пустых дня после живой серии."""
    if has_event(uid, "hard", 1) or has_event(uid, "sos", 0):
        return True
    d0 = datetime.now(TZ).date()
    if not all(marks_count(uid, (d0 - timedelta(days=i)).isoformat()) == 0 for i in (1, 2)):
        return False
    return any(
        marks_count(uid, (d0 - timedelta(days=i)).isoformat()) >= DAY_GOAL for i in range(3, 12)
    )


def pick(items: list, uid: int, salt: str = "") -> str:
    """Один и тот же текст в течение дня, без повторов внутри круга."""
    if not items:
        return ""
    order = list(range(len(items)))
    random.Random(f"{uid}:{salt}").shuffle(order)
    day_no = (datetime.now(TZ).date() - datetime(2026, 1, 1, tzinfo=TZ).date()).days
    return items[order[day_no % len(items)]]


def plural_marks(n: int) -> str:
    """3 отметки / 5 отметок / 21 отметка"""
    tail, hundred = n % 10, n % 100
    if 11 <= hundred <= 14 or tail == 0 or tail >= 5:
        word = "отметок"
    elif tail == 1:
        word = "отметка"
    else:
        word = "отметки"
    return f"{n} {word}"


def phrase_of_day(uid: int, u, low: bool) -> str:
    pack = get_pack(u)
    if low:
        text = pick(pack["low"], uid, "low")
        return text.replace("{count}", plural_marks(max(total_marks(uid), 1)))
    return pick(pack["daily"], uid, "day")


# ---------- авторизация ----------

def verify_init_data(init_data: str):
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received):
        return None
    try:
        if datetime.now().timestamp() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        return None


def allowed(uid: int) -> bool:
    return not ALLOWED_IDS or uid in ALLOWED_IDS


async def auth(request):
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "bad_request"}, status=400)
    u = verify_init_data(body.get("initData", ""))
    if not u:
        return None, web.json_response({"error": "unauthorized"}, status=401)
    uid = int(u.get("id", 0))
    if not allowed(uid):
        return None, web.json_response({"error": "forbidden"}, status=403)
    ensure_user(uid, u.get("first_name", ""))
    return (uid, body), None


# ---------- API ----------

INDEX = Path(__file__).parent / "index.html"


async def page(request):
    return web.FileResponse(INDEX)


async def health(request):
    return web.Response(text="ok")


def state_payload(uid: int) -> dict:
    u = user_row(uid)
    if not u["goal"]:
        return {"setup": True, "name": u["name"], "again": u["runs"] > 0,
                "directions": DIRECTIONS}
    low = in_low(uid)
    goal_n = MIN_GOAL if low else DAY_GOAL
    e = day_entries(uid, today())
    pack = get_pack(u)
    practices = []
    for p in C.PRACTICES:
        item = dict(p)
        if p["key"] == "affirm" and pack.get("affirm"):
            item["hint"] = pack["affirm"]
        practices.append(item)
    return {
        "setup": False,
        "name": u["name"],
        "goal": u["goal"],
        "vision": u["vision"],
        "practices": practices,
        "partTitles": C.PART_TITLES,
        "marks": {k: True for k in PRACTICE_KEYS if e.get(k) == "1"},
        "notes": {k: e.get(f"note:{k}", "") for k in NOTE_KEYS},
        "phrase": phrase_of_day(uid, u, low),
        "low": low,
        "streak": streak(uid, goal_n),
        "dayGoal": goal_n,
        "totalMarks": total_marks(uid),
        "history": history(uid),
        "personal": bool(u["pack"]),
        "sos": {
            "breath": C.BREATH,
            "steps": C.SOS_STEPS,
            "close": pick(pack["sos_close"], uid, "sos"),
        },
        "date": today(),
    }


async def api_state(request):
    res, err = await auth(request)
    if err is not None:
        return err
    add_event(res[0], "open")  # когда человек заходит — пригодится, чтобы понять, что его зовёт
    return web.json_response(state_payload(res[0]))


async def make_pack_for(uid: int):
    """С ключом — персональная генерация. Без ключа — готовый пак по словам цели."""
    u = user_row(uid)
    if not u or not u["goal"]:
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        pack, usage = await build_pack(u["goal"], u["vision"], u["gender"])
        save_usage(uid, "pack", usage)
    else:
        pack = (pack_by_key(u["track"])                     # выбранное направление
                or match_pack(u["goal"], u["vision"])       # запасной подбор по словам
                or dict(C.FALLBACK_PACK))
    save_pack(uid, pack)
    log.info("pack ready for %s", uid)


async def api_setup(request):
    res, err = await auth(request)
    if err is not None:
        return err
    uid, body = res
    goal = (body.get("goal") or "").strip()
    if len(goal) < 3:
        return web.json_response({"error": "empty_goal"}, status=400)
    gender = body.get("gender") if body.get("gender") in ("f", "m", "n") else "n"
    track = body.get("track") or ""
    start_goal(uid, track, goal, (body.get("vision") or "").strip(), gender)
    if os.environ.get("ANTHROPIC_API_KEY"):
        asyncio.create_task(make_pack_for(uid))   # фразы догонят через несколько секунд
    else:
        await make_pack_for(uid)                  # готовый пак подставляется сразу
    return web.json_response(state_payload(uid))


async def api_toggle(request):
    res, err = await auth(request)
    if err is not None:
        return err
    uid, body = res
    if body.get("key") not in PRACTICE_KEYS:
        return web.json_response({"error": "unknown_key"}, status=400)
    toggle_mark(uid, today(), body["key"])
    return web.json_response(state_payload(uid))


async def api_note(request):
    res, err = await auth(request)
    if err is not None:
        return err
    uid, body = res
    if body.get("key") not in NOTE_KEYS:
        return web.json_response({"error": "unknown_key"}, status=400)
    save_note(uid, today(), body["key"], (body.get("text") or "").strip())
    return web.json_response({"ok": True})


async def api_event(request):
    res, err = await auth(request)
    if err is not None:
        return err
    uid, body = res
    if body.get("kind") not in {"hard", "sos"}:
        return web.json_response({"error": "unknown_kind"}, status=400)
    add_event(uid, body["kind"])
    return web.json_response(state_payload(uid))


async def api_finish(request):
    res, err = await auth(request)
    if err is not None:
        return err
    uid, _ = res
    u = user_row(uid)
    if not u["goal"]:
        return web.json_response({"error": "no_goal"}, status=400)
    days = 1
    if u["started_at"]:
        days = (datetime.now(TZ).date() - datetime.fromisoformat(u["started_at"]).date()).days + 1
    marks = total_marks(uid)
    hard = count_events(uid, "hard", "sos")
    with db() as conn:
        conn.execute(
            "INSERT INTO archive (user_id, goal, started_at, finished_at, days, marks) "
            "VALUES (?,?,?,?,?,?)",
            (uid, u["goal"], u["started_at"], today(), days, marks),
        )
        conn.execute("UPDATE users SET goal='', vision='', pack='' WHERE user_id=?", (uid,))
    try:
        await bot.send_message(uid, C.FINISH.format(goal=u["goal"], days=days, marks=marks, hard=hard))
    except Exception as exc:
        log.warning("finish msg: %s", exc)
    return web.json_response(state_payload(uid))


def make_app():
    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/health", health)
    for path, handler in (
        ("/api/state", api_state), ("/api/setup", api_setup), ("/api/toggle", api_toggle),
        ("/api/note", api_note), ("/api/event", api_event), ("/api/finish", api_finish),
    ):
        app.router.add_post(path, handler)
    return app


# ---------- бот ----------

def kb():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if not allowed(m.from_user.id):
        await m.answer(f"Доступ закрыт. Твой ID: <code>{m.from_user.id}</code>")
        return
    ensure_user(m.from_user.id, m.from_user.first_name or "")
    await m.answer(
        "Опора.\n\nОдна цель, несколько коротких действий в день и пара слов тогда, когда они "
        "нужны. Верить ни во что не надо — надо отмечать сделанное.\n\nОткрой и напиши цель.",
        reply_markup=kb(),
    )


@dp.message(Command("day"))
async def cmd_day(m: Message):
    if allowed(m.from_user.id):
        ensure_user(m.from_user.id, m.from_user.first_name or "")
        await m.answer("Сегодня:", reply_markup=kb())


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    uid = m.from_user.id
    if not allowed(uid):
        return
    ensure_user(uid, m.from_user.first_name or "")
    u = user_row(uid)
    if not u["goal"]:
        await m.answer("Цель ещё не выбрана.", reply_markup=kb())
        return
    goal_n = MIN_GOAL if in_low(uid) else DAY_GOAL
    bar = "".join("●" if d["count"] >= goal_n else ("◐" if d["count"] else "○") for d in history(uid))
    await m.answer(
        f"{u['goal']}\n\nСерия: <b>{streak(uid, goal_n)}</b> дн. · всего отметок: {total_marks(uid)}\n"
        f"<code>{bar}</code>",
        reply_markup=kb(),
    )


@dp.message(Command("lyudi"))
async def cmd_people(m: Message):
    """Сводка для владельца: сколько людей, насколько живые, сколько стоит ИИ."""
    if not OWNER_ID or m.from_user.id != OWNER_ID:
        return
    d0 = datetime.now(TZ).date()
    week, month = (d0 - timedelta(days=7)).isoformat(), d0.replace(day=1).isoformat()
    labels = {d["key"]: d["label"] for d in DIRECTIONS}
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        with_goal = conn.execute("SELECT user_id FROM users WHERE goal != ''").fetchall()
        active = lambda since: conn.execute(
            "SELECT COUNT(DISTINCT user_id) c FROM events WHERE kind='open' AND day>=?", (since,)
        ).fetchone()["c"]
        today_n, week_n = active(d0.isoformat()), active(week)
        new_week = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT user_id, MIN(day) d FROM events GROUP BY user_id) WHERE d>=?",
            (week,),
        ).fetchone()["c"]
        tracks = conn.execute(
            "SELECT track, COUNT(*) c FROM users WHERE goal != '' GROUP BY track ORDER BY c DESC"
        ).fetchall()
        finished = conn.execute("SELECT COUNT(*) c FROM archive").fetchone()["c"]
        spend = lambda since: conn.execute(
            "SELECT COALESCE(SUM(usd),0) s, COUNT(*) n FROM usage WHERE day>=?", (since,)
        ).fetchone()
        sp_month, sp_all = spend(month), spend("0")
    low_n = sum(1 for r in with_goal if in_low(r["user_id"]))
    by_track = ", ".join(f"{labels.get(t['track'], t['track'] or '—')} {t['c']}" for t in tracks) or "—"
    await m.answer(
        f"Людей всего: <b>{total}</b>, с целью: {len(with_goal)}, целей закрыто: {finished}\n"
        f"Заходили сегодня: {today_n}, за неделю: {week_n}, новых за неделю: {new_week}\n"
        f"Сейчас на спаде: {low_n}\n"
        f"По направлениям: {by_track}\n\n"
        f"ИИ за месяц: ${sp_month['s']:.2f} ({sp_month['n']} запросов), "
        f"всего: ${sp_all['s']:.2f} ({sp_all['n']})"
    )


@dp.message(Command("tikho"))
async def cmd_quiet(m: Message):
    if not allowed(m.from_user.id):
        return
    add_event(m.from_user.id, "mute")
    await m.answer("Молчу три дня. Приложение на месте, заходи когда захочешь.")


@dp.message(Command("frazy"))
async def cmd_refresh(m: Message):
    """Пересобрать персональный набор фраз под текущую цель."""
    uid = m.from_user.id
    if not allowed(uid):
        return
    u = user_row(uid)
    if not u or not u["goal"]:
        await m.answer("Сначала нужна цель.", reply_markup=kb())
        return
    await m.answer("Собираю новые фразы под твою цель, минуту.")
    await make_pack_for(uid)
    await m.answer(phrase_of_day(uid, user_row(uid), False), reply_markup=kb())


# ---------- уведомления ----------

async def notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text, reply_markup=kb())
    except Exception as exc:
        log.warning("push to %s failed: %s", uid, exc)
    await asyncio.sleep(0.05)


SLOTS = {"morning": ((8, 30), (9, 0)), "evening": ((21, 0), (21, 30))}


def current_slot(now: datetime):
    for slot, (start, end) in SLOTS.items():
        if start <= (now.hour, now.minute) < end:
            return slot
    return None


def push_sent(uid: int, slot: str, day: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE user_id=? AND kind=? AND day=? LIMIT 1",
            (uid, f"push:{slot}", day),
        ).fetchone() is not None


async def reminders():
    """Не больше двух в день. Отправленное помнится в базе, а не в памяти процесса —
    перезапуск на Railway ничего не сбивает. Вечернее приходит, пока день не закрыт."""
    while True:
        now = datetime.now(TZ)
        slot = current_slot(now)
        if slot:
            day = now.date().isoformat()
            with db() as conn:
                users = conn.execute("SELECT * FROM users WHERE goal != ''").fetchall()
            for u in users:
                uid = u["user_id"]
                if not allowed(uid) or push_sent(uid, slot, day) or has_event(uid, "mute", 3):
                    continue
                low = in_low(uid)
                pack = get_pack(u)
                if slot == "morning":
                    text = phrase_of_day(uid, u, True) if low else pick(pack["morning"], uid, "morn")
                else:
                    if marks_count(uid, day) >= (MIN_GOAL if low else DAY_GOAL):
                        continue  # день уже закрыт — не дёргать
                    text = pick(C.PUSH_QUIET if low else pack["evening"], uid, "eve")
                add_event(uid, f"push:{slot}")  # до отправки: при сбое не долбить каждую минуту
                await notify(uid, text)
                log.info("push %s -> %s", slot, uid)
        await asyncio.sleep(60)


async def main():
    init_db()
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Опора", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception as exc:
            log.warning("menu button: %s", exc)
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web on :%s", PORT)
    asyncio.create_task(reminders())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
