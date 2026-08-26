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
# Переписка с клиентами идёт в отдельную группу, чтобы не засорять заявки.
# Пока переменная не задана — падает туда же, куда заявки (ничего не теряется).
BRIDGE_CHAT = os.environ.get("BRIDGE_CHAT_ID", "").strip() or ORDERS_CHAT
# Ссылка на Telegram, подключённый к 1С (Телеграм Премиум). Когда задана,
# кнопка «Написать менеджеру» ведёт клиента прямо туда: переписка рождается
# в родном канале 1С — видна во вкладке мессенджера и в карточке клиента.
# Пока пусто — работает встроенный мост через группу.
MANAGER_TG_URL = os.environ.get("MANAGER_TG_URL", "").strip()
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
              "Три отдельных зала. Расписание пришлём — там видно, что идёт в ваше время."),
    "fight": ("Бокс и кикбоксинг",
              "Бойцовский клуб 500 м², ринг и мешки. Первая тренировка по боксу бесплатная."),
    "any": ("Ещё не решил",
            "За три дня как раз попробуете разное и поймёте, что заходит."),
}


# Тематические входы с лендингов: t.me/sandowclub_bot?start=<код>.
# Человек пришёл со страницы про конкретное направление — первое сообщение
# продолжает тему, а не начинает с нуля (раньше был шов: лендинг про бокс,
# а бот сразу про 72 часа). Дальше сценарий обычный, без изменений.
# Код темы уходит менеджеру строкой «Источник» в заявке.
# ММА в текстах нет намеренно: в клубе только бокс и кикбоксинг.
LANDINGS = {
    "boks": ("лендинг Бокс",
             "Интересует бокс? Отличный выбор — в «Сандов Фитнес» первое занятие "
             "боксом бесплатное. И сверх этого дарим 72 часа в клубе: тренажёрный "
             "зал 1100 м², групповые программы и финская сауна — три дня полного доступа."),
    "kik": ("лендинг Кикбоксинг",
            "Интересует кикбоксинг? В «Сандов Фитнес» он есть и в группах по "
            "расписанию, и персонально с тренером. А ещё дарим 72 часа в клубе — "
            "три дня полного доступа: зал 1100 м², групповые программы, сауна."),
    "zal": ("лендинг Тренажёрный зал",
            "Ищете тренажёрный зал? В «Сандов Фитнес» он занимает 1100 м²: свободные "
            "веса, тренажёры по группам мышц, помост для становой и приседа. "
            "И дарим 72 часа в клубе — три дня подряд, чтобы всё попробовать."),
    "noch": ("лендинг Круглосуточный фитнес",
             "Удобнее тренироваться поздно вечером или рано утром? «Сандов Фитнес» "
             "открыт для членов клуба круглосуточно, без выходных. А знакомство "
             "начните с подарка — дарим 72 часа в клубе: зал 1100 м², "
             "групповые программы, сауна."),
    "boec": ("лендинг Бойцовский клуб",
             "Интересуют единоборства? Бойцовский клуб «Сандов Фитнес» — 500 м²: "
             "бокс и кикбоксинг, ринг и мешки, персональные тренировки. Первое "
             "занятие боксом бесплатное, и сверх этого дарим 72 часа в клубе."),
}


FALLBACK_CHAT = os.environ.get("FALLBACK_CHAT_ID", "220285486").strip()


def api(method, **payload):
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=20)
        out = r.json()
        if not out.get("ok"):
            print(f"[api] {method} отказ: {out.get('description')}", flush=True)
        return out
    except Exception as exc:  # сеть моргнула — не роняем обработчик
        print(f"[api] {method}: {exc}", flush=True)
        return {}


def send_to_orders(**payload):
    """Отправка в рабочую группу с запасным выходом.

    14.08 бот оказался удалён из группы заявок, и заявка ушла в никуда —
    молча. Теперь при недоступной группе сообщение падает в личный чат
    Ольги с пометкой тревоги: потерять заявку тихо больше нельзя.
    """
    r = api("sendMessage", chat_id=ORDERS_CHAT, **payload)
    if r.get("ok"):
        return r
    warn = "⚠️ ГРУППА ЗАЯВОК НЕДОСТУПНА (бот не в группе?) — сообщение ниже не дошло:\n\n"
    payload["text"] = warn + payload.get("text", "")
    return api("sendMessage", chat_id=FALLBACK_CHAT, **payload)


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


LEADS_GH_PATH = "data/lead_response_log.json"


