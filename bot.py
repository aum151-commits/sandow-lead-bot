# -*- coding: utf-8 -*-
"""
Бот клуба «Сандов Фитнес» — единая точка входа с сайта.

Версия 2: бот различает две аудитории и ведёт их по-разному.

  • Новичок — как раньше: подарок 72 часа, два вопроса, номер, заявка
    в группу «Sandow заявки» и в 1С.
  • Действующий член клуба — меню без продажи: расписание, заморозка,
    переписка с менеджером. Заявок в 1С не создаёт, менеджеров не дёргает.

Мост с менеджером: сообщение клиента падает в рабочую группу с меткой
#id<чат>. Менеджер отвечает реплаем на это сообщение — бот доставляет
ответ клиенту. Ни телефоны, ни личные аккаунты менеджеров не светятся.

База подписчиков: каждый, кто нажал /start, сохраняется в приватный
репозиторий GitHub (data/tg_subscribers.json) — переживает перезапуски
Render. По этой базе потом делаются сегментированные рассылки: боту
разрешено писать первым каждому, кто его запустил.

Работает через webhook: Telegram присылает обновление на /tg/<секрет>.
"""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ORDERS_CHAT = os.environ.get("ORDERS_CHAT_ID", "").strip()
HOOK_SECRET = os.environ.get("HOOK_SECRET", "sandow").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MSK = timezone(timedelta(hours=3))

# 1С:Фитнес клуб принимает заявки тем же вебхуком, что и формы Тильды: обычная
# форма, не JSON. Адрес содержит секретный идентификатор, поэтому живёт только
# в переменных сервиса. Пусто — отправка выключена, заявка всё равно идёт в группу.
ONEC_WEBHOOK = os.environ.get("ONEC_WEBHOOK", "").strip()
ONEC_SOURCE = os.environ.get("ONEC_SOURCE", "Telegram-бот, сайт").strip()

# База подписчиков — в приватном репозитории, потому что диск Render
# стирается при каждом перезапуске. Пусто — база просто не ведётся.
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_SUBSCRIBERS_REPO", "aum151-commits/sandow-automation").strip()
GH_PATH = "data/tg_subscribers.json"

app = Flask(__name__)

# Состояние диалога живёт в памяти: он короткий, а ответы всё равно
# продублированы в callback_data кнопок.
STATE = {}
LAST_LEAD = {}
LOCK = threading.Lock()

CLUB = "Нижегородская ул., 29/33, стр. 3"
PHONE = "+7 (495) 795-69-57"
GIFT = "72 часа в клубе"
SCHEDULE_CHANNEL = "https://t.me/sandowfit"
FREEZE_URL = "https://sandowfitness.ru/zamorozka"

# Кнопка «Расписание» ведёт не просто в канал, а на последний пост с
# расписанием: в ленте канала сверху бывают отмены занятий, и человек по
# кнопке «расписание» попадал на сообщение об отмене. Свежий пост ищем по
# публичной веб-версии канала, ответ кэшируем на час.
_SCHED = {"url": SCHEDULE_CHANNEL, "ts": 0}


