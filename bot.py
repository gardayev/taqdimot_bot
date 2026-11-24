import os, io, json, asyncio
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from pptx import Presentation
from google import genai
from google.genai import types as gt

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_ID"])
DB_URL = os.environ["DATABASE_URL"]
URL = os.environ.get("RENDER_EXTERNAL_URL", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

ai = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
bot = Bot(TOKEN)
dp = Dispatcher()
pool = None
sem = asyncio.Semaphore(2)


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DB_URL, statement_cache_size=0)
    await pool.execute("""
    CREATE TABLE IF NOT EXISTS users(
      id BIGINT PRIMARY KEY,
      balance INT DEFAULT 1,
      invited_by BIGINT,
      counted BOOLEAN DEFAULT FALSE,
      ref_progress INT DEFAULT 0)""")


def make_pptx(topic, data):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = topic
    s.placeholders[1].text = "Taqdimot"
    for sl in data["slides"]:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = sl["title"]
        tf = s.placeholders[1].text_frame
        tf.clear()
        for i, p in enumerate(sl["points"]):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.text = p
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def ask_ai(topic):
    prompt = (
        f"'{topic}' mavzusida o'zbek tilida (lotin) taqdimot tuz. "
        "8 ta slayd, har birida qisqa sarlavha va 3-4 ta punkt. "
        'Faqat JSON qaytar: {"slides":[{"title":"...","points":["..."]}]}'
    )
    r = ai.models.generate_content(
        model=MODEL, contents=prompt,
        config=gt.GenerateContentConfig(response_mime_type="application/json"))
    return json.loads(r.text)


@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    parts = (m.text or "").split()
    inv = None
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inv = int(parts[1][4:])
        except ValueError:
            pass
        if inv == uid:
            inv = None
    await pool.execute(
        "INSERT INTO users(id, invited_by) VALUES($1,$2) ON CONFLICT DO NOTHING",
        uid, inv)
    me = await bot.get_me()
    await m.answer(
        "Salom! Mavzu yozing, men taqdimot (.pptx) tayyorlayman.\n"
        "Sizda 1 ta bepul slayd bor. Keyingisi 500 so'm yoki 5 do'st taklif qiling.\n\n"
        f"Taklif havolangiz:\nhttps://t.me/{me.username}?start=ref_{uid}\n\n"
        "/balans - balansni ko'rish")


@dp.message(Command("balans"))
async def balans(m: Message):
    b = await pool.fetchval("SELECT balance FROM users WHERE id=$1", m.from_user.id)
    await m.answer(f"Balansingiz: {b or 0} ta slayd")


@dp.message(Command("add"))
async def add(m: Message):
    if m.from_user.id != ADMIN:
        return
    try:
        _, uid, n = m.text.split()
        await pool.execute("UPDATE users SET balance=balance+$2 WHERE id=$1", int(uid), int(n))
        await m.answer("Qo'shildi")
        await bot.send_message(int(uid), f"Balansingizga {n} ta slayd qo'shildi")
    except Exception:
        await m.answer("Format: /add USER_ID SONI")


@dp.message()
async def topic(m: Message):
    if not m.text or m.text.startswith("/"):
        return
    uid = m.from_user.id
    await pool.execute("INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    left = await pool.fetchval(
        "UPDATE users SET balance=balance-1 WHERE id=$1 AND balance>0 RETURNING balance", uid)
    if left is None:
        await m.answer("Balans tugadi. 500 so'm to'lang yoki 5 do'st taklif qiling (/start).")
        return
    wait = await m.answer("Tayyorlanmoqda, 1 daqiqa kuting...")
    try:
        async with sem:
            data = await asyncio.to_thread(ask_ai, m.text)
            file = make_pptx(m.text, data)
        await m.answer_document(BufferedInputFile(file, filename="taqdimot.pptx"))
        await wait.delete()
    except Exception:
        await pool.execute("UPDATE users SET balance=balance+1 WHERE id=$1", uid)
        await m.answer("Xatolik bo'ldi, balans qaytarildi. Qayta urinib ko'ring.")
        return
    row = await pool.fetchrow("SELECT invited_by, counted FROM users WHERE id=$1", uid)
    if row["invited_by"] and not row["counted"]:
        await pool.execute("UPDATE users SET counted=TRUE WHERE id=$1", uid)
        prog = await pool.fetchval(
            "UPDATE users SET ref_progress=ref_progress+1 WHERE id=$1 RETURNING ref_progress",
            row["invited_by"])
        if prog and prog >= 5:
            await pool.execute(
                "UPDATE users SET ref_progress=ref_progress-5, balance=balance+1 WHERE id=$1",
                row["invited_by"])
            try:
                await bot.send_message(row["invited_by"], "5 do'st taklif qildingiz: +1 slayd!")
            except Exception:
                pass


async def on_startup(app):
    await init_db()
    if URL:
        await bot.set_webhook(f"{URL}/webhook")


async def health(request):
    return web.Response(text="ok")


app = web.Application()
app.router.add_get("/", health)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
setup_application(app, dp, bot=bot)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