def log_lead_event(user_id, event, who=None):
    """Пишет момент создания заявки и момент «Беру в работу» — источник для
    метрики SLA (норматив 3 минуты, по разбору с коучем 18.08.2026). Раньше
    такого лога не было нигде: ни у Тильды, ни у бота, поэтому текущее время
    ответа не с чем было сверить."""
    if not GH_TOKEN:
        return
    threading.Thread(target=_log_lead_event, args=(str(user_id), event, who), daemon=True).start()


def _log_lead_event(user_id, event, who):
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{LEADS_GH_PATH}"
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        if r.status_code == 200:
            payload = r.json()
            data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            sha = payload["sha"]
        else:
            data, sha = {}, None

        rec = data.get(user_id, {})
        now = datetime.now(MSK)
        if event == "lead_created":
            rec["lead_created_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        elif event == "taken" and not rec.get("taken_at"):
            rec["taken_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            rec["taken_by"] = who or ""
            if rec.get("lead_created_at"):
                try:
                    created = datetime.strptime(rec["lead_created_at"], "%Y-%m-%d %H:%M:%S")
                    rec["response_seconds"] = int((now.replace(tzinfo=None) - created).total_seconds())
                except Exception:
                    pass
        data[user_id] = rec

        body = {"message": f"lead-log: {event} {user_id}",
                "content": base64.b64encode(
                    json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()}
        if sha:
            body["sha"] = sha
        w = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
        if w.status_code not in (200, 201):
            print(f"[lead-log] запись не прошла: {w.status_code} {w.text[:120]}", flush=True)
    except Exception as exc:
        print(f"[lead-log] {exc}", flush=True)


def save_subscriber(user, segment=None, phone=None, call_name=None):
    """Дописывает или обновляет запись о человеке в базе подписчиков.

    Работает в отдельном потоке: GitHub отвечает не мгновенно, а Telegram
    ждёт ответ вебхука. Ошибка записи не должна ломать диалог.
    """
    if not GH_TOKEN:
        return
    threading.Thread(target=_save_subscriber, args=(dict(user), segment, phone, call_name),
                     daemon=True).start()


def _save_subscriber(user, segment, phone, call_name=None):
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
        if call_name:
            rec["call_name"] = call_name
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

def step_gate(chat_id, name, theme=None):
    """Развилка: бот обслуживает и новых людей, и действующих членов клуба.

    theme — код лендинга из deep-link (/start boks): первое сообщение
    начинается с темы страницы, дальше сценарий прежний. Без темы —
    текст прежний, слово в слово.
    """
    if theme in LANDINGS:
        text = LANDINGS[theme][1]
    else:
        text = "Здравствуйте, это Сандов Фитнес на Нижегородской 🏆"
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=text,
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
        "ваше это или нет. С какого дня начать — подберём вместе."
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
    text = "Рад видеть своих! Чем помочь?" if greet else "Чем ещё помочь?"
    # «Написать менеджеру» — через событие: так в группу заявок уходит
    # уведомление об обращении (требование Ольги), а клиент получает кнопку
    # перехода в чат клуба. Прямую ссылку Telegram без потери уведомления
    # не умеет: url-кнопки не сообщают боту о нажатии.
    markup = kb_mixed([
        [("📅 Расписание групповых программ", "url:" + schedule_url())],
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


def step_dir(chat_id, message_id, goal, intro=False):
    # Сокращение 15.08 по тесту Ольги: путь был в 5 шагов, клиент не доходил.
    # Вопрос о цели убран (её выясняет менеджер в звонке) — остался один вопрос.
    text = ("Подберу вам первые визиты — один вопрос.\n\nС чего начнёте?"
            if intro else "С чего начнёте?")
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=text, reply_markup=kb([
            [("Тренажёрный зал", f"d:gym:{goal}")],
            [("Групповые программы", f"d:group:{goal}")],
            [("Бокс и кикбоксинг", f"d:fight:{goal}")],
            [("Ещё не решил", f"d:any:{goal}")],
        ]))


def step_phone(chat_id, message_id, goal, direction):
    # Вопрос квиза удаляем: в чате остаётся ровно ОДНО сообщение — финал.
    # Первая строка — тёплый отклик на выбор: человек видит, что его услышали.
    warm = {
        "gym": "Отличный выбор — железо не врёт 💪",
        "group": "Отличный выбор — групповые программы втягивают быстрее всего 🔥",
        "fight": "Уважаем — бокс закаляет 🥊",
        "any": "И правильно — попробуете всё и поймёте, что ваше 👍",
    }.get(direction, "Отличный план 👍")
    dname = DIRS.get(direction, DIRS["any"])[0].lower()
    # «начнёте с направления „ещё не решил“» звучит нелепо — для этого
    # варианта направление не называем, тёплая строка уже всё сказала.
    # Вопроса о цели больше нет — упоминаем только направление.
    fixed = ("" if direction == "any"
             else f"Записал: начнёте с направления «{dname}».")
    api("deleteMessage", chat_id=chat_id, message_id=message_id)
    body = f"{warm}\n{fixed}\n\n" if fixed else f"{warm}\n\n"
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(body +
              "🎁 <b>Дарим вам 72 часа в клубе</b> — три дня подряд, "
              "всё включено: зал 1100 м², групповые программы, сауна.\n\n"
              "Чтобы закрепить подарок за вами — нажмите кнопку внизу или напишите номер.\n\n"
              "Отправляя номер, вы соглашаетесь на обработку персональных данных."),
        reply_markup=ASK_PHONE)


def step_done(chat_id, name):
    # Сокращение 15.08: вопрос «как обращаться?» убран (имя берём из Телеграма,
    # остальное менеджер уточнит в звонке) — сразу мост в клубный Телеграм.
    # Тексты моста согласованы Ольгой: коротко, кнопка сама говорит, что делать.
    hi = f"Готово, {name}!" if name else "Готово!"
    if MANAGER_TG_URL:
        btn = kb_mixed([[("Написать нам «Подарок»", "url:" + MANAGER_TG_URL)]])
        api("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"{hi} Подарок закреплён 🎁 До него один шаг:",
            reply_markup=btn)

        def _nudge(cid=chat_id, b=btn):
            api("sendMessage", chat_id=cid, text="Подарок ждёт 🎁", reply_markup=b)
        threading.Timer(2700, _nudge).start()
        return
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"{hi} Ваши <b>72 часа</b> закреплены — мы с вами свяжемся и подберём дни.\n\n"
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
              "Оставьте номер — подскажем, когда ближайшая тренировка и что взять."),
        reply_markup=ASK_PHONE)


# --------------------------------------------------------- мост с менеджером

def bridge_on(chat_id, message_id=None, user=None):
    # Подключён Telegram клуба (интеграция с 1С): клиента отправляем туда —
    # переписка рождается во вкладке мессенджера 1С и в карточке клиента.
    # Здесь остаётся только отметка в группу заявок для контроля.
    u = user or {}
    phone = lookup_phone(u.get("id")) if u.get("id") else ""

    # Без номера переписку не открываем: ник без номера — отдельная,
    # не связанная с 1С карточка (инцидент с Лерой 16.08.2026, у клиента
    # завелись две несвязанные карточки — ПЧК и ЧК). Сначала номер,
    # потом мост. Поправка Ольги 16.08.2026.
    if not phone:
        with LOCK:
            st = STATE.setdefault(chat_id, {})
            st["member_phone"] = True
            st["want_bridge_after_phone"] = True
        # ASK_PHONE — обычная (не инлайн) клавиатура, editMessageText с ней
        # не работает (Telegram ждёт инлайн-разметку) — поэтому удаляем
        # старое сообщение с кнопкой и шлём новое, как в "member_phone".
        if message_id:
            api("deleteMessage", chat_id=chat_id, message_id=message_id)
        api("sendMessage", chat_id=chat_id,
            text=("Чтобы связать переписку с вашей карточкой в 1С, сначала оставьте "
                  "номер — один раз, дальше не будем спрашивать."),
            reply_markup=ASK_PHONE)
        return

    if MANAGER_TG_URL:
        who = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or "без имени"
        uname = f"@{u['username']}" if u.get("username") else "без ника"
        seg = "член клуба" if _segment(chat_id) == "member" else "новый"
        pline = f"\n<b>Телефон:</b> <code>{phone}</code>" if phone else ""
        # Уведомление с подтверждением: пока никто не нажал «Беру» — бот
        # напоминает через 30 и 60 минут. «Прозевать» становится невозможно.
        ack_key = f"ack:{chat_id}:{int(time.time())}"
        send_to_orders(parse_mode="HTML",
            text=(f"💬 <b>Открыл чат клуба</b> ({seg})\n"
                  f"<b>{who}</b> · {uname}{pline}\n"
                  "Переписка — в 1С, вкладка Телеграм. Кто смотрит — нажмите «Беру»."),
            reply_markup=kb([[("✅ Беру", ack_key)]]))

        def _remind(round_no, w=who, p=pline, k=ack_key):
            with LOCK:
                if STATE.get(k):
                    return              # уже взяли в работу
            mark = "⏰" if round_no == 1 else "⚠️ ВТОРОЕ НАПОМИНАНИЕ"
            send_to_orders(parse_mode="HTML",
                text=(f"{mark} <b>Проверьте вкладку Телеграм в 1С</b> — "
                      f"чат открывал: <b>{w}</b>{p}"),
                reply_markup=kb([[("✅ Беру", k)]]))
            if round_no == 1:
                threading.Timer(1800, _remind, args=(2,)).start()
        threading.Timer(1800, _remind, args=(1,)).start()
        # если человек не тапнет кнопку, а напишет прямо здесь — мост подхватит:
        # сообщение уйдёт в группу заявок, менеджер ответит реплаем
        with LOCK:
            STATE.setdefault(chat_id, {})["bridge"] = True
        markup = kb_mixed([[("✍️ Написать", "url:" + MANAGER_TG_URL)]])
        if message_id:
            return api("editMessageText", chat_id=chat_id, message_id=message_id,
                       text="Мы на связи — пишите 🙂",
                       reply_markup=markup)
        return api("sendMessage", chat_id=chat_id,
                   text="Мы на связи — пишите 🙂",
                   reply_markup=markup)

    with LOCK:
        STATE.setdefault(chat_id, {})["bridge"] = True
    text = "Пишите — передам менеджеру, ответ придёт прямо сюда."
    markup = kb([[("Завершить разговор", "bridge_off")]])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def archive_dialog(chat_id):
    """Завершённый разговор → в 1С (обращение в карточке клиента) и в базу.

    Открытый API 1С не умеет дописывать чат в карточку, поэтому используем
    лид-приёмник: он привязывает обращение к клиенту по номеру телефона.
    Без номера в 1С не шлём — некому привязывать; копия в базе остаётся всегда.
    """
    with LOCK:
        st = STATE.get(chat_id, {})
        dlg = st.pop("dlg", [])
        seg = st.get("segment", "")
    if not dlg:
        return
    lines = "\n".join(f"— {who}: {txt}" for who, txt in dlg[:60])
    stamp = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")

    def _push():
        phone = lookup_phone(chat_id)
        # вечная копия в базе подписчиков
        try:
            url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
            r = requests.get(url, headers=_gh_headers(), timeout=30)
            if r.status_code == 200:
                payload = r.json()
                data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
                rec = data.setdefault(str(chat_id), {})
                hist = rec.setdefault("dialogs", [])
                hist.append({"date": stamp, "text": lines})
                del hist[:-20]          # держим последние 20 разговоров
                requests.put(url, headers=_gh_headers(), timeout=30, json={
                    "message": f"tg-бот: переписка {chat_id}",
                    "content": base64.b64encode(json.dumps(
                        data, ensure_ascii=False, indent=1).encode()).decode(),
                    "sha": payload["sha"]})
        except Exception as exc:
            print(f"[dlg-base] {exc}", flush=True)
        # дубль в 1С — только если знаем номер
        if not (ONEC_WEBHOOK and phone):
            return
        try:
            kind = "член клуба" if seg == "member" else "новый клиент"
            requests.post(ONEC_WEBHOOK, timeout=25, data={
                "Name": "Переписка из Telegram-бота",
                "Phone": re.sub(r"\D", "", phone),
                "Comment": (f"Переписка из Telegram-бота ({kind}), {stamp}. "
                            f"НЕ заявка на продажу — журнал диалога:\n{lines}"),
                "source": ONEC_SOURCE,
                "formname": "Переписка Telegram-бота",
                "formid": "sandow_bot_dialog",
                "tranid": f"tgdlg-{chat_id}-{int(time.time())}",
            })
            print(f"[dlg-1c] переписка {chat_id} выгружена", flush=True)
        except Exception as exc:
            print(f"[dlg-1c] {exc}", flush=True)

    threading.Thread(target=_push, daemon=True).start()


def bridge_off(chat_id, message_id=None):
    archive_dialog(chat_id)
    with LOCK:
        st = STATE.get(chat_id)
        if st:
            st.pop("bridge", None)
            st.pop("bridge_ack", None)
            st.pop("bridge_fallback_notified", None)
    seg = _segment(chat_id)
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text="Разговор завершён. Если что — я тут.")
    if seg == "member":
        step_member_menu(chat_id, greet=False)