def schedule_url():
    if time.time() - _SCHED["ts"] < 3600:
        return _SCHED["url"]
    try:
        h = requests.get("https://t.me/s/sandowfit", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"}).text
        best = None
        for m in re.finditer(
                r'data-post="sandowfit/(\d+)".*?class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                h, re.S):
            txt = re.sub(r"<[^>]+>", " ", m.group(2))
            if re.search(r"расписани", txt, re.I) and "отмен" not in txt.lower():
                best = m.group(1)
        if best:
            _SCHED["url"] = f"{SCHEDULE_CHANNEL}/{best}"
    except Exception as exc:
        print(f"[sched] {exc}", flush=True)   # не вышло — остаётся ссылка на канал
    _SCHED["ts"] = time.time()
    return _SCHED["url"]

GOALS = {
    "strength": "Набрать форму и силу",
    "shape": "Похудеть, привести тонус",
    "keep": "Держать себя в форме",
    "stress": "Снять стресс, разгрузиться",
}

DIRS = {
    "gym": ("Тренажёрный зал",
            "Зал 1100 м²: свободные веса, тренажёры, помост для становой и приседа."),
    "group": ("Групповые программы",
              "Три отдельных зала. Расписание менеджер пришлёт — там видно, что идёт в ваше время."),
    "fight": ("Бокс и кикбоксинг",
              "Бойцовский клуб 500 м², ринг и мешки. Первая тренировка по боксу бесплатная."),
    "any": ("Ещё не решил",
            "За три дня как раз попробуете разное и поймёте, что заходит."),
}


def api(method, **payload):
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=20)
        return r.json()
    except Exception as exc:  # сеть моргнула — не роняем обработчик
        print(f"[api] {method}: {exc}", flush=True)
        return {}


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def kb_mixed(rows):
    """Клавиатура, где кнопка может быть и ссылкой: ('текст', 'url:https://...')."""
    out = []
    for row in rows:
        line = []
        for t, d in row:
            if d.startswith("url:"):
                line.append({"text": t, "url": d[4:]})
            else:
                line.append({"text": t, "callback_data": d})
        out.append(line)
    return {"inline_keyboard": out}


ASK_PHONE = {
    "keyboard": [[{"text": "📱 Отправить мой номер", "request_contact": True}]],
    "resize_keyboard": True, "one_time_keyboard": True,
}


# ------------------------------------------------------- база подписчиков

def _gh_headers():
    return {"Authorization": "Bearer " + GH_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "sandow-lead-bot"}


def save_subscriber(user, segment=None, phone=None):
    """Дописывает или обновляет запись о человеке в базе подписчиков.

    Работает в отдельном потоке: GitHub отвечает не мгновенно, а Telegram
    ждёт ответ вебхука. Ошибка записи не должна ломать диалог.
    """
    if not GH_TOKEN:
        return
    threading.Thread(target=_save_subscriber, args=(dict(user), segment, phone),
                     daemon=True).start()


def _save_subscriber(user, segment, phone):
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        if r.status_code == 200:
            payload = r.json()
            data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            sha = payload["sha"]
        else:
            data, sha = {}, None

        key = str(user.get("id"))
        rec = data.get(key, {})
        rec.update({
            "name": " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x),
            "username": user.get("username", ""),
            "last_seen": datetime.now(MSK).strftime("%Y-%m-%d %H:%M"),
        })
        rec.setdefault("first_seen", rec["last_seen"])
        if segment:
            rec["segment"] = segment
        if phone:
            rec["phone"] = phone
        data[key] = rec

        body = {"message": f"tg-бот: подписчик {key} ({rec.get('segment', '?')})",
                "content": base64.b64encode(
                    json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()}
        if sha:
            body["sha"] = sha
        w = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
        if w.status_code not in (200, 201):
            print(f"[base] запись не прошла: {w.status_code} {w.text[:120]}", flush=True)
    except Exception as exc:
        print(f"[base] {exc}", flush=True)


# ---------------------------------------------------------------- шаги диалога

def step_gate(chat_id, name):
    """Развилка: бот обслуживает и новых людей, и действующих членов клуба."""
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="Здравствуйте, это Сандов Фитнес на Нижегородской 🏆",
        reply_markup=kb([
            [("Хочу в клуб", "seg:new")],
            [("Уже занимаюсь", "seg:member")],
        ]))


def step_hello(chat_id, message_id=None):
    text = (
        "<b>Дарим 72 часа в клубе.</b> Это три дня один за другим — полный доступ: "
        "тренажёрный зал 1100 м², групповые программы по расписанию, финская сауна.\n\n"
        "За один визит клуб не оценишь: первый раз только смотришь, где что лежит. "
        "Три дня подряд — это уже режим: успеете позаниматься по-настоящему и понять, "
        "ваше это или нет. С какого дня начать, подберёте с менеджером."
    )
    markup = kb([
        [("Забрать 72 часа", "go")],
        [("Интересуют единоборства", "combat")],
        [("У меня вопрос", "ask")],
    ])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="HTML", reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)


