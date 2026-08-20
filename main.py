import os
import json
import asyncio
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

from ready_texts import TEXTS
from questions import QUESTIONS

# ─── Sozlamalar ───────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

# ─── Ruxsat tizimi (faqat admin tasdiqlagan foydalanuvchilar kirishi mumkin) ──
DATA_FILE = "bot_data.json"

def _load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return (
                set(d.get("approved", [])),
                {int(k): v for k, v in d.get("pending", {}).items()},
                {int(k): v for k, v in d.get("user_info", {}).items()},
                d.get("drug_files", {}),
            )
        except Exception:
            pass
    return set(), {}, {}, {}

APPROVED, PENDING, USER_INFO, DRUG_FILES = _load_data()

def _save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "approved": list(APPROVED),
                "pending": PENDING,
                "user_info": USER_INFO,
                "drug_files": DRUG_FILES,
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Data saqlashda xato: {e}")

def _drug_files(drug_id, category):
    return DRUG_FILES.setdefault(drug_id, {}).setdefault(category, [])

def has_access(uid: int) -> bool:
    return uid == ADMIN_ID or uid in APPROVED

def _user_tag(user) -> dict:
    return {
        "username": user.username or "",
        "full_name": user.full_name or "",
    }

# ─── State ────────────────────────────────────────────────────────────────────
# user_state[uid] = {
#     "lang": "uz"/"ru",
#     "screen": joriy ekran nomi,
#     "drug": joriy tanlangan препарат id (yoki None),
#     "kb_map": {tugma matni: (action, payload)}  -> pastdagi klaviatura tugmalarini aniqlash uchun,
#     "quiz": {...}  -> test/vaziyat holati
# }
user_state    = {}
feedback_mode = set()
admin_upload_state = {}   # admin_id -> {"drug_id": str, "cat": "evidence"/"media"}

EXCLUDED_KEYS = {"welcome", "select_section", "btn_back", "btn_main",
                 "comp_direct", "comp_indirect", "btn_quiz", "sections"}

# Далиллар базаси учун рухсат этилган файл кенгайтмалари
EVIDENCE_EXTS = (".docx", ".xlsx", ".pdf")

def get_lang(uid): return user_state.get(uid, {}).get("lang", "uz")

# ══════════════════════════════════════════════════════════════════════════════
#  ПАСТКИ (REPLY) КЛАВИАТУРАЛАР
#  Ҳар бир "screen" учун tugma qatorlari (rows) va shu tugmalarning nimani
#  bajarishini bildiruvchi lug'at (kb_map) birgalikda quriladi. kb_map
#  user_state ichida saqlanadi va foydalanuvchi matn yuborganda shu lug'atdan
#  qidiriladi — shu tarzda callback_data umuman kerak bo'lmaydi.
# ══════════════════════════════════════════════════════════════════════════════
def kb(rows, placeholder=None):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )

def build_lang_rows():
    rows = [["O'zbekcha", "Русский"]]
    kbmap = {"O'zbekcha": ("lang", "uz"), "Русский": ("lang", "ru")}
    return rows, kbmap

def build_main_menu(lang, uid):
    rows, kbmap = [], {}
    for d_id, d in TEXTS[lang].items():
        if d_id not in EXCLUDED_KEYS:
            rows.append([d["name"]])
            kbmap[d["name"]] = ("drug", d_id)

    quiz_label = "📝 Савол-жавоб (Тест & Вазият)" if lang == "uz" else "📝 Вопросы (Тест & Случаи)"
    rows.append([quiz_label]); kbmap[quiz_label] = ("quiz_menu", None)

    fb_label = "💬 Фидбек / Муаммо билдириш" if lang == "uz" else "💬 Обратная связь"
    rows.append([fb_label]); kbmap[fb_label] = ("feedback", None)

    if uid == ADMIN_ID:
        admin_label = "🛠 Админ панели (Файл юклаш)" if lang == "uz" else "🛠 Панель админа (Загрузка файлов)"
        rows.append([admin_label]); kbmap[admin_label] = ("admin_panel", None)
    return rows, kbmap