def lookup_phone(user_id):
    """Телефон из базы подписчиков — чтобы менеджер сразу нашёл клиента в 1С."""
    if not GH_TOKEN:
        return ""
    try:
        r = requests.get(f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}",
                         headers=_gh_headers(), timeout=15)
        if r.status_code != 200:
            return ""
        data = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
        return data.get(str(user_id), {}).get("phone", "")
    except Exception:
        return ""


def bridge_to_group(chat_id, user, text, message_id=None):
    """Сообщение клиента → рабочая группа. Метка #id — по ней вернётся ответ.

    Длинные диалоги менеджер ведёт из 1С по номеру (Телеграм Премиум) — здесь
    только первое касание для контроля. Телефон в карточке — ключ к клиенту в 1С.
    """
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    seg = "член клуба" if _segment(chat_id) == "member" else "новый"
    with LOCK:
        st = STATE.setdefault(chat_id, {})
        st.setdefault("dlg", []).append(("Клиент", text))
        st["dlg_ts"] = time.time()
    # автоархив: если разговор затих на 4 часа — сам уезжает в карточку 1С,
    # не дожидаясь кнопки «Завершить разговор»
    def _auto(ts_snapshot=STATE[chat_id]["dlg_ts"]):
        with LOCK:
            still = STATE.get(chat_id, {}).get("dlg_ts") == ts_snapshot
        if still:
            archive_dialog(chat_id)
    threading.Timer(14400, _auto).start()
    phone = lookup_phone(user.get("id"))
    pline = (f"<b>Телефон:</b> <code>{phone}</code> — продолжить диалог из 1С\n"
             if phone else "Телефон не оставлял — отвечайте реплаем здесь\n")
    send_to_orders(parse_mode="HTML",
        text=(f"💬 <b>СООБЩЕНИЕ ИЗ БОТА</b> ({seg})\n\n"
              f"<b>{who}</b> · {uname}\n"
              f"{pline}\n"
              f"{text}\n\n"
              f"#id{chat_id}\n"
              "Ответ реплаем на это сообщение уйдёт клиенту в бот."))
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
    with LOCK:
        STATE.setdefault(target, {}).setdefault("dlg", []).append(("Менеджер", text))
    api("setMessageReaction", chat_id=msg["chat"]["id"],
        message_id=msg["message_id"], reaction=[{"type": "emoji", "emoji": "👌"}])
    return True