def step_member_menu(chat_id, message_id=None, greet=True):
    """Меню действующего члена клуба. Никаких заявок и продаж — только польза."""
    text = ("Рад видеть своих! Чем помочь?\n\n"
            "Если нужен живой человек — жмите «Написать менеджеру», "
            "переписка пойдёт прямо здесь.") if greet else "Чем ещё помочь?"
    markup = kb_mixed([
        [("📅 Расписание групповых", "url:" + schedule_url())],
        [("❄️ Заморозка абонемента", "url:" + FREEZE_URL)],
        [("💬 Написать менеджеру", "bridge")],
        [("📱 Оставить номер для связи", "member_phone")],
    ])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def step_goal(chat_id, message_id, intro=False):
    # Квиз вместо меню: по опыту работающих ботов человеку легче отвечать
    # о себе, чем выбирать услугу из каталога. Подарок вручается в конце —
    # как награда за два ответа, а не как приманка с порога.
    text = ("Подберу вам первые визиты — всего два вопроса.\n\nКакая задача?"
            if intro else "Какая задача?")
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=text, reply_markup=kb([
            [("Набрать форму и силу", "g:strength")],
            [("Похудеть, привести тонус", "g:shape")],
            [("Держать себя в форме", "g:keep")],
            [("Снять стресс, разгрузиться", "g:stress")],
        ]))


def step_dir(chat_id, message_id, goal):
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text="С чего начнёте?", reply_markup=kb([
            [("Тренажёрный зал", f"d:gym:{goal}")],
            [("Групповые программы", f"d:group:{goal}")],
            [("Бокс и кикбоксинг", f"d:fight:{goal}")],
            [("Ещё не решил", f"d:any:{goal}")],
        ]))


def step_phone(chat_id, message_id, goal, direction):
    # Вопрос квиза удаляем: в чате остаётся ровно ОДНО сообщение — финал.
    # Раньше их было два, и на крупном шрифте начало уезжало за экран,
    # человек не видел подарка. Финал короткий — влезает целиком.
    api("deleteMessage", chat_id=chat_id, message_id=message_id)
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🎁 <b>Дарим вам 72 часа в клубе</b> — три дня подряд, "
              "всё включено: зал 1100 м², групповые, сауна.\n\n"
              "Нажмите кнопку внизу или напишите номер.\n\n"
              "Отправляя номер, вы соглашаетесь на обработку персональных данных."),
        reply_markup=ASK_PHONE)


def step_done(chat_id, name):
    hi = f"Готово, {name}." if name else "Готово."
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"{hi} Ваши <b>72 часа</b> закреплены.\n\n"
              "Из формальностей — только паспорт: по нему оформят договор на посещение, "
              "это пять минут. Форма и обувь ваши, остальное наше: полотенца, вода, шкафчик.\n\n"
              f"{CLUB}\n"
              f"Телефон клуба: {PHONE}"),
        reply_markup={"remove_keyboard": True})


def step_combat(chat_id, message_id=None):
    # одно сообщение, подарок первым — как и в основном финале
    with LOCK:
        STATE.setdefault(chat_id, {}).update({"goal": "stress", "dir": "fight"})
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🎁 <b>Первая тренировка по боксу — бесплатная</b>, и 72 часа в клубе "
              "тоже ваши: бойцовский клуб 500 м², зал и сауна входят.\n\n"
              "Оставьте номер — менеджер скажет, когда ближайшая тренировка и что взять."),
        reply_markup=ASK_PHONE)


# --------------------------------------------------------- мост с менеджером