def build_sections(lang, drug_id):
    rows, kbmap = [], {}
    for s_key, s_name in TEXTS[lang]["sections"].items():
        rows.append([s_name])
        kbmap[s_name] = ("section", (drug_id, s_key))

    ev_label = "📚 Далиллар базаси" if lang == "uz" else "📚 Доказательная база"
    rows.append([ev_label]); kbmap[ev_label] = ("evidence", drug_id)

    media_label = "🎥 Медиаконтент"
    rows.append([media_label]); kbmap[media_label] = ("media", drug_id)

    back_label    = "🏠 Бош менюга қайтиш" if lang == "uz" else "🏠 Вернуться в главное меню"
    restart_label = "🔄 Ботни қайта ишга тушириш" if lang == "uz" else "🔄 Перезапустить бота"
    rows.append([back_label, restart_label])
    kbmap[back_label]    = ("back_main", None)
    kbmap[restart_label] = ("restart", None)
    return rows, kbmap

def build_quiz_menu(lang):
    rows, kbmap = [], {}
    tests = QUESTIONS[lang]["tests"]
    cases = QUESTIONS[lang]["cases"]
    if tests:
        label = (f"🧪 Тест ({len(tests)} та савол)" if lang == "uz"
                  else f"🧪 Тест ({len(tests)} вопросов)")
        rows.append([label]); kbmap[label] = ("quiz_test", 0)
    if cases:
        label = (f"🏥 Клиник вазиятлар ({len(cases)} та)" if lang == "uz"
                  else f"🏥 Клинические случаи ({len(cases)})")
        rows.append([label]); kbmap[label] = ("case", 0)
    back = "⬅️ Орқага" if lang == "uz" else "⬅️ Назад"
    rows.append([back]); kbmap[back] = ("back_main", None)
    return rows, kbmap

def build_quiz_test(lang, idx):
    q = QUESTIONS[lang]["tests"][idx]
    rows, kbmap = [], {}
    for i, opt in enumerate(q["options"]):
        rows.append([opt])
        kbmap[opt] = ("answer", (idx, i))
    back = "⬅️ Менюга" if lang == "uz" else "⬅️ В меню"
    rows.append([back]); kbmap[back] = ("quiz_menu", None)
    return rows, kbmap

def build_after_answer(lang, idx, total):
    rows, kbmap = [], {}
    nxt = idx + 1
    if nxt < total:
        label = "➡️ Кейинги савол" if lang == "uz" else "➡️ Следующий вопрос"
        rows.append([label]); kbmap[label] = ("quiz_test", nxt)
    home = "🏠 Бош меню" if lang == "uz" else "🏠 Главное меню"
    rows.append([home]); kbmap[home] = ("back_main", None)
    return rows, kbmap

def build_case(lang, idx):
    rows, kbmap = [], {}
    skip = "⏭ Ўтказиб юбориш" if lang == "uz" else "⏭ Пропустить"
    rows.append([skip]); kbmap[skip] = ("case_skip", idx)
    back = "⬅️ Орқага" if lang == "uz" else "⬅️ Назад"
    rows.append([back]); kbmap[back] = ("quiz_menu", None)
    return rows, kbmap

def build_after_case(lang, idx, total):
    rows, kbmap = [], {}
    nxt = idx + 1
    if nxt < total:
        label = "➡️ Кейинги вазият" if lang == "uz" else "➡️ Следующий случай"
        rows.append([label]); kbmap[label] = ("case", nxt)
    home = "🏠 Бош меню" if lang == "uz" else "🏠 Главное меню"
    rows.append([home]); kbmap[home] = ("back_main", None)
    return rows, kbmap

def build_home_only(lang):
    home = "🏠 Бош меню" if lang == "uz" else "🏠 Главное меню"
    return [[home]], {home: ("back_main", None)}

def build_feedback_kb(lang):
    back = "⬅️ Орқага" if lang == "uz" else "⬅️ Назад"
    return [[back]], {back: ("back_main", None)}

def build_admin_drug_list(lang):
    rows, kbmap = [], {}
    for d_id, d in TEXTS[lang].items():
        if d_id not in EXCLUDED_KEYS:
            rows.append([d["name"]])
            kbmap[d["name"]] = ("admin_pick_drug", d_id)
    back = "⬅️ Орқага" if lang == "uz" else "⬅️ Назад"
    rows.append([back]); kbmap[back] = ("back_main", None)
    return rows, kbmap