def _segment(chat_id):
    with LOCK:
        return STATE.get(chat_id, {}).get("segment", "")


# ------------------------------------------------------------------- заявка

def send_to_1c(user, phone, goal, direction, source=""):
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
                    f"Начнёт с: {DIRS.get(direction, DIRS['any'])[0]}."
                    + (f" Источник: {source}." if source else "")),
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


def send_lead(user, phone, goal, direction, source=""):
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    now = datetime.now(MSK).strftime("%d.%m в %H:%M")
    src_line = f"<b>Источник:</b> {source}\n" if source else ""
    text = (
        "🎁 <b>ЗАЯВКА ИЗ TELEGRAM-БОТА</b>\n\n"
        f"<b>Имя:</b> {who}\n"
        f"<b>Телефон:</b> <code>{phone}</code>\n\n"
        f"<b>Подарок:</b> {GIFT}\n"
        f"<b>Задача:</b> {GOALS.get(goal, 'не указана')}\n"
        f"<b>Начнёт с:</b> {DIRS.get(direction, DIRS['any'])[0]}\n"
        f"{src_line}\n"
        f"Telegram: {uname} · {now}"
    )
    text += send_to_1c(user, phone, goal, direction, source)
    r = send_to_orders(text=text, parse_mode="HTML",
                        reply_markup=kb([[("Беру в работу", f"take:{user.get('id')}")]]))
    log_lead_event(user.get("id"), "lead_created")
    return (r.get("result") or {}).get("message_id")