def bridge_on(chat_id, message_id=None):
    with LOCK:
        STATE.setdefault(chat_id, {})["bridge"] = True
    text = ("Пишите — передам менеджеру, ответ придёт прямо сюда.\n"
            "Когда закончите, нажмите «Завершить разговор».")
    markup = kb([[("Завершить разговор", "bridge_off")]])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def bridge_off(chat_id, message_id=None):
    with LOCK:
        st = STATE.get(chat_id)
        if st:
            st.pop("bridge", None)
            st.pop("bridge_ack", None)
    seg = _segment(chat_id)
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text="Разговор завершён. Если что — я тут.")
    if seg == "member":
        step_member_menu(chat_id, greet=False)


def bridge_to_group(chat_id, user, text, message_id=None):
    """Сообщение клиента → рабочая группа. Метка #id — по ней вернётся ответ."""
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    seg = "член клуба" if _segment(chat_id) == "member" else "новый"
    api("sendMessage", chat_id=ORDERS_CHAT, parse_mode="HTML",
        text=(f"💬 <b>СООБЩЕНИЕ ИЗ БОТА</b> ({seg})\n\n"
              f"<b>{who}</b> · {uname}\n\n"
              f"{text}\n\n"
              f"#id{chat_id}\n"
              "Ответьте реплаем на это сообщение — я передам."))
    # Подтверждение не пишем: при входе в режим бот уже сказал «передам,
    # ответ придёт сюда». Достаточно тихой реакции на сообщении клиента.
    if message_id:
        api("setMessageReaction", chat_id=chat_id, message_id=message_id,
            reaction=[{"type": "emoji", "emoji": "👌"}])


BRIDGE_TAG = re.compile(r"#id(-?\d+)")


def bridge_from_group(msg):
    """Реплай менеджера в группе → клиенту. Работает только на реплаях к боту."""
    reply = msg.get("reply_to_message") or {}
    tag = BRIDGE_TAG.search(reply.get("text") or "")
    if not tag:
        return False
    target = int(tag.group(1))
    text = (msg.get("text") or "").strip()
    if not text:
        api("sendMessage", chat_id=msg["chat"]["id"],
            reply_to_message_id=msg["message_id"],
            text="Могу передать только текст — напишите словами.")
        return True
    api("sendMessage", chat_id=target, text=f"Менеджер клуба:\n\n{text}",
        reply_markup=kb([[("Завершить разговор", "bridge_off")]]))
    api("setMessageReaction", chat_id=msg["chat"]["id"],
        message_id=msg["message_id"], reaction=[{"type": "emoji", "emoji": "👌"}])
    return True


def _segment(chat_id):
    with LOCK:
        return STATE.get(chat_id, {}).get("segment", "")


# ------------------------------------------------------------------- заявка

def send_to_1c(user, phone, goal, direction):
    """Заводит заявку в 1С:Фитнес клуб. Возвращает приписку к сообщению в группе.

    Данные уходят формой — тем же способом, каким шлёт Тильда. JSON приёмник
    не принимает. Ошибку не глотаем: менеджер должен знать, что записи в 1С нет.
    """
    if not ONEC_WEBHOOK:
        return ""

    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x).strip()
    data = {
        "Name": name or "Без имени",
        "Phone": re.sub(r"\D", "", phone or ""),
        "Comment": (f"Заявка из Telegram-бота. Подарок: {GIFT}. "
                    f"Задача: {GOALS.get(goal, 'не указана')}. "
                    f"Начнёт с: {DIRS.get(direction, DIRS['any'])[0]}."),
        "source": ONEC_SOURCE,
        "utm_source": "telegram",
        "utm_medium": "bot",
        "utm_campaign": "72h",
        "formname": "Telegram-бот сайта",
        "formid": "sandow_lead_bot",
        "tranid": f"tg-{user.get('id')}-{int(time.time())}",
    }
    try:
        r = requests.post(ONEC_WEBHOOK, data=data, timeout=25)
        if r.status_code == 200:
            print(f"[1c] заявка принята: {r.text[:80]}", flush=True)
            return "\n\n✅ Заведено в 1С"
        print(f"[1c] отказ {r.status_code}: {r.text[:200]}", flush=True)
        return f"\n\n⚠️ В 1С не попало (код {r.status_code}) — занесите вручную"
    except Exception as exc:
        print(f"[1c] ошибка: {exc}", flush=True)
        return "\n\n⚠️ В 1С не попало (нет связи) — занесите вручную"


