# -*- coding: utf-8 -*-
"""
Лид-бот клуба «Сандов Фитнес».

Подарок — 72 часа в клубе (три дня подряд). Дальше два вопроса, цель и
направление, и человек оставляет номер. Заявка падает в группу «Sandow заявки».

Вводную тренировку бот не обещает: её дарит менеджер от себя во время звонка —
это его козырь, и он не должен быть израсходован ботом.

Работает через webhook: Telegram присылает обновление на /tg/<секрет>,
бот отвечает и уходит спать до следующего сообщения.
"""
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

app = Flask(__name__)

# Состояние диалога живёт в памяти: он короткий, а ответы всё равно
# продублированы в callback_data кнопок.
STATE = {}
LAST_LEAD = {}
LOCK = threading.Lock()

CLUB = "Нижегородская ул., 29/33, стр. 3"
PHONE = "+7 (495) 795-69-57"
GIFT = "72 часа в клубе"

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


ASK_PHONE = {
    "keyboard": [[{"text": "📱 Отправить мой номер", "request_contact": True}]],
    "resize_keyboard": True, "one_time_keyboard": True,
}


# ---------------------------------------------------------------- шаги диалога

def step_hello(chat_id, name):
    hi = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    text = (
        f"{hi} Клуб «Сандов Фитнес» на Нижегородской.\n\n"
        "<b>Дарим 72 часа в клубе.</b> Это три дня один за другим — полный доступ: "
        "тренажёрный зал 1100 м², групповые программы по расписанию, финская сауна.\n\n"
        "За один визит клуб не оценишь: первый раз только смотришь, где что лежит. "
        "Три дня подряд — это уже режим: успеете позаниматься по-настоящему и понять, "
        "ваше это или нет. С какого дня начать, подберёте с менеджером."
    )
    api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=kb([
            [("Забрать 72 часа", "go")],
            [("Интересуют единоборства", "combat")],
            [("У меня вопрос", "ask")],
        ]))


def step_goal(chat_id, message_id):
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text="Какая задача?", reply_markup=kb([
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
    gname = GOALS.get(goal, "")
    dname, dtext = DIRS.get(direction, DIRS["any"])
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        parse_mode="HTML",
        text=(f"{dtext}\n\n"
              f"Записал: <b>{gname.lower()}</b>, начнёте с направления «{dname.lower()}».\n\n"
              "Менеджер позвонит, подберёт дни под ваш график и расскажет, что взять с собой."))
    api("sendMessage", chat_id=chat_id,
        text="Куда звонить? Нажмите кнопку внизу или напишите номер сообщением.",
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
    text = ("Бойцовский клуб 500 м², бокс и кикбоксинг. <b>Первая тренировка по боксу "
            "бесплатная</b>, и 72 часа в клубе тоже ваши — зал и сауна входят.\n\n"
            "Оставьте номер — менеджер скажет, когда ближайшая тренировка и что взять.")
    with LOCK:
        STATE.setdefault(chat_id, {}).update({"goal": "stress", "dir": "fight"})
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="HTML")
    else:
        api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
    api("sendMessage", chat_id=chat_id, text="Куда звонить?", reply_markup=ASK_PHONE)


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

TAIL = "\n\nЗаберёте 72 часа? Оставьте номер — менеджер позвонит и подберёт дни."


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
    api("answerCallbackQuery", callback_query_id=cq["id"])

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

    # Бот состоит в рабочей группе, чтобы отправлять туда заявки. Отвечать
    # там он не должен: на любое сообщение коллег он предлагал подарок
    # и засорял группу. Диалог ведём только в личных чатах.
    if msg["chat"].get("type") != "private":
        return

    if msg.get("contact"):
        phone = clean_phone(msg["contact"].get("phone_number"))
        return finish(chat_id, user, phone)

    if text.startswith("/start"):
        with LOCK:
            STATE.pop(chat_id, None)
        return step_hello(chat_id, user.get("first_name"))

    if text.startswith("/help"):
        return api("sendMessage", chat_id=chat_id,
                   text=f"Клуб «Сандов Фитнес», {CLUB}. Телефон {PHONE}.\n"
                        "Наберите /start, чтобы забрать 72 часа в клубе.")

    # человек прислал номер текстом
    if PHONE_RE.fullmatch(text or "") or len(re.sub(r"\D", "", text or "")) >= 10:
        phone = clean_phone(text)
        if phone:
            return finish(chat_id, user, phone)
        return api("sendMessage", chat_id=chat_id,
                   text="Не разобрал номер. Пришлите в формате +7 999 123-45-67 "
                        "или нажмите кнопку «Отправить мой номер».")

    answer = faq_answer(text)
    if answer:
        return api("sendMessage", chat_id=chat_id, text=answer + TAIL,
                   reply_markup=kb([[("Забрать 72 часа", "go")]]))

    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="<b>Дарим 72 часа в клубе</b> — три дня один за другим, полный доступ: "
             "зал, групповые, сауна.\n\nДва вопроса — и менеджер подберёт дни.",
        reply_markup=kb([[("Забрать 72 часа", "go")],
                         [("Интересуют единоборства", "combat")]]))


def finish(chat_id, user, phone):
    if not phone:
        return api("sendMessage", chat_id=chat_id,
                   text="Не разобрал номер. Пришлите в формате +7 999 123-45-67.")
    with LOCK:
        st = STATE.get(chat_id, {})
    goal, direction = st.get("goal", "keep"), st.get("dir", "any")
    if too_soon(user.get("id")):
        return api("sendMessage", chat_id=chat_id,
                   text="Ваш номер уже у менеджера — он позвонит. "
                        f"Если срочно, наберите нас: {PHONE}",
                   reply_markup={"remove_keyboard": True})
    send_lead(user, phone, goal, direction)
    step_done(chat_id, user.get("first_name"))
    with LOCK:
        STATE.pop(chat_id, None)


# Метка версии: по ней видно, доехал ли новый код до сервера. Render
# иногда не пересобирает сервис, а без панели управления это не проверить.
VERSION = "2026-08-12-1c-ready"


@app.route("/health")
@app.route("/")
def health():
    return jsonify(ok=True, bot="sandow-lead-bot", leads=len(LAST_LEAD),
                   version=VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