# Частые вопросы: с сайта заходят с ними чаще, чем с готовностью оставить номер.
# Цены не называем — ведём на подарок и разговор с менеджером.
FAQ = [
    (("скольк", "цен", "стоим", "прайс", "абонемент", "почём", "почем"),
     "Зависит от формата и частоты — подберём под вас. "
     "А начать можно с подарочных 72 часов, они бесплатные."),
    (("бассейн", "плава", "аква"),
     "Бассейна у нас нет, клуб сухой — говорю честно. Если бассейн обязателен, мы не подойдём. "
     "Если нет — 2500 м², сауна и бойцовский клуб в одном месте."),
    (("адрес", "где вы", "как добра", "метро"),
     f"{CLUB}."),
    # Парковку охраняет бизнес-центр, и она положена не всем — гостю не обещаем.
    (("парков", "машин", "припарк"),
     "Парковка у бизнес-центра есть. Членам клуба она доступна не на всех абонементах — "
     "подскажем точно по вашему варианту."),
    # Круглосуточно — только для членов клуба; гостю время назовёт менеджер.
    (("график", "во сколь", "режим", "часы работ", "круглосут", "ночью работ"),
     "Для членов клуба вход круглосуточный, без выходных. По гостевому визиту время "
     "подберём вместе — когда вам удобнее прийти."),
    (("сауна", "полотенц", "душ", "раздевал"),
     "Финская сауна и ведро-водопад. Полотенца и вода без доплат."),
    (("бокс", "кикбокс", "единоборств", "бойцов", "борьб"),
     "Бойцовский клуб 500 м², бокс и кикбоксинг. Первая тренировка по боксу бесплатная."),
    (("тренер", "персональн", "инструктор"),
     "Персональные тренеры есть, подберём под вашу задачу."),
    (("групповы", "расписан", "йога", "пилатес", "аэроб"),
     "Три зала групповых программ, входят в абонемент. Расписание пришлём."),
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
        return step_dir(chat_id, mid, "", intro=True)

    if data == "seg:member":
        with LOCK:
            STATE.setdefault(chat_id, {})["segment"] = "member"
        save_subscriber(user, segment="member")
        return step_member_menu(chat_id, mid)

    if data == "bridge":
        return bridge_on(chat_id, mid, user)

    if data == "bridge_off":
        return bridge_off(chat_id, mid)

    if data == "member_phone":
        with LOCK:
            STATE.setdefault(chat_id, {})["member_phone"] = True
        api("deleteMessage", chat_id=chat_id, message_id=mid)
        return api("sendMessage", chat_id=chat_id,
                   text="Нажмите кнопку внизу — сохраню номер, чтобы присылать "
                        "только то, что касается вас.\n\n"
                        "Отправляя номер, вы соглашаетесь на обработку персональных данных.",
                   reply_markup=ASK_PHONE)

    if data == "go":
        return step_dir(chat_id, mid, "")

    if data == "combat":
        return step_combat(chat_id, mid)

    if data == "ask":
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="Спрашивайте. Отвечу сам, а если нужно подробнее — перезвоним.")

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

    if data.startswith("ack:"):
        with LOCK:
            STATE[data] = True
        who_took = cq["from"].get("first_name", "менеджер")
        old = cq["message"].get("text", "")
        api("editMessageText", chat_id=chat_id, message_id=mid,
            text=old + f"\n\n✅ Смотрит: {who_took}")
        return

    if data.startswith("take:"):
        who = cq["from"].get("first_name", "менеджер")
        old = cq["message"].get("text", "")
        api("editMessageText", chat_id=chat_id, message_id=mid,
            text=old + f"\n\n✅ В работе: {who}")
        log_lead_event(data.split(":", 1)[1], "taken", who)