def send_lead(user, phone, goal, direction):
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    now = datetime.now(MSK).strftime("%d.%m в %H:%M")
    text = (
        "🎁 <b>ЗАЯВКА ИЗ TELEGRAM-БОТА</b>\n\n"
        f"<b>Имя:</b> {who}\n"
        f"<b>Телефон:</b> <code>{phone}</code>\n\n"
        f"<b>Подарок:</b> {GIFT}\n"
        f"<b>Задача:</b> {GOALS.get(goal, 'не указана')}\n"
        f"<b>Начнёт с:</b> {DIRS.get(direction, DIRS['any'])[0]}\n\n"
        f"Telegram: {uname} · {now}"
    )
    text += send_to_1c(user, phone, goal, direction)
    api("sendMessage", chat_id=ORDERS_CHAT, text=text, parse_mode="HTML",
        reply_markup=kb([[("Беру в работу", f"take:{user.get('id')}")]]))


# Частые вопросы: с сайта заходят с ними чаще, чем с готовностью оставить номер.
# Цены не называем — ведём на подарок и разговор с менеджером.
FAQ = [
    (("скольк", "цен", "стоим", "прайс", "абонемент", "почём", "почем"),
     "Зависит от формата и частоты — менеджер подберёт под вас. "
     "А начать можно с подарочных 72 часов, они бесплатные."),
    (("бассейн", "плава", "аква"),
     "Бассейна у нас нет, клуб сухой — говорю честно. Если бассейн обязателен, мы не подойдём. "
     "Если нет — 2500 м², сауна и бойцовский клуб в одном месте."),
    (("адрес", "где вы", "как добра", "метро"),
     f"{CLUB}."),
    # Парковку охраняет бизнес-центр, и она положена не всем — гостю не обещаем.
    (("парков", "машин", "припарк"),
     "Парковка у бизнес-центра есть. Членам клуба она доступна не на всех абонементах — "
     "менеджер скажет точно по вашему варианту."),
    # Круглосуточно — только для членов клуба; гостю время назовёт менеджер.
    (("график", "во сколь", "режим", "часы работ", "круглосут", "ночью работ"),
     "Для членов клуба вход круглосуточный, без выходных. По гостевому визиту время "
     "подберёт менеджер — скажет, когда вам удобнее прийти."),
    (("сауна", "полотенц", "душ", "раздевал"),
     "Финская сауна и ведро-водопад. Полотенца и вода без доплат."),
    (("бокс", "кикбокс", "единоборств", "бойцов", "борьб"),
     "Бойцовский клуб 500 м², бокс и кикбоксинг. Первая тренировка по боксу бесплатная."),
    (("тренер", "персональн", "инструктор"),
     "Персональные тренеры есть, подберём под вашу задачу — менеджер расскажет."),
    (("групповы", "расписан", "йога", "пилатес", "аэроб"),
     "Три зала групповых программ, входят в абонемент. Расписание пришлёт менеджер."),
    (("паспорт", "документ", "что взять", "с собой", "договор"),
     "Нужен паспорт — по нему оформляют договор на посещение, это пять минут на месте. "
     "Ещё форма и обувь, остальное наше."),
]

TAIL = "\n\nХотите попробовать? Подберу первые визиты — всего два вопроса."


def faq_answer(text):
    low = (text or "").lower()
    for keys, answer in FAQ:
        if any(k in low for k in keys):
            return answer
    return None


PHONE_RE = re.compile(r"[\d\+\-\(\)\s]{10,20}")


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return f"+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"