def build_admin_cat(lang, drug_id):
    ev = "📚 Далиллар базаси"
    md = "🎥 Медиаконтент"
    back = "⬅️ Орқага" if lang == "uz" else "⬅️ Назад"
    rows = [[ev], [md], [back]]
    kbmap = {
        ev:   ("admin_cat", (drug_id, "evidence")),
        md:   ("admin_cat", (drug_id, "media")),
        back: ("admin_panel", None),
    }
    return rows, kbmap

def build_admin_upload_kb():
    label = "✅ Тугатиш"
    return [[label]], {label: ("aupload_done", None)}

# ── State yozib, klaviaturani yuboruvchi yordamchilar ─────────────────────────
def _set_screen(uid, **fields):
    st = user_state.setdefault(uid, {})
    st.update(fields)
    return st

async def show_main(uid, lang, text=None):
    rows, kbmap = build_main_menu(lang, uid)
    _set_screen(uid, lang=lang, screen="main", drug=None, kb_map=kbmap)
    await bot.send_message(uid, text or TEXTS[lang]["welcome"], reply_markup=kb(rows))

async def send_lang_select(uid):
    rows, kbmap = build_lang_rows()
    user_state[uid] = {"screen": "lang_select", "kb_map": kbmap}
    await bot.send_message(
        uid, "🇺🇿 Тилни танланг / 🇷🇺 Выберите язык", reply_markup=kb(rows)
    )

# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id

    if has_access(uid):
        await send_lang_select(uid)
        return

    if uid in PENDING:
        await message.answer(
            "⏳ Сизнинг сўровингиз кўриб чиқилмоқда. Илтимос, админ тасдиғини кутинг."
        )
        return

    PENDING[uid] = _user_tag(message.from_user)
    _save_data()

    if ADMIN_ID:
        uname = f"@{message.from_user.username}" if message.from_user.username else "—"
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Янги уланиш сўрови!</b>\n"
            f"👤 {message.from_user.full_name} ({uname})\n"
            f"🆔 <code>{uid}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Рухсат бериш", callback_data=f"approve_{uid}"),
                InlineKeyboardButton(text="❌ Рад этиш",     callback_data=f"deny_{uid}")
            ]])
        )

    await message.answer(
        "📨 Сизнинг ботга уланиш сўровингиз админга юборилди.\n"
        "Тасдиқлангач сизга хабар келади. Илтимос, кутинг."
    )

# ─── Admin: so'rovni tasdiqlash / rad etish ──────────────────────────────────
# Bu ikkita tugma (Рухсат/Рад) alohida, bir martalik admin amali bo'lgani va
# bir vaqtning o'zida bir nechta so'rov kelishi mumkinligi uchun pastki
# klaviatura o'rniga aynan shu bildirishnoma xabariga bog'langan inline
# tugma sifatida qoldirildi.
@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    uid = int(call.data.split("_", 1)[1])
    info = PENDING.pop(uid, _user_tag(call.from_user))
    APPROVED.add(uid)
    USER_INFO[uid] = info
    _save_data()

    await call.message.edit_text(call.message.text + "\n\n✅ Рухсат берилди.")
    try:
        await send_lang_select(uid)
    except Exception:
        pass
    await call.answer("Рухсат берилди.")