def on_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()

    # В группах бот молчит — с одним исключением: реплай менеджера на
    # сообщение с меткой #id он доставляет клиенту.
    if msg["chat"].get("type") != "private":
        if (msg.get("text") or "").strip().startswith("/chat_id"):
            api("sendMessage", chat_id=chat_id, reply_to_message_id=msg["message_id"],
                text=f"Идентификатор этого чата: {chat_id}")
            return
        if msg.get("reply_to_message"):
            bridge_from_group(msg)
        return

    # Ответ менеджера реплаем на карточку с меткой #id. Обычно карточки живут
    # в рабочей группе, но при тестовом режиме ORDERS_CHAT — личный чат, и
    # реплаи должны работать и там.
    if str(chat_id) in (str(ORDERS_CHAT), str(BRIDGE_CHAT)) and msg.get("reply_to_message"):
        if bridge_from_group(msg):
            return

    if msg.get("contact"):
        phone = clean_phone(msg["contact"].get("phone_number"))
        return finish(chat_id, user, phone)

    if text.startswith("/start"):
        archive_dialog(chat_id)
        # deep-link с лендинга: «/start boks» → тема разговора с первого слова
        parts = text.split(maxsplit=1)
        theme = parts[1].strip().lower() if len(parts) > 1 else ""
        if theme not in LANDINGS:
            theme = None
        with LOCK:
            STATE.pop(chat_id, None)
            if theme:
                STATE[chat_id] = {"src": theme}
        save_subscriber(user)
        return step_gate(chat_id, user.get("first_name"), theme=theme)

    if text.startswith("/help"):
        return api("sendMessage", chat_id=chat_id,
                   text=f"Клуб «Сандов Фитнес», {CLUB}. Телефон {PHONE}.\n"
                        "Наберите /start — помогу и с первым визитом, и по клубу.")

    # мост открыт: если клиент не тапнул кнопку «Написать», а пишет прямо
    # здесь — в группу уходит только первое такое сообщение (для контроля).
    # Дальше не спамим группу репостами: переписка должна идти в клубный
    # Телеграм (1С, вкладка Мессенджер), туда и подталкиваем каждый раз.
    # Поправка Ольги 16.08.2026.
    with LOCK:
        st = STATE.setdefault(chat_id, {})
        in_bridge = st.get("bridge")
        already_nudged = st.get("bridge_fallback_notified")
    if in_bridge and text:
        if not already_nudged or not MANAGER_TG_URL:
            with LOCK:
                STATE.setdefault(chat_id, {})["bridge_fallback_notified"] = True
            return bridge_to_group(chat_id, user, text, msg.get("message_id"))
        with LOCK:
            st2 = STATE.setdefault(chat_id, {})
            st2.setdefault("dlg", []).append(("Клиент", text))
            st2["dlg_ts"] = time.time()
        markup = kb_mixed([[("✍️ Написать", "url:" + MANAGER_TG_URL)]])
        return api("sendMessage", chat_id=chat_id,
                   text="Мы уже на связи в клубном чате — продолжим там 🙂",
                   reply_markup=markup)

    # после заявки спросили, как обращаться — ловим ответ. Необязательный шаг:
    # что бы человек ни написал дальше, заявка уже ушла и ничего не теряется.
    with LOCK:
        st = STATE.get(chat_id, {})
        awaiting = st.get("await_name")
        lead_mid = st.get("lead_mid")
    if awaiting and text and not text.startswith("/") \
            and len(text) <= 40 and len(re.sub(r"\D", "", text)) < 6:
        name = re.sub(r"^(меня зовут|я|это)\s+", "", text, flags=re.I).strip(" .,!")
        with LOCK:
            STATE.get(chat_id, {}).pop("await_name", None)
        save_subscriber(user, call_name=name)
        note = f"✏️ Клиент просит обращаться: <b>{name}</b>"
        if lead_mid:
            send_to_orders(text=note, parse_mode="HTML", reply_to_message_id=lead_mid)
        else:
            send_to_orders(text=note, parse_mode="HTML")
        # сразу открываем дорогу в клубный Телеграм: пока клиент сам не написал
        # первым, номерной аккаунт 1С не может отправить ему ни слова
        # (приватность Телеграма) — 15.08 заявка Татьяны осталась без связи.
        # Тексты согласованы Ольгой 15.08: коротко, одно действие — кнопка
        # сама подсказывает, что написать. Одно напоминание через 45 минут.
        if MANAGER_TG_URL:
            btn = kb_mixed([[("Написать нам «Подарок»", "url:" + MANAGER_TG_URL)]])
            api("sendMessage", chat_id=chat_id,
                text=f"{name}, подарок закреплён 🎁 До него один шаг:",
                reply_markup=btn)

            def _bridge_nudge(cid=chat_id, n=name, b=btn):
                api("sendMessage", chat_id=cid,
                    text=f"{n}, подарок ждёт 🎁", reply_markup=b)
            threading.Timer(2700, _bridge_nudge).start()
            return None
        return api("sendMessage", chat_id=chat_id,
                   text=f"Приятно познакомиться, {name}! 🤝 До встречи в клубе.")

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
                       reply_markup=kb_mixed([[("💬 Написать менеджеру", "bridge")]]))
        return api("sendMessage", chat_id=chat_id,
                   text="Передать это менеджеру?",
                   reply_markup=kb_mixed([[("💬 Написать менеджеру", "bridge")],
                                    [("Показать меню", "seg:member")]]))

    answer = faq_answer(text)
    if answer:
        return api("sendMessage", chat_id=chat_id, text=answer + TAIL,
                   reply_markup=kb_mixed([[("Подобрать первые визиты", "go")],
                                    [("💬 Написать менеджеру", "bridge")]]))

    api("sendMessage", chat_id=chat_id,
        text="Подберу вам первые визиты — один вопрос. "
             "Или спросите словами, отвечу.",
        reply_markup=kb_mixed([[("Подобрать первые визиты", "go")],
                         [("💬 Написать менеджеру", "bridge")],
                         [("Я член клуба", "seg:member")]]))