def too_soon(uid):
    """Один человек — одна заявка в сутки, чтобы менеджеров не заваливало."""
    with LOCK:
        last = LAST_LEAD.get(uid)
        if last and time.time() - last < 86400:
            return True
        LAST_LEAD[uid] = time.time()
        return False


# ------------------------------------------------------------------- webhook

@app.route(f"/tg/{HOOK_SECRET}", methods=["POST"])
def hook():
    upd = request.get_json(force=True, silent=True) or {}
    try:
        handle(upd)
    except Exception as exc:
        print(f"[hook] {exc}", flush=True)
    return jsonify(ok=True)


def handle(upd):
    if "callback_query" in upd:
        return on_button(upd["callback_query"])
    msg = upd.get("message") or upd.get("edited_message")
    if msg:
        return on_message(msg)


def on_button(cq):
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    user = cq.get("from", {})
    api("answerCallbackQuery", callback_query_id=cq["id"])

    if data == "seg:new":
        with LOCK:
            STATE.setdefault(chat_id, {})["segment"] = "new"
        save_subscriber(user, segment="new")
        return step_goal(chat_id, mid, intro=True)

    if data == "seg:member":
        with LOCK:
            STATE.setdefault(chat_id, {})["segment"] = "member"
        save_subscriber(user, segment="member")
        return step_member_menu(chat_id, mid)

    if data == "bridge":
        return bridge_on(chat_id, mid)

    if data == "bridge_off":
        return bridge_off(chat_id, mid)

    if data == "member_phone":
        with LOCK:
            STATE.setdefault(chat_id, {})["member_phone"] = True
        api("editMessageText", chat_id=chat_id, message_id=mid,
            text="Нажмите кнопку внизу — я сохраню номер, чтобы находить вас "
                 "в системе клуба и присылать только то, что касается вас.\n\n"
                 "Отправляя номер, вы соглашаетесь на обработку персональных данных.")
        return api("sendMessage", chat_id=chat_id, text="Одним нажатием:",
                   reply_markup=ASK_PHONE)

    if data == "go":
        return step_goal(chat_id, mid)

    if data == "combat":
        return step_combat(chat_id, mid)

    if data == "ask":
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="Спрашивайте. Отвечу сам, а если нужно подробнее — менеджер перезвонит.")

    if data.startswith("g:"):
        goal = data.split(":", 1)[1]
        with LOCK:
            STATE.setdefault(chat_id, {})["goal"] = goal
        return step_dir(chat_id, mid, goal)

    if data.startswith("d:"):
        _, direction, goal = data.split(":", 2)
        with LOCK:
            STATE.setdefault(chat_id, {}).update({"goal": goal, "dir": direction})
        return step_phone(chat_id, mid, goal, direction)

    if data.startswith("take:"):
        who = cq["from"].get("first_name", "менеджер")
        old = cq["message"].get("text", "")
        api("editMessageText", chat_id=chat_id, message_id=mid,
            text=old + f"\n\n✅ В работе: {who}")