@dp.callback_query(F.data.startswith("deny_"))
async def cb_deny(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    uid = int(call.data.split("_", 1)[1])
    PENDING.pop(uid, None)
    _save_data()

    await call.message.edit_text(call.message.text + "\n\n❌ Рад этилди.")
    try:
        await bot.send_message(uid, "❌ Сизнинг ботга уланиш сўровингиз рад этилди.")
    except Exception:
        pass
    await call.answer("Рад этилди.")

# ══════════════════════════════════════════════════════════════════════════════
#  ФАЙЛ ЮБОРИШ ЁРДАМЧИСИ
# ══════════════════════════════════════════════════════════════════════════════
async def _send_stored_file(chat_id, f):
    ftype = f.get("type")
    fid   = f.get("file_id")
    name  = f.get("file_name") or None
    if ftype == "document":
        await bot.send_document(chat_id, fid, caption=name)
    elif ftype == "photo":
        await bot.send_photo(chat_id, fid)
    elif ftype == "video":
        await bot.send_video(chat_id, fid)
    elif ftype == "animation":
        await bot.send_animation(chat_id, fid)
    elif ftype == "audio":
        await bot.send_audio(chat_id, fid)

# ══════════════════════════════════════════════════════════════════════════════
#  АСОСИЙ НАВИГАЦИЯ — пастки клавиатура тугмаси босилганда бажариладиган амал
# ══════════════════════════════════════════════════════════════════════════════
async def do_action(uid: int, lang: str, kind: str, payload):
    if kind == "lang":
        new_lang = payload
        user_state[uid] = {"lang": new_lang}
        await show_main(uid, new_lang)
        return

    if kind == "back_main":
        feedback_mode.discard(uid)
        admin_upload_state.pop(uid, None)
        if uid in user_state:
            user_state[uid].pop("quiz", None)
        await show_main(uid, lang)
        return

    if kind == "restart":
        feedback_mode.discard(uid)
        admin_upload_state.pop(uid, None)
        user_state.pop(uid, None)
        await send_lang_select(uid)
        return

    if kind == "drug":
        drug_id = payload
        if drug_id not in TEXTS[lang]:
            return
        rows, kbmap = build_sections(lang, drug_id)
        _set_screen(uid, screen="sections", drug=drug_id, kb_map=kbmap)
        text = TEXTS[lang]["select_section"].format(TEXTS[lang][drug_id]["name"])
        await bot.send_message(uid, text, reply_markup=kb(rows))
        return

    if kind == "section":
        drug_id, s_key = payload
        if drug_id not in TEXTS[lang]:
            return
        rows, kbmap = build_sections(lang, drug_id)
        _set_screen(uid, screen="sections", drug=drug_id, kb_map=kbmap)
        markup = kb(rows)

        if s_key == "mkt_comp":
            cd = TEXTS[lang][drug_id].get("comp_dir", "")
            ci = TEXTS[lang][drug_id].get("comp_indir", "")
            msgs = [m for m in (cd, ci) if m]
            for i, m in enumerate(msgs):
                last = (i == len(msgs) - 1)
                await bot.send_message(uid, m, reply_markup=markup if last else None)
            return

        content = TEXTS[lang][drug_id].get(s_key)
        if content:
            await bot.send_message(uid, content, reply_markup=markup)
        return

    if kind in ("evidence", "media"):
        drug_id = payload
        if drug_id not in TEXTS[lang]:
            return
        cat = "evidence" if kind == "evidence" else "media"
        rows, kbmap = build_sections(lang, drug_id)
        _set_screen(uid, screen="sections", drug=drug_id, kb_map=kbmap)
        markup = kb(rows)

        files = _drug_files(drug_id, cat)
        if not files:
            if cat == "evidence":
                msg = ("📚 Ушбу препарат бўйича ҳали далиллар базаси файллари қўшилмаган."
                       if lang == "uz" else
                       "📚 Файлы доказательной базы по этому препарату ещё не добавлены.")
            else:
                msg = ("🎥 Ушбу препарат бўйича ҳали медиаконтент қўшилмаган."
                       if lang == "uz" else
                       "🎥 Медиаконтент по этому препарату ещё не добавлен.")
            await bot.send_message(uid, msg, reply_markup=markup)
            return

        title = "📚 <b>Далиллар базаси</b>" if cat == "evidence" else "🎥 <b>Медиаконтент</b>"
        await bot.send_message(uid, title)
        for f in files:
            await _send_stored_file(uid, f)
        await bot.send_message(uid, TEXTS[lang][drug_id]["name"], reply_markup=markup)
        return

    if kind == "quiz_menu":
        has_tests = len(QUESTIONS[lang]["tests"]) > 0
        has_cases = len(QUESTIONS[lang]["cases"]) > 0
        if not has_tests and not has_cases:
            msg = "📝 Саволлар ҳали қўшилмаган!" if lang == "uz" else "📝 Вопросы ещё не добавлены!"
            await bot.send_message(uid, msg)
            return
        rows, kbmap = build_quiz_menu(lang)
        _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
        title = ("📝 <b>Савол-жавоб бўлими</b>\n\nНимани синаб кўрмоқчисиз?" if lang == "uz"
                  else "📝 <b>Раздел вопросов</b>\n\nЧто хотите проверить?")
        await bot.send_message(uid, title, reply_markup=kb(rows))
        return

    if kind == "quiz_test":
        idx   = payload
        tests = QUESTIONS[lang]["tests"]
        if idx >= len(tests):
            rows, kbmap = build_home_only(lang)
            _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
            fin = "🎉 Тест тугади!" if lang == "uz" else "🎉 Тест завершён!"
            await bot.send_message(uid, fin, reply_markup=kb(rows))
            return
        q   = tests[idx]
        rows, kbmap = build_quiz_test(lang, idx)
        _set_screen(uid, screen="quiz_test", kb_map=kbmap)
        num       = f"{idx+1}/{len(tests)}"
        drug_name = TEXTS[lang].get(q["drug"], {}).get("name", q["drug"])
        txt = f"🧪 <b>Тест {num}</b> — <i>{drug_name}</i>\n\n❓ {q['question']}"
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "answer":
        idx, ans = payload
        tests   = QUESTIONS[lang]["tests"]
        q       = tests[idx]
        correct = q["correct"]

        if ans == correct:
            result = "✅ <b>Тўғри!</b>" if lang == "uz" else "✅ <b>Правильно!</b>"
        else:
            result = (
                f"❌ <b>Нотўғри.</b>\nСиз: {q['options'][ans]}\nТўғриси: {q['options'][correct]}"
                if lang == "uz" else
                f"❌ <b>Неверно.</b>\nВы: {q['options'][ans]}\nПравильно: {q['options'][correct]}"
            )
        explanation = f"\n\n💡 {q['explanation']}" if q.get("explanation") else ""
        drug_name   = TEXTS[lang].get(q["drug"], {}).get("name", q["drug"])
        txt = (f"🧪 <b>Тест {idx+1}/{len(tests)}</b> — <i>{drug_name}</i>\n\n"
               f"❓ {q['question']}\n\n{result}{explanation}")

        rows, kbmap = build_after_answer(lang, idx, len(tests))
        _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "case":
        idx   = payload
        cases = QUESTIONS[lang]["cases"]
        if idx >= len(cases):
            rows, kbmap = build_home_only(lang)
            _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
            fin = ("🎉 Барча клиник вазиятлар тугади!" if lang == "uz"
                    else "🎉 Все клинические случаи завершены!")
            await bot.send_message(uid, fin, reply_markup=kb(rows))
            return
        c         = cases[idx]
        num       = f"{idx+1}/{len(cases)}"
        drug_name = TEXTS[lang].get(c["drug"], {}).get("name", c["drug"])
        txt = (f"🏥 <b>{c['title']} ({num})</b> — <i>{drug_name}</i>\n\n"
               f"📋 {c['case']}\n\n"
               f"<i>{'Жавобингизни матн орқали ёзинг 👇' if lang == 'uz' else 'Напишите ваш ответ текстом 👇'}</i>")

        user_state.setdefault(uid, {})["quiz"] = {"mode": "case_waiting", "idx": idx}
        rows, kbmap = build_case(lang, idx)
        _set_screen(uid, screen="case", kb_map=kbmap)
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "case_skip":
        idx   = payload
        cases = QUESTIONS[lang]["cases"]
        case  = cases[idx]
        if uid in user_state:
            user_state[uid]["quiz"] = {}
        txt = (
            f"⏭ <b>Ўтказиб юборилди.</b>\n\n✅ <b>Тўғри жавоб:</b>\n{case['answer']}"
            if lang == "uz" else
            f"⏭ <b>Пропущено.</b>\n\n✅ <b>Правильный ответ:</b>\n{case['answer']}"
        )
        rows, kbmap = build_after_case(lang, idx, len(cases))
        _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "feedback":
        feedback_mode.add(uid)
        rows, kbmap = build_feedback_kb(lang)
        _set_screen(uid, screen="feedback", kb_map=kbmap)
        txt = (
            "💬 <b>Фидбек / Муаммо билдириш</b>\n\nСавол ёки муаммонгизни ёзинг. "
            "Админ имкон қадар тезроқ жавоб беради.\n\n<i>Бекор қилиш учун пастдаги «⬅️ Орқага» тугмасини босинг</i>"
            if lang == "uz" else
            "💬 <b>Обратная связь</b>\n\nНапишите ваш вопрос или проблему. "
            "Администратор ответит как можно скорее.\n\n<i>Для отмены нажмите «⬅️ Назад» внизу</i>"
        )
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "admin_panel":
        if uid != ADMIN_ID:
            return
        rows, kbmap = build_admin_drug_list(lang)
        _set_screen(uid, screen="admin_drug_list", kb_map=kbmap)
        txt = ("🛠 <b>Админ панели</b>\n\nФайл юклаш учун препаратни танланг:"
               if lang == "uz" else
               "🛠 <b>Панель админа</b>\n\nВыберите препарат для загрузки файла:")
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "admin_pick_drug":
        if uid != ADMIN_ID:
            return
        drug_id = payload
        if drug_id not in TEXTS[lang]:
            return
        rows, kbmap = build_admin_cat(lang, drug_id)
        _set_screen(uid, screen="admin_cat", drug=drug_id, kb_map=kbmap)
        txt = (f"📋 <b>{TEXTS[lang][drug_id]['name']}</b>\n\nҚайси бўлимга файл юкламоқчисиз?"
               if lang == "uz" else
               f"📋 <b>{TEXTS[lang][drug_id]['name']}</b>\n\nКуда загрузить файл?")
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "admin_cat":
        if uid != ADMIN_ID:
            return
        drug_id, cat = payload
        if drug_id not in TEXTS[lang]:
            return
        admin_upload_state[uid] = {"drug_id": drug_id, "cat": cat}
        rows, kbmap = build_admin_upload_kb()
        _set_screen(uid, screen="admin_upload", kb_map=kbmap)
        cat_name = "📚 Далиллар базаси" if cat == "evidence" else "🎥 Медиаконтент"
        if cat == "evidence":
            hint = ("Файлларни (DOCX, XLSX, PDF) юборинг. Ҳар бир файл автоматик сақланади.\n"
                    "Тайёр бўлгач, «✅ Тугатиш» тугмасини босинг.")
        else:
            hint = ("Медиафайлларни (расм, видео, ҳужжат) юборинг. Ҳар бир файл автоматик сақланади.\n"
                    "Тайёр бўлгач, «✅ Тугатиш» тугмасини босинг.")
        txt = f"📤 <b>{TEXTS[lang][drug_id]['name']}</b> — {cat_name}\n\n{hint}"
        await bot.send_message(uid, txt, reply_markup=kb(rows))
        return

    if kind == "aupload_done":
        if uid != ADMIN_ID:
            return
        admin_upload_state.pop(uid, None)
        txt = "✅ Файллар сақланди." if lang == "uz" else "✅ Файлы сохранены."
        await show_main(uid, lang, text=txt)
        return

# ══════════════════════════════════════════════════════════════════════════════
#  /cancel
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    uid = message.from_user.id
    if not has_access(uid):
        return
    lang = get_lang(uid)
    feedback_mode.discard(uid)
    admin_upload_state.pop(uid, None)
    if uid in user_state:
        user_state[uid].pop("quiz", None)
    txt = "❌ Бекор қилинди." if lang == "uz" else "❌ Отменено."
    await show_main(uid, lang, text=txt)

# ── Admin reply (foydalanuvchiga javob) — handle_text'dan OLDIN ro'yxatdan
#     o'tkazilishi shart, aks holda admin javobi oddiy matn sifatida tutilib qoladi.
#
#     MUHIM: bu handler faqat admin ANIQ o'sha "🔔 Янги уланиш сўрови!" /
#     "📩 Янги фидбек!" bildirishnomasiga reply qilganda ishga tushadi.
#     Aks holda (masalan, telefonda tasodifan swipe-to-reply ishlab ketib,
#     pastki klaviatura tugmasi yoki oddiy xabar "reply" sifatida yuborilib
#     qolsa) — bu handler unga aralashmaydi, xabar oddiy matn sifatida
#     handle_text'ga o'tadi va navigatsiya normal davom etadi.
def _is_reply_to_notification(message: Message) -> bool:
    if message.from_user.id != ADMIN_ID or not message.reply_to_message:
        return False
    orig = message.reply_to_message.text or message.reply_to_message.caption or ""
    return bool(re.search(r"🆔 (\d+)", orig))

@dp.message(_is_reply_to_notification)
async def admin_reply(message: Message):
    orig = message.reply_to_message.text or message.reply_to_message.caption or ""
    m = re.search(r"🆔 (\d+)", orig)
    target_uid  = int(m.group(1))
    target_lang = get_lang(target_uid)
    prefix = "👨‍💼 <b>Админ жавоби:</b>\n\n" if target_lang == "uz" else "👨‍💼 <b>Ответ администратора:</b>\n\n"
    try:
        await bot.send_message(target_uid, prefix + (message.text or message.caption or ""))
        await message.answer("✅ Жавоб юборилди!")
    except Exception as e:
        await message.answer(f"❌ Хато: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  ПАСТКИ КЛАВИАТУРА ТУГМАЛАРИ ВА ЭРКИН МАТНЛАРНИ ҚАБУЛ ҚИЛУВЧИ УМУМИЙ ХЕНДЛЕР
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    uid = message.from_user.id
    if not has_access(uid):
        return

    text = message.text
    st   = user_state.setdefault(uid, {})
    lang = st.get("lang", "uz")

    # 1) Аввало — бу пастки клавиатурадаги танилган тугмами?
    kb_map = st.get("kb_map", {})
    if text in kb_map:
        kind, payload = kb_map[text]
        await do_action(uid, lang, kind, payload)
        return

    # 2) Admin fayl yuklash rejimida bo'lsa (fayl kutilmoqda)
    if uid == ADMIN_ID and uid in admin_upload_state:
        await message.answer(
            "📎 Илтимос, файл (расм/видео/ҳужжат) юборинг ёки \"✅ Тугатиш\" тугмасини босинг."
        )
        return

    # 3) Feedback rejimida erkin matn — admin'ga yuboriladi
    if uid in feedback_mode:
        feedback_mode.discard(uid)
        if ADMIN_ID:
            uname = f"@{message.from_user.username}" if message.from_user.username else f"id:{uid}"
            await bot.send_message(
                ADMIN_ID,
                f"📩 <b>Янги фидбек!</b>\n"
                f"👤 {message.from_user.full_name} ({uname})\n"
                f"🆔 <code>{uid}</code>\n\n"
                f"💬 {message.text}"
            )
        conf = ("✅ Хабарингиз юборилди! Админ тезда жавоб беради." if lang == "uz"
                else "✅ Ваше сообщение отправлено! Администратор скоро ответит.")
        await show_main(uid, lang, text=conf)
        return

    # 4) Quiz — ochiq javob (klinik vaziyat)
    quiz_state = st.get("quiz", {})
    if quiz_state.get("mode") == "case_waiting":
        idx   = quiz_state["idx"]
        cases = QUESTIONS[lang]["cases"]
        case  = cases[idx]
        answer_text = (
            f"📋 <b>Сизнинг жавобингиз:</b>\n{message.text}\n\n"
            f"✅ <b>Тўғри жавоб:</b>\n{case['answer']}"
            if lang == "uz" else
            f"📋 <b>Ваш ответ:</b>\n{message.text}\n\n"
            f"✅ <b>Правильный ответ:</b>\n{case['answer']}"
        )
        st["quiz"] = {}
        rows, kbmap = build_after_case(lang, idx, len(cases))
        _set_screen(uid, screen="quiz_menu", kb_map=kbmap)
        await message.answer(answer_text, reply_markup=kb(rows))
        return

    # 5) Boshqa hech narsaga to'g'ri kelmasa — e'tiborsiz qoldiramiz

# ══════════════════════════════════════════════════════════════════════════════
#  АДМИН ПАНЕЛИ — Далиллар базаси ва Медиаконтент файлларини юклаш
# ══════════════════════════════════════════════════════════════════════════════
def _extract_uploaded_file(message: Message):
    if message.document:
        return {"type": "document", "file_id": message.document.file_id,
                "file_name": message.document.file_name or ""}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id,
                "file_name": message.video.file_name or ""}
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "file_name": ""}
    if message.animation:
        return {"type": "animation", "file_id": message.animation.file_id,
                "file_name": message.animation.file_name or ""}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id,
                "file_name": message.audio.file_name or ""}
    return None