def finish(chat_id, user, phone):
    if not phone:
        return api("sendMessage", chat_id=chat_id,
                   text="Не разобрал номер. Пришлите в формате +7 999 123-45-67.")

    # член клуба делится номером для связи — это не заявка: в 1С не шлём,
    # менеджеров не дёргаем, просто запоминаем в базе
    with LOCK:
        st = STATE.setdefault(chat_id, {})
        member_phone = st.pop("member_phone", False)
        want_bridge = st.pop("want_bridge_after_phone", False)
    if member_phone or _segment(chat_id) == "member":
        save_subscriber(user, segment="member", phone=phone)
        api("sendMessage", chat_id=chat_id, text="Сохранил 👍",
            reply_markup={"remove_keyboard": True})
        if want_bridge:
            return bridge_on(chat_id, user=user)
        return step_member_menu(chat_id, greet=False)

    with LOCK:
        st = STATE.get(chat_id, {})
    goal, direction = st.get("goal", "keep"), st.get("dir", "any")
    source = LANDINGS.get(st.get("src"), ("",))[0]
    if too_soon(user.get("id")):
        return api("sendMessage", chat_id=chat_id,
                   text="Ваш номер уже у нас — позвоним. "
                        f"Если срочно, наберите нас: {PHONE}",
                   reply_markup={"remove_keyboard": True})
    save_subscriber(user, segment="new", phone=phone)
    lead_mid = send_lead(user, phone, goal, direction, source)
    step_done(chat_id, user.get("first_name"))
    with LOCK:
        st = STATE.get(chat_id, {})
        seg = st.get("segment")
        STATE[chat_id] = {"segment": seg, "lead_mid": lead_mid}