def on_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()

    # В группах бот молчит — с одним исключением: реплай менеджера на
    # сообщение с меткой #id он доставляет клиенту.
    if msg["chat"].get("type") != "private":
        if msg.get("reply_to_message"):
            bridge_from_group(msg)
        return

    # Ответ менеджера реплаем на карточку с меткой #id. Обычно карточки живут
    # в рабочей группе, но при тестовом режиме ORDERS_CHAT — личный чат, и
    # реплаи должны работать и там.
    if str(chat_id) == str(ORDERS_CHAT) and msg.get("reply_to_message"):
        if bridge_from_group(msg):
            return

    if msg.get("contact"):
        phone = clean_phone(msg["contact"].get("phone_number"))
        return finish(chat_id, user, phone)

    if text.startswith("/start"):
        with LOCK:
            STATE.pop(chat_id, None)
        save_subscriber(user)
        return step_gate(chat_id, user.get("first_name"))

    if text.startswith("/help"):
        return api("sendMessage", chat_id=chat_id,
                   text=f"Клуб «Сандов Фитнес», {CLUB}. Телефон {PHONE}.\n"
                        "Наберите /start — помогу и с первым визитом, и по клубу.")

    # мост открыт — любые слова уходят менеджеру
    with LOCK:
        in_bridge = STATE.get(chat_id, {}).get("bridge")
    if in_bridge and text:
        return bridge_to_group(chat_id, user, text, msg.get("message_id"))

    # человек прислал номер текстом
    if PHONE_RE.fullmatch(text or "") or len(re.sub(r"\D", "", text or "")) >= 10:
        phone = clean_phone(text)
        if phone:
            return finish(chat_id, user, phone)
        return api("sendMessage", chat_id=chat_id,
                   text="Не разобрал номер. Пришлите в формате +7 999 123-45-67 "
                        "или нажмите кнопку «Отправить мой номер».")

    # член клуба написал текстом без моста: не продаём, зовём в диалог
    if _segment(chat_id) == "member":
        answer = faq_answer(text)
        if answer:
            return api("sendMessage", chat_id=chat_id, text=answer,
                       reply_markup=kb([[("💬 Написать менеджеру", "bridge")]]))
        return api("sendMessage", chat_id=chat_id,
                   text="Передать это менеджеру? Нажмите кнопку — и переписка "
                        "пойдёт прямо здесь.",
                   reply_markup=kb([[("💬 Написать менеджеру", "bridge")],
                                    [("Показать меню", "seg:member")]]))

    answer = faq_answer(text)
    if answer:
        return api("sendMessage", chat_id=chat_id, text=answer + TAIL,
                   reply_markup=kb([[("Подобрать первые визиты", "go")],
                                    [("💬 Написать менеджеру", "bridge")]]))

    api("sendMessage", chat_id=chat_id,
        text="Подберу вам первые визиты — всего два вопроса. "
             "Или спросите словами, отвечу.",
        reply_markup=kb([[("Подобрать первые визиты", "go")],
                         [("💬 Написать менеджеру", "bridge")],
                         [("Я член клуба", "seg:member")]]))


def finish(chat_id, user, phone):
    if not phone:
        return api("sendMessage", chat_id=chat_id,
                   text="Не разобрал номер. Пришлите в формате +7 999 123-45-67.")

    # член клуба делится номером для связи — это не заявка: в 1С не шлём,
    # менеджеров не дёргаем, просто запоминаем в базе
    with LOCK:
        member_phone = STATE.get(chat_id, {}).pop("member_phone", False)
    if member_phone or _segment(chat_id) == "member":
        save_subscriber(user, segment="member", phone=phone)
        return api("sendMessage", chat_id=chat_id,
                   text="Сохранил. Теперь буду присылать только то, что касается вас.",
                   reply_markup={"remove_keyboard": True})

    with LOCK:
        st = STATE.get(chat_id, {})
    goal, direction = st.get("goal", "keep"), st.get("dir", "any")
    if too_soon(user.get("id")):
        return api("sendMessage", chat_id=chat_id,
                   text="Ваш номер уже у менеджера — он позвонит. "
                        f"Если срочно, наберите нас: {PHONE}",
                   reply_markup={"remove_keyboard": True})
    save_subscriber(user, segment="new", phone=phone)
    send_lead(user, phone, goal, direction)
    step_done(chat_id, user.get("first_name"))
    with LOCK:
        st = STATE.get(chat_id, {})
        seg = st.get("segment")
        STATE[chat_id] = {"segment": seg} if seg else {}


# Метка версии: по ней видно, доехал ли новый код до сервера. Render
# иногда не пересобирает сервис, а без панели управления это не проверить.
VERSION = "2026-08-13-v2-members-bridge"


@app.route("/health")
@app.route("/")
def health():
    return jsonify(ok=True, bot="sandow-lead-bot", leads=len(LAST_LEAD),
                   version=VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