@dp.message(
    F.from_user.id.func(lambda uid: uid == ADMIN_ID),
    F.document | F.photo | F.video | F.animation | F.audio
)
async def admin_upload_file(message: Message):
    uid = message.from_user.id
    state = admin_upload_state.get(uid)
    if not state:
        return  # admin fayl yuklash rejimida emas — e'tiborsiz qoldiramiz

    lang    = get_lang(uid) or "uz"
    drug_id = state["drug_id"]
    cat     = state["cat"]

    f = _extract_uploaded_file(message)
    if not f:
        return

    if cat == "evidence":
        if f["type"] != "document" or not f["file_name"].lower().endswith(EVIDENCE_EXTS):
            await message.answer(
                "⚠️ Далиллар базасига фақат DOCX, XLSX ёки PDF форматидаги ҳужжат юбориш мумкин."
            )
            return

    _drug_files(drug_id, cat).append(f)
    _save_data()

    rows, kbmap = build_admin_upload_kb()
    _set_screen(uid, screen="admin_upload", kb_map=kbmap)

    count = len(_drug_files(drug_id, cat))
    name  = f["file_name"] or f["type"]
    await message.answer(
        f"✅ Сақланди: <code>{name}</code>\nЖами: {count} та файл.",
        reply_markup=kb(rows)
    )