# ── Заявка с сайта ──────────────────────────────────────────────────────────
# Кнопка в Телеграм у части людей просто не открывается: без ВПН страница
# бота не грузится (проверено 26.08). Для них — короткая форма прямо на
# странице: одно поле и кнопка. Заявка уходит туда же, куда заявки бота:
# в группу менеджеров и в 1С, тем же send_to_1c.

SITE_ORIGINS = ("https://lp.sandowfitness.ru", "https://sandowfitness.ru")
_SITE_LEADS = {}          # телефон -> время последней заявки, от дублей
_SITE_LOCK = threading.Lock()


def _cors(resp, origin):
    if origin in SITE_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/site-lead", methods=["POST", "OPTIONS"])
def site_lead():
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)), origin)

    data = request.get_json(silent=True) or request.form or {}
    # ловушка для роботов: поле спрятано от людей, заполняется только ботами
    if (data.get("company") or "").strip():
        return _cors(jsonify(ok=True), origin)

    raw_phone = (data.get("phone") or "").strip()
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) < 10:
        return _cors(jsonify(ok=False, error="phone"), origin), 400
    phone = "+7" + digits[-10:]

    # один и тот же номер дважды за десять минут — не тревожим менеджеров
    now = time.time()
    with _SITE_LOCK:
        last = _SITE_LEADS.get(phone, 0)
        _SITE_LEADS[phone] = now
    if now - last < 600:
        return _cors(jsonify(ok=True, repeat=True), origin)

    page = (data.get("page") or "").strip()[:120]
    name = (data.get("name") or "").strip()[:60]
    when = datetime.now(MSK).strftime("%d.%m в %H:%M")

    text = (
        "🌐 <b>ЗАЯВКА С САЙТА</b>\n\n"
        f"<b>Имя:</b> {name or 'не указано'}\n"
        f"<b>Телефон:</b> <code>{phone}</code>\n\n"
        f"<b>Подарок:</b> {GIFT}\n"
        f"<b>Страница:</b> {page or 'не указана'}\n\n"
        f"Форма на статье · {when}"
    )
    user = {"id": f"site-{digits[-10:]}", "first_name": name or "Гость с сайта"}
    text += send_to_1c(user, phone, "keep", "any", f"статья: {page}")
    send_to_orders(text=text, parse_mode="HTML")
    return _cors(jsonify(ok=True), origin)


# Метка версии: по ней видно, доехал ли новый код до сервера. Render
# иногда не пересобирает сервис, а без панели управления это не проверить.
VERSION = "2026-08-26-v8-site-lead-form"


@app.route("/health")
@app.route("/")
def health():
    return jsonify(ok=True, bot="sandow-lead-bot", leads=len(LAST_LEAD),
                   version=VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