# ══════════════════════════════════════════════════════════════════════════════
#  BROADCAST
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("❗ Формат: /broadcast Матн")
        return
    ok = fail = 0
    for uid in list(APPROVED):
        try:
            await bot.send_message(uid, f"📢 <b>Админ хабари:</b>\n\n{text}")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Юборилди: {ok} та\n❌ Хато: {fail} та")

# ══════════════════════════════════════════════════════════════════════════════
#  FOYDALANUVCHILAR RO'YXATI
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    if not APPROVED:
        await message.answer("👥 Ҳозирча уланган фойдаланувчилар йўқ.")
        return

    lines = [f"👥 <b>Жами уланган фойдаланувчилар: {len(APPROVED)}</b>\n"]
    for uid in sorted(APPROVED):
        info  = USER_INFO.get(uid, {})
        uname = f"@{info['username']}" if info.get("username") else "—"
        full  = info.get("full_name") or "—"
        lines.append(f"🆔 <code>{uid}</code> | {full} | {uname}")

    if PENDING:
        lines.append(f"\n⏳ <b>Кутилаётган сўровлар: {len(PENDING)}</b>")
        for uid, info in PENDING.items():
            uname = f"@{info['username']}" if info.get("username") else "—"
            full  = info.get("full_name") or "—"
            lines.append(f"🆔 <code>{uid}</code> | {full} | {uname}")

    # Telegram xabar chegarasi (4096) uchun bo'laklarga bo'lamiz
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)

# ══════════════════════════════════════════════════════════════════════════════
#  RENDER УЧУН МИНИ ВЕБ-СЕРВЕР (health-check / uyg'oq turish uchun)
#  Render'ning bepul "Web Service" turi ochiq port kutadi, shuning uchun
#  polling bilan bir vaqtda shu yengil HTTP server ham ishga tushiriladi.
# ══════════════════════════════════════════════════════════════════════════════
async def _health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Health-check сервери {PORT}-портда ишга тушди.")

# ══════════════════════════════════════════════════════════════════════════════
#  ISHGA TUSHIRISH — polling + mini web server
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    print("Бот фаолият бошлади (polling режими)...")
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
