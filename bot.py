# -*- coding: utf-8 -*-
"""
Бот клуба «Сандов Фитнес» — единая точка входа с сайта.

Версия 2: бот различает две аудитории и ведёт их по-разному.

  • Новичок — как раньше: подарок «год в подарок», два вопроса, номер, заявка
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
from urllib.parse import quote as _url_quote

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

# Ольга 30.08.2026: уведомления и напоминания о открытом чате будили людей
# ночью. С 22:00 до 10:00 в группу заявок не шлём — откладываем до 10:00.
QUIET_FROM = 22
QUIET_TO = 10


def is_quiet(now=None):
    now = now or datetime.now(MSK)
    return now.hour >= QUIET_FROM or now.hour < QUIET_TO


def seconds_until_morning(now=None):
    now = now or datetime.now(MSK)
    morning = now.replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    if morning <= now:
        morning += timedelta(days=1)
    return max(1, (morning - now).total_seconds())


def send_or_defer(send_fn):
    """Отправляет сразу, а в тихие часы (22:00-10:00) — откладывает до 10:00."""
    if is_quiet():
        threading.Timer(seconds_until_morning(), send_fn).start()
    else:
        send_fn()

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

CLUB = "Нижегородская ул., 29–33, стр. 3"
PHONE = "+7 (495) 795-69-57"
GIFT = "год в подарок"
# Координатор тренажёрного зала — назначает фитнес-эксперта на запись.
# Ник, не chat_id: бот не в личке с ней, упоминание работает прямо в
# группе «Sandow заявки» (решение Ольги 28.09.2026).
COORDINATOR_TG = "@salikhovadaria"
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

# ВНИМАНИЕ: пользователю показывается только ПЕРВЫЙ элемент кортежа —
# название направления. Второй (описание) в диалоге нигде не выводится,
# он остался от прежней версии сценария и служит справкой при чтении кода.
# 26.09.2026 я вписала сюда строку про бесплатное первое занятие и чуть
# не выложила: правка выглядела сделанной, а человек её не увидел бы
# никогда. Нужно что-то сказать клиенту — говорить надо в step_phone,
# step_combat или FAQ, не здесь.
DIRS = {
    "gym": ("Тренажёрный зал",
            "Зал 1100 м²: свободные веса, тренажёры, помост для становой и приседа."),
    "shape": ("Снижение веса",
              "Кардиогалерея, тренажёрный зал 1100 м², сауна для восстановления."),
    "group": ("Групповые программы",
              "Три отдельных зала. Расписание пришлём — там видно, что идёт в ваше время."),
    "fight": ("Бокс и кикбоксинг",
              "Бойцовский клуб 500 м², ринг и мешки."),
    "any": ("Ещё не решил",
            "Это нормально — на визите покажем всё и подскажем, с чего начать."),
}

# Раздел 2.2 сценария (Тренер Хаб\БОТ-сценарий-реплик-26.09.md, утверждено
# 27.09): развёрнутый ответ-ценность сразу после выбора категории — то,
# чего не хватало в сокращённой версии 15.08. Показывается один раз, потом
# человек выбирает формат знакомства.
VALUE_TEXT = {
    "gym": ("Тренажёрный зал у нас 1100 м² — очередей к тренажёрам не бывает. "
            "В карту уже включены стартовые тренировки с личным тренером — "
            "по желанию: хотите — начните с них, хотите — тренируйтесь "
            "полностью самостоятельно, а тренера подключите, когда посчитаете нужным."),
    "shape": ("Для этой задачи у нас есть всё: кардиогалерея с проекторами, "
              "тренажёрный зал 1100 м² и сауна для восстановления. А если "
              "захотите поддержку — в карту уже включены стартовые тренировки "
              "с личным тренером: он соберёт программу под вашу цель."),
    "group": ("У нас три зала групповых программ: зумба, «здоровая спина», "
              "танцевальные и силовые классы — всё входит в карту. И стартовые "
              "тренировки с личным тренером тоже включены — воспользуетесь, "
              "если захотите: никто ничего не навязывает."),
    "fight": ("Бойцовский клуб у нас 500 м²: октагон, татами, семь мешков — "
              "для самостоятельных тренировок это входит в карту. Есть группы "
              "по кикбоксингу, ММА и грэпплингу. Приходите посмотреть — такого "
              "пространства нет больше нигде в районе."),
    "any": ("Тогда самое правильное — увидеть клуб своими глазами: 2500 м², "
            "круглосуточный режим, сауна. Экскурсия ни к чему не обязывает, "
            "займёт 20 минут."),
}

# Слоты для предварительной записи — координатор сверяет и подтверждает
# вручную в течение часа (раздел 2.5 сценария), поэтому реального календаря
# тренеров бот на старте не требует.
SLOTS_TRAINING = ["10:00", "12:00", "14:00", "16:00", "17:30", "19:30"]
SLOTS_TOUR = ["10:00", "12:00", "14:00", "17:00", "19:00", "21:00"]


# Тематические входы с лендингов: t.me/sandowclub_bot?start=<код>.
# Человек пришёл со страницы про конкретное направление — первое сообщение
# продолжает тему, а не начинает с нуля (раньше был шов: лендинг про бокс,
# а бот сразу про подарок). Дальше сценарий обычный, без изменений.
# Код темы уходит менеджеру строкой «Источник» в заявке.
# ММА в текстах нет намеренно: в клубе только бокс и кикбоксинг.
LANDINGS = {
    "boks": ("лендинг Бокс",
             "Интересует бокс? Отличный выбор — в «Сандов Фитнес» первое занятие "
             "боксом бесплатное. А сейчас у нас год в подарок."),
    "kik": ("лендинг Кикбоксинг",
            "Интересует кикбоксинг? В «Сандов Фитнес» он есть и в группах по "
            "расписанию, и персонально с тренером. А сейчас у нас год в подарок."),
    "zal": ("лендинг Тренажёрный зал",
            "Ищете тренажёрный зал? В «Сандов Фитнес» он занимает 1100 м²: свободные "
            "веса, тренажёры по группам мышц, помост для становой и приседа. "
            "И сейчас у нас год в подарок."),
    "noch": ("лендинг Круглосуточный фитнес",
             "Удобнее тренироваться поздно вечером или рано утром? «Сандов Фитнес» "
             "открыт для членов клуба круглосуточно, без выходных. А знакомство "
             "начните с подарка — сейчас у нас год в подарок."),
    "boec": ("лендинг Бойцовский клуб",
             "Интересуют единоборства? Бойцовский клуб «Сандов Фитнес» — 500 м²: "
             "бокс и кикбоксинг, ринг и мешки, персональные тренировки. Первое "
             "занятие боксом бесплатное, и сейчас у нас год в подарок."),
}

# Входы с печатных макетов ВНУТРИ клуба: t.me/sandowclub_bot?start=<код>.
# Отличие от LANDINGS принципиальное: там человек с сайта, он ещё не наш —
# ему положен подарок и вопрос «хотите в клуб?». Здесь код отсканирован в
# раздевалке или у турникета, то есть человек уже член клуба. Спрашивать
# его «Хочу в клуб / Уже занимаюсь» — глупо, поэтому ведём сразу в меню.
#
# Зачем коды вообще: в боте было 30 контактов, потому что Телеграм
# запрещает боту писать первым. Пока человек сам не нажал «Старт», до него
# не дойдёт ни напоминание об окончании абонемента, ни расписание. Макеты
# в клубе — способ получить это первое касание, а разные коды показывают,
# какой носитель сработал.
КЛУБНЫЕ_ВХОДЫ = {
    "razdevalka": "макет в раздевалке",
    "turniket": "макет у турникета",
    "klub": "макет в клубе",
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

# Счётчик принятых заявок по дням. Нужен для сквозной сверки: сторож
# сравнивает, сколько заявок лежит в Тильде за вчера и сколько из них
# реально доехало до нас. Расхождение «в Тильде есть, у нас ноль» —
# единственный признак, который заметил бы блокировку приёмника
# 15.09.2026, когда все прочие проверки показывали «чисто» и 16 человек
# остались без звонка.
СЧЁТЧИК_ЗАЯВОК = "data/site_leads_by_day.json"


def учесть_заявку() -> None:
    """Отмечает в общем журнале, что заявка с сайта до нас доехала."""
    def _работа():
        try:
            день = datetime.now(MSK).strftime("%Y-%m-%d")
            данные = gh_read_json(СЧЁТЧИК_ЗАЯВОК, default={}) or {}
            данные[день] = int(данные.get(день, 0)) + 1
            # держим только последний месяц, файл не должен расти вечно
            за_месяц = dict(sorted(данные.items())[-31:])
            gh_write_json(СЧЁТЧИК_ЗАЯВОК, за_месяц,
                          f"заявка с сайта {день}")
        except Exception as exc:  # noqa: BLE001
            print(f"[счётчик] не записан: {str(exc)[:120]}", flush=True)

    threading.Thread(target=_работа, daemon=True).start()


def log_lead_event(user_id, event, who=None, phone=None):
    """Пишет момент создания заявки и момент «Беру в работу» — источник для
    метрики SLA (норматив 3 минуты, по разбору с коучем 18.08.2026). Раньше
    такого лога не было нигде: ни у Тильды, ни у бота, поэтому текущее время
    ответа не с чем было сверить.

    Телефон в записи нужен для сверки с журналом звонков АТС: «Беру в
    работу» — это клик в чате, а не факт звонка, поэтому настоящую скорость
    первого контакта считает отдельный отчёт по совпадению номера."""
    if not GH_TOKEN:
        return
    threading.Thread(target=_log_lead_event, args=(str(user_id), event, who, phone), daemon=True).start()


def _log_lead_event(user_id, event, who, phone=None):
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
        if phone:
            rec["phone"] = phone
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


def gh_read_json(path, default=None):
    """Чтение произвольного JSON из хранилища. Нужен модулю распределения:
    у него своя история, но незачем заводить второй способ ходить в GitHub."""
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        if r.status_code != 200:
            return default
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    except Exception as exc:
        print(f"[gh] чтение {path}: {exc}", flush=True)
        return default


def gh_write_json(path, data, message):
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {"message": message,
                "content": base64.b64encode(
                    json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()}
        if sha:
            body["sha"] = sha
        w = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
        if w.status_code not in (200, 201):
            print(f"[gh] запись {path}: {w.status_code} {w.text[:120]}", flush=True)
    except Exception as exc:
        print(f"[gh] запись {path}: {exc}", flush=True)


_COORD_CHAT = {"id": None, "ts": 0}


def coordinator_chat_id():
    """chat_id координатора зала по её нику (COORDINATOR_TG), если она хоть
    раз нажимала /start у бота — тогда она есть в базе подписчиков
    (data/tg_subscribers.json, GH_PATH) с полем username. Без этого бот
    физически не может написать ей лично — Telegram запрещает боту первым
    писать тому, кто не начинал диалог. Пока она не жала /start —
    возвращаем None, упоминание остаётся только в группе (как раньше).
    Правка Ольги 28.09: живая проверка показала, что одного упоминания
    в группе мало — надёжнее личное сообщение."""
    if time.time() - _COORD_CHAT["ts"] < 300:
        return _COORD_CHAT["id"]
    uname = COORDINATOR_TG.lstrip("@").lower()
    data = gh_read_json(GH_PATH, default={}) or {}
    found = None
    for chat_id, rec in data.items():
        if (rec.get("username") or "").lower() == uname:
            found = int(chat_id)
            break
    _COORD_CHAT["id"] = found
    _COORD_CHAT["ts"] = time.time()
    return found


_ACTIVE_MGR = {"name": None, "ts": 0}


def active_manager_name():
    """Кто сейчас «активный» менеджер по графику ОП (data/op_active_manager.json,
    считает workflow «График ОП» в sandow-lp раз в 20 минут — читаем готовое,
    сам бот с Google Таблицей не работает). Кэш 5 минут, чтобы не дёргать
    GitHub на каждое уведомление. Пусто/ошибка — тихо возвращаем None,
    уведомление в группу тогда идёт без имени, как раньше (не ломаем поток)."""
    if time.time() - _ACTIVE_MGR["ts"] < 300:
        return _ACTIVE_MGR["name"]
    data = gh_read_json("data/op_active_manager.json", default=None)
    name = (data or {}).get("active_manager")
    _ACTIVE_MGR["name"] = name
    _ACTIVE_MGR["ts"] = time.time()
    return name


def save_subscriber(user, segment=None, phone=None, call_name=None, source=None):
    """Дописывает или обновляет запись о человеке в базе подписчиков.

    Работает в отдельном потоке: GitHub отвечает не мгновенно, а Telegram
    ждёт ответ вебхука. Ошибка записи не должна ломать диалог.
    """
    if not GH_TOKEN:
        return
    threading.Thread(target=_save_subscriber,
                     args=(dict(user), segment, phone, call_name, source),
                     daemon=True).start()


def _save_subscriber(user, segment, phone, call_name=None, source=None):
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
        if source:
            # Первый источник не перезатираем: важно, что именно привело
            # человека в бот, а не последняя ссылка, по которой он зашёл.
            # Раньше метка жила только в оперативном STATE и умирала при
            # перезапуске сервиса — посчитать носители было нечем.
            rec.setdefault("source", source)
            rec["source_last"] = source
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
            [("Связаться с менеджером", "bridge")],
        ]))


def step_hello(chat_id, message_id=None):
    """НЕ ВЫЗЫВАЕТСЯ. Экран остался от сценария до сокращения 15.08.2026.

    Путь живого человека сейчас: /start → step_gate → «Хочу в клуб» →
    step_dir. Эта функция не вызывается ниоткуда (проверено 26.09.2026),
    поэтому её кнопки «Интересуют единоборства» и «У меня вопрос»
    пользователю не показываются, а step_combat недостижим вовсе.

    Не удаляю: экран может понадобиться, если решим вернуть оффер на
    вход. Но и не считаю рабочим — правки здесь до клиента не доходят.
    """
    text = (
        "<b>Сейчас у нас год в подарок.</b> «Сандов Фитнес» — это 2500 м²: тренажёрный "
        "зал 1100 м², три зала групповых программ, бойцовский клуб 500 м² и "
        "финская сауна. Клуб работает круглосуточно.\n\n"
        "Лучше один раз увидеть: покажем клуб, ответим на вопросы и подберём "
        "формат под вашу цель. Менеджер перезвонит и договорится об удобном времени."
    )
    markup = kb([
        [("Хочу попробовать", "go")],
        [("Интересуют единоборства", "combat")],
        [("У меня вопрос", "ask")],
    ])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="HTML", reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)


def step_member_menu(chat_id, message_id=None, greet=True):
    """Меню действующего члена клуба. Никаких заявок и продаж — только польза.

    Раздел 4 сценария (26.09, утверждено 27.09): заморозка, справка для
    вычета, срок абонемента, запись на тренировку — теперь прямо в боте,
    а не ссылкой (решение Ольги 27.09 «заморозка только через бот»)."""
    text = "Рад видеть своих! Чем помочь?" if greet else "Чем ещё помочь?"
    markup = kb_mixed([
        [("📅 Расписание групповых программ", "url:" + schedule_url())],
        [("❄️ Заморозка абонемента", "freeze")],
        [("🧾 Справка для налогового вычета", "tax_cert")],
        [("📆 Срок моего абонемента", "member_term")],
        [("🏋️ Записаться на тренировку", "book_training")],
        [("💬 Написать менеджеру", "bridge")],
    ])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def member_card_line(user):
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    phone = lookup_phone(user.get("id"))
    pline = f" · <code>{phone}</code>" if phone else " · телефон не оставлял"
    return f"<b>{who}</b> · {uname}{pline}"


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
    # Раздел 2.1 сценария (26.09, утверждено 27.09): вопрос о задаче
    # возвращён — но не как раньше (менеджер выясняет по звонку), а с
    # немедленным ответом-ценностью (step_value) сразу по выбору, до
    # телефона. Категория «Снижение веса» добавлена — её не было в версии
    # 15.08. «goal» больше не используется предметно, оставлен параметром
    # только чтобы не ломать редкие внешние вызовы (deep-link «combat»).
    text = ("Отлично! Что вам ближе? Под вашу задачу подберём тренера и программу."
            if intro else "Что вам ближе?")
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=text, reply_markup=kb([
            [("Тренажёрный зал", f"d:gym:{goal}")],
            [("Снижение веса", f"d:shape:{goal}")],
            [("Групповые программы", f"d:group:{goal}")],
            [("Единоборства", f"d:fight:{goal}")],
            [("Пока просто смотрю", f"d:any:{goal}")],
        ]))


def step_value(chat_id, message_id, direction):
    """Раздел 2.2: ответ-ценность по выбранной категории, затем выбор формата
    знакомства. Приоритет — тренировка с тренером, её кнопка всегда первая
    (концепция клуба: каждый новый человек проходит через тренера)."""
    with LOCK:
        STATE.setdefault(chat_id, {})["dir"] = direction
    text = VALUE_TEXT.get(direction, VALUE_TEXT["any"])
    tail = ("\n\nЛучший способ познакомиться с клубом — первая тренировка "
            "с тренером, она в подарок. Или можно просто прийти посмотреть "
            "клуб. Как вам удобнее?")
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=text + tail, reply_markup=kb([
            [("Тренировка с тренером — в подарок", f"fmt:t:{direction}")],
            [("Посмотреть клуб", f"fmt:v:{direction}")],
        ]))


def step_phone(chat_id, message_id, goal, direction, fmt="training"):
    # Вопрос квиза удаляем: в чате остаётся ровно ОДНО сообщение — финал.
    # Первая строка — тёплый отклик на выбор: человек видит, что его услышали.
    warm = {
        "gym": "Отличный выбор — железо не врёт 💪",
        "group": "Отличный выбор — групповые программы втягивают быстрее всего 🔥",
        "fight": "Уважаем — бокс закаляет 🥊",
        "any": "И правильно — попробуете всё и поймёте, что ваше 👍",
    }.get(direction, "Отличный план 👍")
    with LOCK:
        STATE.setdefault(chat_id, {})["fmt"] = fmt
    api("deleteMessage", chat_id=chat_id, message_id=message_id)
    # Раздел 2.3: контакт берём сразу после выбора формата — страховка от
    # потери лида. Текст различается для тренировки и экскурсии (2.3а/2.3б),
    # но само поле телефона обязательное в обоих случаях (решение Ольги 27.09).
    lead_in = (f"{warm}\n\n🎁 Первая тренировка с тренером — в подарок.\n\n"
               if fmt == "training" else f"{warm}\n\n")
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(lead_in +
              "Оставьте, пожалуйста, имя и номер телефона — закреплю за вами "
              "время, и продолжим.\n\n"
              "Отправляя номер, вы соглашаетесь на обработку персональных данных."),
        reply_markup=ASK_PHONE)


def schedule_dropoff_watch(chat_id, delay=900):
    """Правка Ольги 28.09: клиент, который оставил телефон и продолжает
    отвечать боту (здоровье, время), звонка не ждёт — уведомлять менеджеров
    сразу значит заставлять их звонить тем, кто и не рассчитывает на звонок.
    Через 15 минут тишины, если запись так и не завершена (нет
    booking_mid), — это и есть признак «отвалился»: тогда телефон уходит
    в рабочий чат, но с явной пометкой, что это не завершённая запись,
    а человек, переставший отвечать."""
    def _check():
        with LOCK:
            st = STATE.get(chat_id, {})
            if st.get("booking_mid") or st.get("dropoff_alerted") or not st.get("phone"):
                return
            st["dropoff_alerted"] = True
            phone, name = st.get("phone"), st.get("client_name", "")
            direction, fmt = st.get("dir", "any"), st.get("fmt", "training")
        kind = "тренировка с тренером" if fmt == "training" else "экскурсия по клубу"
        active = active_manager_name()
        who_line = f"\n{active} — вы сейчас активный менеджер, перезвоните:" if active \
            else "\n15 минут не отвечает боту дальше — похоже, отвлёкся. Перезвоните."

        def _send():
            send_to_orders(parse_mode="HTML",
                text=(f"📵 <b>ОСТАВИЛ НОМЕР, ЗАПИСЬ НЕ ЗАВЕРШИЛ</b>\n"
                      f"<b>Имя:</b> {name or 'без имени'}\n"
                      f"<b>Телефон:</b> <code>{phone}</code>\n"
                      f"<b>Хотел:</b> {kind}, {DIRS.get(direction, DIRS['any'])[0]}\n"
                      f"{who_line}"),
                reply_markup=kb([[("Беру в работу", f"take:{chat_id}")]]))
        # Тихие часы (22:00–10:00) — как и другие отложенные уведомления
        # бота (bridge_on): не будим менеджеров ночью из-за молчания клиента.
        send_or_defer(_send)
    threading.Timer(delay, _check).start()


def ask_health(chat_id, name):
    """Раздел 2.3а: необязательный вопрос о здоровье, ПОСЛЕ телефона, только
    для формата «тренировка». Ответ ни на что не влияет, просто передаётся
    фитнес-эксперту и менеджеру (решение Ольги 27.09 — писать в 1С полностью)."""
    with LOCK:
        STATE.setdefault(chat_id, {})["await_health"] = True
    hi = f"Спасибо, {name}!" if name else "Спасибо!"
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"{hi} Чтобы фитнес-эксперт подготовился к встрече: есть ли "
              "особенности здоровья, о которых ему важно знать — спина, "
              "суставы, давление? Если нет — просто напишите «нет»."))


# Правка Ольги 28.09 (живая проверка): «будни/выходные» слишком расплывчато —
# нужен конкретный день недели, не диапазон. Порядок словаря = порядок кнопок.
WEEKDAYS = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда",
    "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}
# Винительный падеж с предлогом — «в среду», «во вторник» — простое
# правило «добавить у» ломается на «среда»/«суббота» (не «средуу»),
# поэтому формы просто выписаны, без грамматических хитростей.
WEEKDAYS_ACC = {
    "mon": "в понедельник", "tue": "во вторник", "wed": "в среду",
    "thu": "в четверг", "fri": "в пятницу", "sat": "в субботу", "sun": "в воскресенье",
}


def step_time(chat_id, fmt, message_id=None):
    text = "Когда вам удобнее? Выберите день:"
    markup = kb([
        [("Пн", f"tp:mon:{fmt}"), ("Вт", f"tp:tue:{fmt}"), ("Ср", f"tp:wed:{fmt}"),
         ("Чт", f"tp:thu:{fmt}")],
        [("Пт", f"tp:fri:{fmt}"), ("Сб", f"tp:sat:{fmt}"), ("Вс", f"tp:sun:{fmt}")],
    ])
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    else:
        api("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def step_slots(chat_id, message_id, period, fmt):
    slots = SLOTS_TRAINING if fmt == "training" else SLOTS_TOUR
    rows = [[(t, f"sl:{fmt}:{period}:{t}")] for t in slots]
    rows.append([("Своё время", f"sl:{fmt}:{period}:own")])
    label = WEEKDAYS_ACC.get(period, period)
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"Выберите время {label}:", reply_markup=kb(rows))


def push_1c_followup(phone, extra_comment):
    """Доклейка деталей (здоровье, время) второй заявкой по тому же телефону —
    тем же способом, каким archive_dialog доклеивает переписку (1С сама
    привязывает по номеру, отдельный вебхук на «обновление» недоступен)."""
    if not (ONEC_WEBHOOK and phone):
        return
    try:
        requests.post(ONEC_WEBHOOK, timeout=25, data={
            "Name": "Уточнение к заявке из Telegram-бота",
            "Phone": re.sub(r"\D", "", phone),
            "Comment": extra_comment,
            "source": ONEC_SOURCE,
            "formname": "Telegram-бот сайта — уточнение",
            "formid": "sandow_lead_bot_followup",
            "tranid": f"tg-followup-{phone}-{int(time.time())}",
        })
    except Exception as exc:
        print(f"[1c-followup] {exc}", flush=True)


def finalize_booking(chat_id, message_id, user, st):
    fmt = st.get("fmt", "training")
    direction = st.get("dir", "any")
    phone = st.get("phone") or lookup_phone(user.get("id"))
    health = st.get("health", "")
    time_pref = st.get("time_pref", "не указано")
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    who_client = user.get("first_name") or ""

    # Правка Ольги 28.09 (живая проверка бота): ночью «подтвержу в течение
    # часа» — невыполнимое обещание, координатор и менеджеры не работают
    # 24/7. Те же тихие часы, что уже действуют для уведомлений (QUIET_FROM/
    # QUIET_TO, 22:00–10:00).
    when = "в течение часа" if not is_quiet() else "в ближайшие рабочие часы"
    if fmt == "training":
        client_text = (
            f"Принято{', ' + who_client if who_client else ''}! Предварительно — {time_pref}. "
            f"Сверю время с расписанием фитнес-эксперта и подтвержу здесь же {when}. "
            "Если окно окажется занято — предложу ближайшее соседнее. До скорой связи!")
    else:
        client_text = (f"Готово{', ' + who_client if who_client else ''}! "
                        f"Ждём вас — {time_pref}. Подтвержу здесь же {when}. До скорой связи!")
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id, text=client_text)
    else:
        api("sendMessage", chat_id=chat_id, text=client_text)

    kind = "Тренировка с тренером" if fmt == "training" else "Экскурсия"
    lines = [
        f"📅 <b>ЗАПИСЬ: {kind.upper()}</b>",
        f"<b>Имя:</b> {name}",
        f"<b>Телефон:</b> <code>{phone or 'не оставлял'}</code>",
        f"<b>Направление:</b> {DIRS.get(direction, DIRS['any'])[0]}",
        f"<b>Время:</b> {time_pref}",
    ]
    if health:
        lines.append(f"<b>Особенности здоровья:</b> {health}")
    # Тренировку с тренером назначает координатор зала — упоминаем её по
    # нику прямо в карточке (решение Ольги 28.09: проще прямого упоминания
    # в группе, чем городить отдельную личную рассылку через бота).
    if fmt == "training":
        lines.append(f"\n{COORDINATOR_TG} — подтвердите время клиенту одним нажатием:")
    else:
        active = active_manager_name()
        who = f"{active} — вы активный менеджер сейчас, подтвердите" if active else "Менеджер — подтвердите"
        lines.append(f"\n{who} время клиенту одним нажатием:")
    r = send_to_orders(parse_mode="HTML", text="\n".join(lines),
                        reply_markup=kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]]))
    booking_mid = (r.get("result") or {}).get("message_id")
    if fmt == "training":
        coord_id = coordinator_chat_id()
        if coord_id:
            api("sendMessage", chat_id=coord_id, parse_mode="HTML",
                text="\n".join(lines[:-1]) + "\n\nПодтвердите время клиенту одним нажатием:",
                reply_markup=kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]]))

    with LOCK:
        STATE[chat_id] = {
            "segment": "new", "dir": direction, "fmt": fmt, "phone": phone,
            "time_pref": time_pref, "health": health,
            "booking_mid": booking_mid, "client_name": who_client,
        }
    extra = (f"Формат: {kind}. Время: {time_pref}."
             + (f" Особенности здоровья: {health}." if health else ""))
    push_1c_followup(phone, extra)


def finalize_member_training(chat_id, message_id, user, st):
    """Раздел 4 «Записаться на тренировку» (член клуба) — правка Ольги 28.09:
    раньше сразу уходило координатору без вопроса о времени, теперь так же,
    как у новых клиентов, сначала день и время. Вторая правка (тот же день):
    реплай-мост убран — раз время уже известно, координатор подтверждает
    одной кнопкой (confirmvisit/coordinator_confirm), тем же способом, что
    и у новых клиентов; отдельно отвечать реплаем незачем."""
    already = st.get("member_training_already", False)
    time_pref = st.get("time_pref", "не указано")
    who_client = user.get("first_name") or ""
    note = (f"Принято{', ' + who_client if who_client else ''}! {time_pref} — "
            "согласую с вашим экспертом и подтвержу здесь же."
            if already else
            f"Хорошая новость{', ' + who_client if who_client else ''}: первая "
            f"тренировка с фитнес-экспертом — в подарок, {time_pref}. Подберу "
            "эксперта под вашу задачу и подтвержу здесь же.")
    if message_id:
        api("editMessageText", chat_id=chat_id, message_id=message_id, text=note)
    else:
        api("sendMessage", chat_id=chat_id, text=note)

    label = "уже занимается с экспертом" if already else "НИКОГДА не занимался — вводная ПТ в подарок"
    card = (f"🏋️ <b>ЗАПИСЬ НА ТРЕНИРОВКУ (член клуба)</b>\n{member_card_line(user)}\n"
            f"Статус: {label}.\n<b>Желаемое время:</b> {time_pref}.")
    r = send_to_orders(parse_mode="HTML",
        text=f"{card}\n\n{COORDINATOR_TG} — подтвердите время клиенту одним нажатием:",
        reply_markup=kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]]))
    booking_mid = (r.get("result") or {}).get("message_id")
    coord_id = coordinator_chat_id()
    if coord_id:
        api("sendMessage", chat_id=coord_id, parse_mode="HTML",
            text=f"{card}\n\nПодтвердите время клиенту одним нажатием:",
            reply_markup=kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]]))
    with LOCK:
        STATE[chat_id] = {
            "segment": "member", "fmt": "training", "time_pref": time_pref,
            "booking_mid": booking_mid, "client_name": who_client,
        }


def slot_chosen(chat_id, message_id, user, fmt, period, hhmm):
    if hhmm == "own":
        with LOCK:
            STATE.setdefault(chat_id, {})["await_own_time"] = True
        return api("editMessageText", chat_id=chat_id, message_id=message_id,
                   text="Напишите, пожалуйста, день и время словами — это нужно, "
                        "чтобы забронировать для вас удобное окно.")
    day_label = WEEKDAYS.get(period, period)
    with LOCK:
        st = STATE.setdefault(chat_id, {})
        st["time_pref"] = f"{day_label}, {hhmm}"
        member_flow = "member_training_already" in st
        snapshot = dict(st)
    if member_flow:
        return finalize_member_training(chat_id, message_id, user, snapshot)
    finalize_booking(chat_id, message_id, user, snapshot)


def coordinator_confirm(group_chat_id, message_id, target_chat_id, who):
    """Координатор/менеджер нажал «Подтвердить время» в группе — клиенту
    уходит финальное подтверждение (раздел 2.5), адрес — ссылкой на карту
    (геометки и фото входа на старте нет: нет готового файла и координат —
    честно заменено ссылкой, не выдумано)."""
    with LOCK:
        st = STATE.get(target_chat_id, {})
        fmt = st.get("fmt", "training")
        time_pref = st.get("time_pref", "")
        name = st.get("client_name", "")
        is_member = st.get("segment") == "member"
    maps_url = "https://yandex.ru/maps/?text=" + _url_quote(f"Москва, {CLUB}")
    hi = f"Подтверждаю, {name}" if name else "Подтверждаю"
    if is_member:
        # Действующий член клуба — у неё уже есть браслет и доступ, паспорт
        # и адрес не нужны (это только для гостя, правка 28.09).
        text = (f"{hi}: {time_pref}. Фитнес-эксперт уже знает о встрече, "
                "ждём вас! До скорой связи!")
    elif fmt == "training":
        text = (f"{hi}: {time_pref}. Возьмите спортивную форму, кроссовки и "
                f"паспорт — он нужен для оформления гостевого визита.\n"
                f"Адрес: Москва, {CLUB}. Маршрут: {maps_url}\n"
                "Вас встретит менеджер и познакомит с фитнес-экспертом. Накануне напомню!")
    else:
        text = (f"{hi}: {time_pref}. Возьмите с собой паспорт — он нужен для "
                f"оформления визита.\nАдрес: Москва, {CLUB}. Маршрут: {maps_url}\n"
                "Вас встретит менеджер. Накануне напомню. До встречи!")
    api("sendMessage", chat_id=target_chat_id, text=text)
    who_name = who.get("first_name", "менеджер")
    api("editMessageReplyMarkup", chat_id=group_chat_id, message_id=message_id,
        reply_markup=kb([[(f"✅ Подтверждено: {who_name}", "noop")]]))


def step_done(chat_id, name):
    # НЕ ВЫЗЫВАЕТСЯ с 28.09.2026: путь «новый клиент» теперь идёт через
    # ask_health → step_time → step_slots → finalize_booking (сценарий
    # координатора, БОТ-сценарий-реплик-26.09.md), а finalize_booking сама
    # шлёт финальное сообщение клиенту. Функция оставлена как справка и на
    # случай отката, как раньше держали step_hello.
    #
    # Сокращение 15.08: вопрос «как обращаться?» убран (имя берём из Телеграма,
    # остальное менеджер уточнит в звонке) — сразу мост в клубный Телеграм.
    # Тексты моста согласованы Ольгой: коротко, кнопка сама говорит, что делать.
    hi = f"Готово, {name}!" if name else "Готово!"
    if MANAGER_TG_URL:
        btn = kb_mixed([[("Написать нам «Подарок»", "url:" + MANAGER_TG_URL)]])
        api("sendMessage", chat_id=chat_id, parse_mode="HTML",
            # «Закреплён» было бы обещанием подарка как уже полученного:
            # у годового подарка есть условия, и бот их не выполняет.
            text=f"{hi} Заявка принята 🎁 Остался один шаг:",
            reply_markup=btn)

        def _nudge(cid=chat_id, b=btn):
            api("sendMessage", chat_id=cid, text="Подарок ждёт 🎁", reply_markup=b)
        threading.Timer(2700, _nudge).start()
        return
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"{hi} Заявка принята — менеджер свяжется с вами, ответит на "
              f"вопросы и договорится о визите.\n\n"
              f"{CLUB}\n"
              f"Телефон клуба: {PHONE}"),
        reply_markup={"remove_keyboard": True})


def step_combat(chat_id, message_id=None):
    # одно сообщение, подарок первым — как и в основном финале
    with LOCK:
        STATE.setdefault(chat_id, {}).update({"goal": "stress", "dir": "fight"})
    api("sendMessage", chat_id=chat_id, parse_mode="HTML",
        # Раньше здесь было «Первая тренировка по боксу — бесплатная».
        # Сужение: по уточнению Ольги 26.09 бесплатно первое занятие в
        # любом направлении, а эта ветка открыта любому новичку из
        # главного меню, не только пришедшему со страницы про бокс.
        text=("🎁 <b>Первое занятие — бесплатное</b>. И сейчас "
              "у нас год в подарок.\n\n"
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

        def _send_open_notice():
            send_to_orders(parse_mode="HTML",
                text=(f"💬 <b>Открыл чат клуба</b> ({seg})\n"
                      f"<b>{who}</b> · {uname}{pline}\n"
                      "Переписка — в 1С, вкладка Телеграм. Кто смотрит — нажмите «Беру»."),
                reply_markup=kb([[("✅ Беру", ack_key)]]))
        send_or_defer(_send_open_notice)

        def _remind(round_no, w=who, p=pline, k=ack_key):
            with LOCK:
                if STATE.get(k):
                    return              # уже взяли в работу

            def _do_send():
                mark = "⏰" if round_no == 1 else "⚠️ ВТОРОЕ НАПОМИНАНИЕ"
                send_to_orders(parse_mode="HTML",
                    text=(f"{mark} <b>Проверьте вкладку Телеграм в 1С</b> — "
                          f"чат открывал: <b>{w}</b>{p}"),
                    reply_markup=kb([[("✅ Беру", k)]]))
                if round_no == 1:
                    threading.Timer(1800, _remind, args=(2,)).start()
            send_or_defer(_do_send)
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

def send_to_1c(user, phone, goal, direction, source="", метки=None, fmt=""):
    """Заводит заявку в 1С:Фитнес клуб. Возвращает приписку к сообщению в группе.

    Данные уходят формой — тем же способом, каким шлёт Тильда. JSON приёмник
    не принимает. Ошибку не глотаем: менеджер должен знать, что записи в 1С нет.
    """
    if not ONEC_WEBHOOK:
        return ""

    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x).strip()
    fmt_line = {"training": " Формат: тренировка с тренером (в подарок).",
                "tour": " Формат: экскурсия по клубу."}.get(fmt, "")
    data = {
        "Name": name or "Без имени",
        "Phone": re.sub(r"\D", "", phone or ""),
        "Comment": (f"Заявка из Telegram-бота. Подарок: {GIFT}. "
                    f"Задача: {GOALS.get(goal, 'не указана')}. "
                    f"Начнёт с: {DIRS.get(direction, DIRS['any'])[0]}."
                    + fmt_line
                    + (f" Источник: {source}." if source else "")),
        "source": ONEC_SOURCE,
        "utm_source": "telegram",
        "utm_medium": "bot",
        # Метка кампании шла с прежнего оффера «72 часа» — в отчётах
        # заявки по «Году в подарок» падали бы в чужую строку.
        "utm_campaign": "god-v-podarok",
        "formname": "Telegram-бот сайта",
        "formid": "sandow_lead_bot",
        "tranid": f"tg-{user.get('id')}-{int(time.time())}",
    }
    # Заявка могла прийти не из бота, а с формы сайта — тогда у неё свои
    # метки источника (yandex/maps и прочее). Подставлять им «telegram /
    # bot» нельзя: в 1С и в отчётах канал станет враньём, и по ним потом
    # считают, откуда приходят люди. Поэтому вызывающий передаёт метки,
    # и они перекрывают умолчания.
    if метки:
        data.update({k: v for k, v in метки.items() if v})
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


def send_lead(user, phone, goal, direction, source="", fmt=""):
    who = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "без имени"
    uname = f"@{user['username']}" if user.get("username") else "без ника"
    now = datetime.now(MSK).strftime("%d.%m в %H:%M")
    src_line = f"<b>Источник:</b> {source}\n" if source else ""
    fmt_label = {"training": "Тренировка с тренером (в подарок)",
                 "tour": "Экскурсия по клубу"}.get(fmt, "")
    fmt_line = f"<b>Формат:</b> {fmt_label}\n" if fmt_label else ""
    text = (
        "🎁 <b>ЗАЯВКА ИЗ TELEGRAM-БОТА</b>\n\n"
        f"<b>Имя:</b> {who}\n"
        f"<b>Телефон:</b> <code>{phone}</code>\n\n"
        f"<b>Подарок:</b> {GIFT}\n"
        f"<b>Задача:</b> {GOALS.get(goal, 'не указана')}\n"
        f"<b>Начнёт с:</b> {DIRS.get(direction, DIRS['any'])[0]}\n"
        f"{fmt_line}"
        f"{src_line}\n"
        f"Telegram: {uname} · {now}\n"
        "Здоровье и время визита придут отдельным уточнением, как только "
        "клиент ответит."
    )
    text += send_to_1c(user, phone, goal, direction, source, fmt=fmt)
    r = send_to_orders(text=text, parse_mode="HTML",
                        reply_markup=kb([[("Беру в работу", f"take:{user.get('id')}")]]))
    log_lead_event(user.get("id"), "lead_created", phone=phone)
    return (r.get("result") or {}).get("message_id")


# Частые вопросы: с сайта заходят с ними чаще, чем с готовностью оставить номер.
# Цены не называем — ведём на подарок и разговор с менеджером.
FAQ = [
    (("скольк", "цен", "стоим", "прайс", "абонемент", "почём", "почем"),
     "Зависит от формата и частоты — подберём под вас. "
     "И сейчас у нас год в подарок."),
    (("бассейн", "плава", "аква"),
     "Бассейна у нас нет, клуб сухой — говорю честно. Если бассейн обязателен, мы не подойдём. "
     "Если нет — 2500 м², сауна и бойцовский клуб в одном месте."),
    (("адрес", "где вы", "как добра", "метро"),
     f"{CLUB}."),
    # Охраняемую парковку правило требует называть — это единственное
    # удобство, которого нет у соседей. А вот механику доступа («не на
    # всех абонементах») в текстах не расписываем: её объясняет менеджер
    # в звонке (CLAUDE.md, раздел 3а).
    (("парков", "машин", "припарк"),
     "Есть охраняемая парковка у бизнес-центра — у соседей такой нет. "
     "Условия подскажет менеджер."),
    # Круглосуточно — только для членов клуба; гостю время назовёт менеджер.
    (("график", "во сколь", "режим", "часы работ", "круглосут", "ночью работ"),
     "Для членов клуба вход круглосуточный, без выходных. По гостевому визиту время "
     "подберём вместе — когда вам удобнее прийти."),
    (("сауна", "полотенц", "душ", "раздевал"),
     "Финская сауна и ведро-водопад. Полотенца и вода без доплат."),
    (("бокс", "кикбокс", "единоборств", "бойцов", "борьб"),
     "Бойцовский клуб 500 м², бокс и кикбоксинг. Первое занятие — бесплатное."),
    (("тренер", "персональн", "инструктор"),
     "Персональные тренеры есть, подберём под вашу задачу."),
    (("групповы", "расписан", "йога", "пилатес", "аэроб"),
     "Три зала групповых программ, входят в абонемент. Первое занятие — бесплатное, расписание пришлём."),
    # Пункта про тренажёрный зал не было вовсе — самое популярное
    # направление клуба, а на вопрос о нём бот отвечал общей фразой
    # «подберу первые визиты». Ключи намеренно узкие: слово «зал» одно
    # добавлять нельзя, оно перехватит вопросы про залы групповых.
    (("тренажёр", "тренажер", "железо", "штанг", "свободные веса", "качал",
      "становая", "присед"),
     "Тренажёрный зал 1100 м²: свободные веса, тренажёры по группам мышц, "
     "помост для становой и приседа. Первое занятие — бесплатное."),
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


# --------------------------------------------------- отметки из чата продаж
#
# Менеджеры отмечают в чате «Отдел продаж SF» состоявшиеся встречи (плюсик и
# телефон) и продажи (телефон и «НК» либо «Продление»). По этим отметкам
# считается воронка: дошёл ли гость и купил ли.
#
# Раньше отметки собирал компьютер Ольги, опрашивая бота каждые 10 минут.
# Значит при выключенном ноутбуке дольше суток они терялись безвозвратно —
# Telegram хранит непрочитанное только сутки. Теперь их принимает сервер:
# мгновенно и независимо ни от ноутбука, ни от расписаний.
#
# ВАЖНО: это ОТДЕЛЬНЫЙ бот-читатель, у него свой адрес приёма (/marks/...).
# Клиентского бота в рабочий чат добавлять нельзя — он шлёт туда заявки, и
# получились бы дубли и путаница ролей (прямое замечание Ольги 31.08.2026).
# Бот-читатель в чате только слушает и не пишет ни слова.
#
# Сырой текст чата не сохраняется намеренно — там обсуждают деньги и людей.
# В журнал идут только телефон, тип отметки и кто отметил.

MARKS_SECRET = os.environ.get("MARKS_HOOK_SECRET", "").strip()
# токен служебного бота — нужен, чтобы подтвердить приём выгрузки отправителю
MARKS_BOT_TOKEN = os.environ.get("MARKS_BOT_TOKEN", "").strip()
SALES_CHAT_TITLE = os.environ.get("SALES_CHAT_TITLE", "Отдел продаж SF").strip()
MARKS_REPO = os.environ.get("MARKS_REPO", "aum151-commits/sandow-automation").strip()
MARKS_TOKEN = os.environ.get("MARKS_REPO_TOKEN", "").strip()

# Номер целиком, без разбивки на группы. Разбивка ломалась на слитной записи
# «89991234567», как чаще всего и пишут: восьмёрка уходила в первую тройку,
# и номер получался 8999123456 — на одну цифру короче и не совпадал ни с чем
# при сверке со звонками. Теперь берём фрагмент целиком и оставляем последние
# десять цифр — так «8999…», «+7999…» и «999…» дают один и тот же номер.
_PHONE_RE = re.compile(r"\d[\d\s\-()]{8,18}\d")


def _tail_after_phone(text):
    """Часть сообщения после последнего телефона.

    Правило чата (Ольга, 31.08.2026): фамилия и имя клиента идут ДО телефона,
    а свои — менеджер и эксперт — после телефона и номера контракта, в самом
    низу. Значит искать их надо только в хвосте: тогда «Продление Петрова
    Мария 8916…» не превратит клиентку Марию в менеджера Спиридонову.

    Если телефона нет, возвращаем весь текст — хуже, чем было, не станет.
    """
    последний = None
    for m in _PHONE_RE.finditer(text):
        цифры = re.sub(r"\D", "", m.group(0))
        if len(цифры) >= 10 and цифры[-10:].startswith("9"):
            последний = m
    return text[последний.end():] if последний else text


def _phones(text):
    """Номера из сообщения, приведённые к десяти цифрам.

    Разбор по группам «(\\d{3})(\\d{3})(\\d{2})(\\d{2})» здесь не годится: на
    слитной записи «89991234567» — а её пишут чаще всего — восьмёрка уходила
    в первую тройку, и номер получался на цифру короче. Такой номер не
    совпадал ни с одним звонком, и вся воронка выходила пустой.

    Поэтому берём любой кусок из цифр и разделителей и оставляем последние
    десять цифр. Признак настоящего номера — мобильные в России начинаются
    с девятки; это же отсекает суммы, даты и номера договоров.
    """
    найдено = []
    for m in _PHONE_RE.finditer(text):
        цифры = re.sub(r"\D", "", m.group(0))
        if len(цифры) < 10:
            continue
        номер = цифры[-10:]
        if номер.startswith("9"):
            найдено.append(номер)
    return list(dict.fromkeys(найдено))   # без повторов, порядок сохраняем

# Как менеджеры пишут на самом деле (со слов Ольги 31.08.2026):
#   встреча — строка начинается с плюса и имени гостя: «+Сергей», телефон, комментарий;
#   продажа — начинается с суммы: «$24990 нал», тип оплаты, тип членства, ФИО, телефон.
# Раньше разбор искал «НК» и «Продление» и считал встречей любой плюс в тексте —
# а плюс есть в каждом телефоне «+7…», из-за чего отметкой становилось что угодно.
_MEETING_RE = re.compile(r"(?:^|\n)\s*\+\s*([А-ЯЁA-Z][а-яёa-z\-]+)")
# 31.08.2026: сумму часто пишут с точкой как разделителем тысяч («$10.000») —
# старая версия ловила только цифры и пробелы, точка обрывала совпадение,
# и вся отметка молча пропадала (реальный случай — продажа Ерёмеевой).
_SALE_RE = re.compile(r"[$＄]\s*(\d[\d\s.,]{1,14})")
_PAYMENT_RE = re.compile(r"\b(нал|qr|б/н|бн|безнал|карт\w*|перевод\w*|рассрочк\w*)\b",
                         re.IGNORECASE)

# Кто купил (со слов Ольги 31.08.2026):
#   НК — новый клиент, вчерашний ПЧК (потенциальный член клуба);
#   Продление — платит действующий член клуба или БЧК (бывший член клуба);
#   Возобновление — иногда пишут про БЧК вместо продления, реже.
_KIND_NK = re.compile(r"\bНК\b", re.IGNORECASE)
_KIND_RENEWAL = re.compile(r"продлени", re.IGNORECASE)
_KIND_RETURN = re.compile(r"возобновлени", re.IGNORECASE)
_PCHK_RE = re.compile(r"\bПЧК\b", re.IGNORECASE)
_BCHK_RE = re.compile(r"\bБЧК\b", re.IGNORECASE)

# Вводная персональная тренировка — подарочная, всегда с именем эксперта.
# По ней считается, насколько хорошо тренер доводит гостя до покупки.
_VPT_RE = re.compile(r"\bВПТ\b", re.IGNORECASE)

# Кто вёл консультацию или тренировку. Список с сайта клуба; пополняется
# без выкладки кода — переменной TRAINERS через запятую.
_TRAINERS = [
    "Дарья Салихова", "Дмитрий Анников", "Евгений Леонтьев", "Ева Каймовская",
    "Александр Купричев", "Юрий Яковлев", "Георгий Шарангия", "Наталья Пожилова",
    "Сергей Кочубей", "Виктор Савельев", "Кристина Девакова", "Александр Белодурин",
    "Сергей Зайцев", "Денис Бандурин", "Виктория Устинова", "Нурлан Курбанов",
    "Денис Сандригайло", "Нина Ипатова", "Александр Квасков",
]
_TRAINERS += [t.strip() for t in os.environ.get("TRAINERS", "").split(",") if t.strip()]

# Менеджеры отдела продаж. Нужны, чтобы отличить продавца от тренера: в
# одной строке бывают оба, а ещё случается, что оформлял один менеджер, а
# продажа записана на другого.
#
# Список здесь — только запасной, на случай если облако недоступно.
# Рабочий лежит в приватном репозитории (data/staff/staff.json) и
# обновляется еженедельной сверкой, поэтому смена состава отдела не
# требует ни выкладки кода, ни включённого ноутбука.
_MANAGERS = ["Рыбалко", "Плотникова", "Спиридонова", "Потапова", "Харыбина"]
_MANAGERS += [m.strip() for m in os.environ.get("MANAGERS", "").split(",") if m.strip()]

# Как менеджеры подписываются в чате. Разделены намеренно.
#
# Короткие формы (Лиза, Даша, Таня, Маша) в отметке значат только менеджера —
# в ФИО клиента так не пишут. Состав сверен с Ольгой 15.09.2026: Тоганидзе
# уволилась, добавлены Харыбина (опытный парт-таймер) и Потапова (стажёр).
_MANAGER_SHORT = {
    "лиза": "Рыбалко",
    "маша": "Спиридонова",
    "алина": "Плотникова",
    "таня": "Потапова",
    "даша": "Харыбина",
}
# Полные имена опаснее: «Мария» и «Виктория» бывают именем клиентки в ФИО
# («Продление Петрова Мария …»), а Виктория есть и среди тренеров —
# Виктория Устинова. Поэтому полное имя принимаем за подпись менеджера,
# только если оно стоит в самом конце сообщения, где и подписываются.
_MANAGER_FULL = {
    "елизавета": "Рыбалко",
    "мария": "Спиридонова",
    "алина": "Плотникова",
    "татьяна": "Потапова",
    "дарья": "Харыбина",
}

_STAFF_CACHE = {"когда": 0.0, "менеджеры": None, "короткие": None,
                "полные": None, "тренеры": None, "по_телеграму": None}


def _refresh_staff():
    """Подтягивает актуальные списки из репозитория, не чаще раза в час.

    Смысл: состав отдела меняется, и держать его в коде значит выкладывать
    бота из-за каждой замены. Здесь список правит еженедельная сверка, а бот
    просто читает свежую версию.
    """
    if time.time() - _STAFF_CACHE["когда"] < 3600 or not MARKS_TOKEN:
        return
    _STAFF_CACHE["когда"] = time.time()
    try:
        r = requests.get(
            f"https://api.github.com/repos/{MARKS_REPO}/contents/data/staff/staff.json",
            headers={"Authorization": f"Bearer {MARKS_TOKEN}",
                     "Accept": "application/vnd.github+json"}, timeout=25)
        if r.status_code != 200:
            return
        данные = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
        менеджеры, короткие, полные = [], {}, {}
        for m in данные.get("менеджеры", []):
            фамилия = m.get("фамилия")
            if not фамилия:
                continue
            менеджеры.append(фамилия)
            # полное имя ищем только в подписи, короткое — где угодно
            if m.get("имя"):
                полные[m["имя"].lower()] = фамилия
            for зовут in (m.get("зовут") or []):
                короткие[зовут.lower()] = фамилия
            # прямая привязка телеграм-аккаунта к фамилии — самый точный путь
            for ид in (m.get("телеграм") or []):
                _STAFF_CACHE.setdefault("по_телеграму", {})
                if _STAFF_CACHE["по_телеграму"] is None:
                    _STAFF_CACHE["по_телеграму"] = {}
                _STAFF_CACHE["по_телеграму"][str(ид)] = фамилия
        if менеджеры:
            _STAFF_CACHE["менеджеры"] = менеджеры
            _STAFF_CACHE["короткие"] = короткие
            _STAFF_CACHE["полные"] = полные
        тренеры = данные.get("тренеры") or []
        if тренеры:
            _STAFF_CACHE["тренеры"] = тренеры
    except Exception as exc:
        print(f"[состав] список из облака не обновлён: {exc}", flush=True)

# Откуда пришёл человек — менеджеры помечают это словом в отметке.
# Тонкость от Ольги: тут бывают ошибки, поэтому канал сверяется со звонками
# при расчёте, а не принимается на веру. Рекламный источник (Яндекс, карты,
# 2ГИС) здесь не появится — менеджеры его попросту не знают.
_CHANNELS = (
    ("входящий звонок", re.compile(r"\bВЗ\b|входящ\w*\s+звон", re.IGNORECASE)),
    ("сарафан", re.compile(r"сарафан|рекомендаци", re.IGNORECASE)),
    ("заявка", re.compile(r"\bзаявк\w*", re.IGNORECASE)),
    ("входящий гость", re.compile(r"\bгост\w*", re.IGNORECASE)),
)


def _channel(text):
    for название, шаблон in _CHANNELS:
        if шаблон.search(text):
            return название
    return ""


def _manager_by_author(sender):
    """Менеджер по автору сообщения.

    Правило чата (Ольга, 31.08.2026): встречу отмечает тот, кто её и провёл,
    поэтому своё имя внизу он не подписывает — автор сообщения и есть
    менеджер. Сопоставляем по имени и нику в Telegram.
    """
    _refresh_staff()
    фамилии = _STAFF_CACHE["менеджеры"] or _MANAGERS
    короткие = _STAFF_CACHE["короткие"] or _MANAGER_SHORT
    полные = _STAFF_CACHE["полные"] or _MANAGER_FULL
    привязки = _STAFF_CACHE["по_телеграму"] or {}

    ид = str(sender.get("id") or "")
    if ид in привязки:
        return привязки[ид]

    подпись = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name"),
                                     sender.get("username")])).lower()
    if not подпись:
        return ""
    for фамилия in фамилии:
        if фамилия.lower()[:-1] in подпись:
            return фамилия
    for зовут, фамилия in короткие.items():
        if re.search(rf"\b{зовут}\b", подпись):
            return фамилия
    for имя, фамилия in полные.items():
        if re.search(rf"\b{имя}\b", подпись):
            return фамилия
    return ""


def _remember_author(sender):
    """Копим, кто пишет в чат: id, ник и имя.

    Нужно, чтобы привязать телеграм-аккаунты к фамилиям менеджеров один раз
    и дальше не гадать по имени в профиле — там бывает что угодно.
    """
    ид = str(sender.get("id") or "")
    if not ид or not MARKS_TOKEN:
        return
    запись = {
        "id": ид,
        "username": sender.get("username", ""),
        "имя": " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])),
        "узнан_как": _manager_by_author(sender),
    }
    _append_to_repo("data/staff/authors.jsonl",
                    json.dumps(запись, ensure_ascii=False) + "\n",
                    replaces_key=f'"id": "{ид}"')


def _manager(text):
    """Кто из отдела продаж указан в отметке.

    Ищем и по фамилии (с учётом падежей), и по тому, как человек
    подписывается: «Лиза» вместо Рыбалко, «Мила» вместо Тоганидзе.
    """
    _refresh_staff()
    фамилии = _STAFF_CACHE["менеджеры"] or _MANAGERS
    короткие = _STAFF_CACHE["короткие"] or _MANAGER_SHORT
    полные = _STAFF_CACHE["полные"] or _MANAGER_FULL

    # ищем только после телефона: до него идут фамилия и имя клиента
    низкий = _tail_after_phone(text).lower()

    for фамилия in фамилии:
        if фамилия.lower()[:-1] in низкий:
            return фамилия
    for зовут, фамилия in короткие.items():
        if re.search(rf"\b{зовут}\b", низкий):
            return фамилия
    for имя, фамилия in полные.items():
        if re.search(rf"\b{имя}\b", низкий):
            return фамилия
    return ""


def _expert(text, кроме=""):
    """Кто вёл встречу или тренировку.

    Ищем по фамилиям — они уникальнее имён: Александров в списке трое, а
    Купричев один. Имя проверяем только если оно встречается у одного
    тренера, иначе непонятно, о ком речь.
    """
    _refresh_staff()
    список = _STAFF_CACHE["тренеры"] or _TRAINERS

    # как и менеджер, эксперт указывается после телефона — до него клиент
    низкий = _tail_after_phone(text).lower()
    for полное in список:
        имя, _, фамилия = полное.partition(" ")
        if фамилия and фамилия.lower()[:-1] in низкий:   # без последней буквы: падежи
            return полное
    for полное in список:
        имя = полное.split()[0]
        if имя == кроме:
            continue   # это имя гостя, а не тренера
        одноимённых = sum(1 for t in список if t.split()[0] == имя)
        if одноимённых == 1 and re.search(rf"\b{имя.lower()}\b", низкий):
            return полное
    return ""


def _append_to_repo(path, line, replaces_message_id=None, replaces_key=None):
    """Пишет строку в журнал внутри приватного репозитория.

    Файловая система сервера здесь не годится: Render перезапускает контейнер
    и всё, что не в репозитории, пропадает.

    replaces_message_id — если менеджер поправил своё сообщение (руководство
    заметило ошибку и написало обратную связь), старую запись надо заменить,
    а не добавить вторую. Иначе одна продажа считалась бы дважды.
    """
    if not MARKS_TOKEN:
        print("[отметки] нет токена репозитория — отметка не сохранена", flush=True)
        return
    head = {"Authorization": f"Bearer {MARKS_TOKEN}",
            "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{MARKS_REPO}/contents/{path}"
    try:
        was = requests.get(url, headers=head, timeout=25)
        body = {"message": "Отметка из чата отдела продаж"}
        if was.status_code == 200:
            old = base64.b64decode(was.json()["content"]).decode("utf-8")
            body["sha"] = was.json()["sha"]
        else:
            old = ""

        метка = None
        if replaces_message_id is not None:
            метка = f'"message_id": {replaces_message_id}'
        elif replaces_key:
            метка = replaces_key
        if метка:
            строки = [s for s in old.splitlines() if s.strip() and метка not in s]
            old = ("\n".join(строки) + "\n") if строки else ""

        body["content"] = base64.b64encode((old + line).encode("utf-8")).decode()
        r = requests.put(url, headers=head, json=body, timeout=30)
        if r.status_code not in (200, 201):
            print(f"[отметки] журнал не обновлён: {r.status_code}", flush=True)
    except Exception as exc:
        print(f"[отметки] сбой записи: {exc}", flush=True)


def mark_from_sales_chat(msg, правка=False):
    """Разбирает отметку менеджера. Возвращает True, если сообщение из чата
    продаж и дальше его обрабатывать не нужно.

    правка=True — сообщение отредактировали. Так бывает часто: руководство
    видит ошибку, пишет обратную связь, менеджер исправляет своё сообщение.
    Тогда прежнюю запись заменяем, а не добавляем ещё одну.
    """
    chat = msg.get("chat", {})
    if chat.get("title") != SALES_CHAT_TITLE:
        return False

    # Автора запоминаем при любом сообщении, а не только при отметке: так
    # телеграм-аккаунты менеджеров привязываются к фамилиям за день, без
    # пересылок куда-то на сторону.
    _remember_author(msg.get("from", {}))

    text = msg.get("text") or msg.get("caption") or ""
    phones = _phones(text)
    if not phones:
        # 31.08.2026: раньше молчали совсем — за день пришли десятки
        # сообщений из чата продаж, но ни одно не превратилось в отметку,
        # и разобраться, почему, было не по чему. Короткий обрезок текста —
        # ровно то, что уже видно в чате, лишнего не пишем.
        print(f"[отметки] без телефона, пропущено: {text[:100]!r}", flush=True)
        return True  # это чат продаж, но не отметка — просто молчим

    продажа = _SALE_RE.search(text)
    встреча = _MEETING_RE.search(text)
    if not (продажа or встреча):
        print(f"[отметки] телефон есть, но нет $ или + в начале строки: {text[:100]!r}", flush=True)
        return True

    sender = msg.get("from", {})
    who = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    base = {
        "marked_at": datetime.fromtimestamp(msg.get("date", 0), timezone.utc).isoformat(),
        "reporter": who,
        "message_id": msg.get("message_id"),
        "source": "бот в чате",
    }

    заменить = msg.get("message_id") if правка else None

    if продажа:
        сумма = int(re.sub(r"\D", "", продажа.group(1)) or 0)
        оплата = _PAYMENT_RE.search(text)
        # ФИО клиента не сохраняем: для воронки хватает телефона, а лишние
        # персональные данные в журнале — лишний риск

        # кто купил: новый клиент или тот, кто уже был в клубе
        if _KIND_NK.search(text) or _PCHK_RE.search(text):
            вид = "НК"
        elif _KIND_RETURN.search(text):
            вид = "Возобновление"
        elif _KIND_RENEWAL.search(text) or _BCHK_RE.search(text):
            вид = "Продление"
        else:
            вид = ""   # менеджер не указал — пусть будет видно, а не выдумано

        rows = "".join(json.dumps({
            "phone": p,
            "amount": сумма,
            "payment": (оплата.group(1).lower() if оплата else ""),
            "kind": вид,
            # тренера иногда дописывают в конце строки продажи
            "expert": _expert(text),
            # в продаже менеджера подписывают явно; если не подписан —
            # берём автора сообщения, как и во встрече
            "manager": _manager(text) or _manager_by_author(sender),
            "channel": _channel(text),
            "edited": bool(правка),
            **base,
        }, ensure_ascii=False) + "\n" for p in phones)
        _append_to_repo("data/conversions/sales_marks.jsonl", rows, заменить)
        print(f"[отметки] продажа{' (правка)' if правка else ''}: {сумма} ₽, "
              f"{вид or 'вид не указан'}, {len(phones)} тел.", flush=True)
    else:
        гость = встреча.group(1)
        # Встречу отмечает тот, кто её провёл, — своё имя он не подписывает.
        # Поэтому менеджер здесь берётся из автора сообщения, а из текста —
        # только если он всё же подписался.
        менеджер = _manager(text) or _manager_by_author(sender)
        rows = "".join(json.dumps({
            "phone": p,
            "guest": гость,
            # эксперт — он же тренер, он же фитнес-эксперт: подписывается внизу
            "expert": _expert(text, кроме=гость),
            "manager": менеджер,
            "channel": _channel(text),
            # подарочная вводная тренировка — отдельная воронка
            "vpt": bool(_VPT_RE.search(text)),
            "edited": bool(правка),
            **base,
        }, ensure_ascii=False) + "\n" for p in phones)
        _append_to_repo("data/conversions/meetings.jsonl", rows, заменить)
        print(f"[отметки] {'ВПТ' if _VPT_RE.search(text) else 'встреча'}"
              f"{' (правка)' if правка else ''}: гость «{гость}», "
              f"эксперт «{_expert(text, кроме=гость) or 'не указан'}»", flush=True)
    return True


def queue_document(msg):
    """Кладёт присланную выгрузку 1С в очередь для компьютера.

    Сам файл не скачиваем: у Telegram он хранится долго, достаточно запомнить
    file_id. Компьютер, когда включится, заберёт очередь и скачает файлы сам.
    Так выгрузка не теряется, даже если ноутбук выключен неделю — раньше
    непрочитанное пропадало через сутки.
    """
    doc = msg.get("document")
    if not doc:
        return False
    запись = {
        "file_id": doc.get("file_id"),
        "file_name": doc.get("file_name"),
        "size": doc.get("file_size"),
        "chat_id": msg.get("chat", {}).get("id"),
        "from_id": msg.get("from", {}).get("id"),
        "message_id": msg.get("message_id"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_to_repo("data/intake/queue.jsonl",
                    json.dumps(запись, ensure_ascii=False) + "\n")
    print(f"[приём] файл в очереди: {doc.get('file_name')}", flush=True)
    return True


@app.route("/marks/<secret>", methods=["POST"])   # имя в адресе — только латиницей
def marks_hook(secret):
    """Приём сообщений служебного бота: отметки из чата продаж и выгрузки 1С.

    Отдельный адрес: клиентский поток сюда не попадает и не смешивается.
    В чат отдела продаж этот путь ничего не пишет — только слушает.
    """
    if not MARKS_SECRET or secret != MARKS_SECRET:
        return jsonify(ok=False), 404
    upd = request.get_json(force=True, silent=True) or {}
    правка = "edited_message" in upd
    msg = upd.get("message") or upd.get("edited_message")
    if msg:
        # Отметка в журнале о самом факте прихода: название чата и тип, без
        # текста. Без неё непонятно, доходят ли сообщения из группы вообще —
        # а это первое, что нужно знать, если отметки перестали появляться.
        чат = msg.get("chat", {})
        print(f"[приём] сообщение из «{чат.get('title') or 'лички'}» "
              f"({чат.get('type')}), ожидаем «{SALES_CHAT_TITLE}»", flush=True)
        try:
            if not mark_from_sales_chat(msg, правка=правка):
                # не чат продаж — значит личка: выгрузка 1С или что-то ещё
                if queue_document(msg):
                    чат = msg.get("chat", {}).get("id")
                    имя = (msg.get("document") or {}).get("file_name", "файл")
                    if MARKS_BOT_TOKEN:
                        requests.post(
                            f"https://api.telegram.org/bot{MARKS_BOT_TOKEN}/sendMessage",
                            data={"chat_id": чат,
                                  "text": f"Принято: {имя}. Обработаю, как только "
                                          f"компьютер будет на связи."},
                            timeout=25)
        except Exception as exc:
            print(f"[отметки] {exc}", flush=True)
    return jsonify(ok=True)


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

    if data == "freeze":
        with LOCK:
            STATE.setdefault(chat_id, {})["await_freeze"] = True
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="С какого числа и на сколько дней оформить заморозку? "
                        "Напишите, например: «с 5 октября на 14 дней».")

    if data == "tax_cert":
        with LOCK:
            STATE.setdefault(chat_id, {})["await_tax_photo"] = True
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="Подготовим справку. Пришлите, пожалуйста, фото разворота "
                        "паспорта (2-я и 3-я страницы) — и передам в работу. "
                        "Менеджер напишет здесь, когда справка будет готова.\n\n"
                        "Отправляя фото, вы соглашаетесь на обработку персональных данных.")

    if data == "member_term":
        send_to_orders(parse_mode="HTML",
            text=f"📆 <b>СПРОСИЛ СРОК АБОНЕМЕНТА</b>\n{member_card_line(user)}\n"
                 "Ответьте клиенту реплаем на это сообщение — уйдёт ему в бот.\n"
                 f"#id{chat_id}")
        with LOCK:
            STATE.setdefault(chat_id, {})["bridge"] = True
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="Сейчас уточню у менеджера — он ответит вам здесь.")

    if data == "book_training":
        return api("editMessageText", chat_id=chat_id, message_id=mid,
                   text="Вы уже занимаетесь с кем-то из наших фитнес-экспертов?",
                   reply_markup=kb([[("Да", "bt:yes")], [("Нет", "bt:no")]]))

    if data.startswith("bt:"):
        # РЕШЕНИЕ ОЛЬГИ 27.09: члену клуба, который никогда не занимался с
        # тренером В НАШЕМ клубе, вводная тренировка дарится обязательно;
        # тому, кто уже занимается, — не дарится (фильтр вопросом выше).
        # Правка 28.09 (живая проверка): раньше уходило координатору сразу,
        # без вопроса о времени — теперь как у новых клиентов, сначала день
        # и время, и только потом карточка координатору.
        already = data.split(":", 1)[1] == "yes"
        with LOCK:
            STATE.setdefault(chat_id, {})["member_training_already"] = already
        return step_time(chat_id, "training", message_id=mid)

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
        return step_value(chat_id, mid, direction)

    if data.startswith("fmt:"):
        _, code, direction = data.split(":", 2)
        fmt = "training" if code == "t" else "tour"
        with LOCK:
            STATE.setdefault(chat_id, {}).update({"dir": direction, "fmt": fmt})
        return step_phone(chat_id, mid, "", direction, fmt=fmt)

    if data.startswith("tp:"):
        _, period, fmt = data.split(":", 2)
        return step_slots(chat_id, mid, period, fmt)

    if data.startswith("sl:"):
        _, fmt, period, hhmm = data.split(":", 3)
        return slot_chosen(chat_id, mid, user, fmt, period, hhmm)

    if data.startswith("confirmvisit:"):
        target = int(data.split(":", 1)[1])
        return coordinator_confirm(chat_id, mid, target, user)

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
    # реплаи должны работать и там. Координатору (правка 28.09) карточка
    # дублируется личным сообщением — её реплай тоже должен долетать.
    reply_allowed_chats = {str(ORDERS_CHAT), str(BRIDGE_CHAT)}
    coord_id = coordinator_chat_id()
    if coord_id:
        reply_allowed_chats.add(str(coord_id))
    if str(chat_id) in reply_allowed_chats and msg.get("reply_to_message"):
        if bridge_from_group(msg):
            return

    if msg.get("contact"):
        phone = clean_phone(msg["contact"].get("phone_number"))
        return finish(chat_id, user, phone)

    # Раздел 4 «Справка для налогового вычета»: ждём фото разворота паспорта.
    # Фото пересылается менеджеру, в базе бота не хранится (только у Telegram).
    photos = msg.get("photo")
    with LOCK:
        awaiting_tax_photo = STATE.get(chat_id, {}).get("await_tax_photo")
    if awaiting_tax_photo and photos:
        with LOCK:
            STATE.setdefault(chat_id, {}).pop("await_tax_photo", None)
        file_id = photos[-1]["file_id"]
        api("sendPhoto", chat_id=ORDERS_CHAT, photo=file_id, parse_mode="HTML",
            caption=f"🧾 <b>СПРАВКА ДЛЯ ВЫЧЕТА — фото паспорта</b>\n{member_card_line(user)}\n"
                    f"Когда справка готова, ответьте клиенту реплаем на это сообщение.\n"
                    f"#id{chat_id}")
        return api("sendMessage", chat_id=chat_id,
                   text="Фото получено, передала в работу. Менеджер напишет здесь, "
                        "когда справка будет готова.")
    if awaiting_tax_photo and text and not text.startswith("/"):
        return api("sendMessage", chat_id=chat_id,
                   text="Жду именно фото разворота паспорта (2-я и 3-я страницы) — "
                        "пришлите его картинкой.")

    # Раздел 4 «Заморозка абонемента»: свободный текст с датой и сроком —
    # менеджер подтверждает оформление, сам бот стоимость не называет
    # (зависит от формата карты).
    with LOCK:
        awaiting_freeze = STATE.get(chat_id, {}).get("await_freeze")
    if awaiting_freeze and text and not text.startswith("/"):
        with LOCK:
            STATE.setdefault(chat_id, {}).pop("await_freeze", None)
        send_to_orders(parse_mode="HTML",
            text=f"❄️ <b>ЗАПРОС ЗАМОРОЗКИ</b>\n{member_card_line(user)}\n"
                 f"Клиент указал: {text}\n"
                 "Оформите и подтвердите клиенту реплаем на это сообщение.\n"
                 f"#id{chat_id}")
        return api("sendMessage", chat_id=chat_id,
                   text=f"Принято: заморозка — {text}. Менеджер подтвердит "
                        "оформление здесь же.")

    if text.startswith("/start"):
        archive_dialog(chat_id)
        # deep-link с лендинга: «/start boks» → тема разговора с первого слова
        parts = text.split(maxsplit=1)
        theme = parts[1].strip().lower() if len(parts) > 1 else ""
        # Белый список: payload deep-link у Telegram и так ограничен
        # [A-Za-z0-9_-], но напечатанное руками «/start <b» ушло бы в
        # заявку как есть и сломало разбор HTML — заявка не дошла бы
        # ни в группу, ни в личку.
        src = theme if re.fullmatch(r"[a-z0-9_-]{1,32}", theme or "") else None

        # Код с печатного макета внутри клуба: человек уже член клуба,
        # развилка «Хочу в клуб / Уже занимаюсь» ему не нужна — ведём
        # сразу в меню. Источник пишем в базу, чтобы было видно, какой
        # носитель сработал.
        if src in КЛУБНЫЕ_ВХОДЫ:
            with LOCK:
                STATE.pop(chat_id, None)
                STATE[chat_id] = {"src": src}
            save_subscriber(user, segment="member", source=КЛУБНЫЕ_ВХОДЫ[src])
            имя = user.get("first_name") or ""
            api("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=(f"Здравствуйте{', ' + имя if имя else ''}! Это Телеграм "
                      "клуба «Сандов Фитнес».\n\n"
                      "Теперь расписание и связь с менеджером — здесь, "
                      "звонить не нужно."))
            return step_member_menu(chat_id, greet=False)

        if theme not in LANDINGS:
            theme = None
        with LOCK:
            STATE.pop(chat_id, None)
            if src:
                STATE[chat_id] = {"src": src}
        save_subscriber(user, source=(LANDINGS[theme][0] if theme in LANDINGS else src))
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

    # Раздел 2.3а: ждём ответ на вопрос о здоровье. Не влияет на запись —
    # что бы человек ни написал, идём дальше к выбору времени.
    with LOCK:
        st = STATE.get(chat_id, {})
        awaiting_health = st.get("await_health")
    if awaiting_health and text and not text.startswith("/"):
        with LOCK:
            st = STATE.setdefault(chat_id, {})
            st.pop("await_health", None)
            health = "" if text.strip().lower() in ("нет", "нету", "-", "нет.") else text.strip()
            st["health"] = health
            fmt = st.get("fmt", "training")
        # В рабочий чат здоровье отдельно НЕ шлём — оно уже попадёт в общую
        # карточку записи (finalize_booking), когда клиент выберет время.
        # Ранняя отправка была ровно тем, из-за чего менеджер думал, что
        # нужно звонить прямо сейчас, хотя клиент ещё отвечает боту.
        return step_time(chat_id, fmt)

    # Раздел 2.3б/«Своё время»: человек не выбрал готовый слот, а пишет
    # день и время словами — передаём координатору как есть.
    with LOCK:
        st = STATE.get(chat_id, {})
        awaiting_own_time = st.get("await_own_time")
    if awaiting_own_time and text and not text.startswith("/"):
        with LOCK:
            st = STATE.setdefault(chat_id, {})
            st.pop("await_own_time", None)
            st["time_pref"] = text.strip()
            member_flow = "member_training_already" in st
            snapshot = dict(st)
        if member_flow:
            return finalize_member_training(chat_id, None, user, snapshot)
        return finalize_booking(chat_id, None, user, snapshot)

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
                text=f"{name}, заявка принята 🎁 Остался один шаг:",
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
    fmt = st.get("fmt", "training")
    source = LANDINGS.get(st.get("src"), ("",))[0]
    if too_soon(user.get("id")):
        return api("sendMessage", chat_id=chat_id,
                   text="Ваш номер уже у нас — позвоним. "
                        f"Если срочно, наберите нас: {PHONE}",
                   reply_markup={"remove_keyboard": True})
    save_subscriber(user, segment="new", phone=phone)
    # Правка Ольги 28.09: заявка в 1С заводится сразу по номеру (не терять
    # данные), но РАБОЧИЙ ЧАТ не уведомляется сразу — человек ещё отвечает
    # боту (здоровье, время), менеджеру рано звонить, он решит, что клиент
    # ждёт звонка, а тот просто продолжает диалог. Уведомление уходит
    # только: (а) когда запись реально оформлена — finalize_booking,
    # (б) если человек замолчал на 15 минут, не дойдя до конца — watchdog.
    send_to_1c(user, phone, goal, direction, source, fmt=fmt)
    log_lead_event(user.get("id"), "lead_created", phone=phone)
    with LOCK:
        STATE[chat_id] = {
            "segment": "new", "dir": direction, "fmt": fmt, "phone": phone,
            "client_name": user.get("first_name", ""),
        }
    schedule_dropoff_watch(chat_id)
    if fmt == "training":
        ask_health(chat_id, user.get("first_name"))
    else:
        step_time(chat_id, fmt)


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

    сырые = request.get_json(silent=True) or request.form or {}

    # Имена полей приводим к нижнему регистру. Наши формы шлют "phone" и
    # "name", а вебхук Тильды — "Phone", "Name", "Comment" с большой
    # буквы. Пока этого не было, заявка с формы Тильзы упиралась в
    # ответ 400 «phone»: приёмник просто не находил телефон.
    # Найдено 26.09.2026 при разборе «заявки с сайта не доходят до чата
    # менеджеров» (требование Ольги: заявки должны падать И в 1С, И в
    # группу заявок — только так их можно контролировать).
    data = {str(k).lower(): v for k, v in dict(сырые).items()}

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

    # «page» — наши формы, «formname»/«referer» — вебхук Тильды.
    page = (data.get("page") or data.get("formname")
            or data.get("referer") or "").strip()[:120]
    name = (data.get("name") or "").strip()[:60]
    when = datetime.now(MSK).strftime("%d.%m в %H:%M")

    # Проверочные номера (+7 000 ...) в рабочую группу не идут: она для
    # живых заявок, а не для наших тестов. Ответ форме при этом обычный,
    # чтобы проверять всю цепочку целиком.
    if digits[-10:].startswith("000"):
        api("sendMessage", chat_id=FALLBACK_CHAT, parse_mode="HTML",
            text=(f"🧪 <b>Тестовая заявка с сайта</b> (в группу не отправлена)\n"
                  f"Телефон: <code>{phone}</code>\nСтраница: {page or '—'}\n{when}"))
        return _cors(jsonify(ok=True, test=True), origin)

    text = (
        "🌐 <b>ЗАЯВКА С САЙТА</b>\n\n"
        f"<b>Имя:</b> {name or 'не указано'}\n"
        f"<b>Телефон:</b> <code>{phone}</code>\n\n"
        f"<b>Подарок:</b> {GIFT}\n"
        f"<b>Страница:</b> {page or 'не указана'}\n\n"
        # Раньше здесь всегда стояло «Форма на статье» — для заявки с
        # главной страницы это неправда и сбивает менеджера с толку.
        f"{'Форма на сайте' if data.get('formid') or data.get('formname') else 'Форма на статье'} · {when}"
    )
    user = {"id": f"site-{digits[-10:]}", "first_name": name or "Гость с сайта"}
    # Метки источника берём те, что пришли с формой: у заявок с сайта
    # это yandex/maps и прочее — они должны доехать до 1С как есть.
    метки = {k: data.get(k) for k in
             ("utm_source", "utm_medium", "utm_campaign", "utm_content",
              "utm_term", "formname", "formid", "referer")}
    метки["tranid"] = (data.get("tranid")
                       or f"site-{digits[-10:]}-{int(time.time())}")
    if not метки.get("utm_source"):
        метки["utm_source"] = "site"
        метки["utm_medium"] = "form"
    # Комментарий в карточке 1С тоже не должен врать про Telegram-бота:
    # менеджер по нему понимает, откуда человек и о чём с ним говорить.
    метки["formid"] = метки.get("formid") or "sandow_site_form"
    метки["Comment"] = ("Заявка с сайта. "
                        + (f"Страница: {page}. " if page else "")
                        + f"Подарок: {GIFT}.")
    text += send_to_1c(user, phone, "keep", "any", f"страница: {page}", метки)
    send_to_orders(text=text, parse_mode="HTML")
    учесть_заявку()
    # Заявка с сайта — в тот же журнал SLA, что и заявки бота. До 27.09.2026
    # журнал видел только Telegram-заявки (которых почти нет), поэтому
    # скорость первого контакта было не по чему считать.
    log_lead_event(user["id"], "lead_created", phone=phone)
    return _cors(jsonify(ok=True), origin)


# ------------------------------------------------ расписание облачных задач
#
# Зачем расписание живёт здесь, а не в GitHub. Задачи клуба выполняет GitHub
# Actions, но время там соблюдается «по возможности»: замерено за 01.09.2026 —
# «каждые полчаса» обернулось четырьмя запусками за сутки, «каждые 10 минут»
# тремя. Человек, которому не перезвонили, попадал в отчёт через три часа.
# Этот сервис живёт круглосуточно, поэтому время задаёт он, а GitHub только
# делает работу.
#
# Снаружи каждые несколько минут стучится бесплатный будильник (cron-job.org)
# на адрес /cron/<секрет>. Наружу отдан только адрес: токен GitHub остаётся
# здесь, в переменных сервиса.

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
# ВНИМАНИЕ: имена намеренно СВОИ, не GH_TOKEN/GH_REPO. Раньше здесь стояли
# те же имена, что у хранилища подписчиков (строки 82-83), и переопределяли
# их на весь модуль: база подписчиков и журнал лидов уходили в ПУБЛИЧНЫЙ
# репозиторий sandow-lp вместо приватного sandow-automation. Утечка найдена
# 13.09.2026. Не переименовывать обратно и не заводить здесь общих имён.
CRON_GH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "").strip()
CRON_GH_REPO = os.environ.get("GH_REPO", "aum151-commits/sandow-lp").strip()

# задача → (запускать раз в N минут, в какие часы по Москве).
# Часы совпадают с расписанием в самих задачах; здесь они продублированы,
# потому что решение принимается тут.
РАСПИСАНИЕ = {
    "calls_watch.yml":     (30, range(10, 22)),   # пропущенные звонки
    "call_tasks.yml":      (10, range(10, 22)),   # напоминания перезвонить
    "calls.yml":           (30, range(9, 23)),    # отчёт о звонках
    "yandex_requests.yml": (20, range(0, 24)),    # заявки с карточки Яндекса
    "plans.yml":           (30, range(8, 19)),    # планы и просрочки
    "renewals.yml":        (60, range(10, 19)),   # отчёт по звонкам 13 и 17
    "conversions.yml":     (60, range(0, 24)),    # конверсии отдела продаж
    "appointments.yml":    (60, range(0, 24)),    # встречи из звонков
    "site_guard.yml":     (120, range(0, 24)),    # охрана сайта
    "cloud_guard.yml":    (180, range(0, 24)),    # сторож самих задач
}

_СЛОТЫ = {}                       # задача → номер отрезка, который уже отработал
_СЛОТЫ_ЗАМОК = threading.Lock()


def _слот(шаг, сейчас):
    """Номер отрезка внутри суток.

    Считаем от начала суток, а не «сколько прошло с прошлого раза»: тогда
    повторный стук будильника в ту же минуту не запускает задачу дважды,
    а перезапуск сервиса не сбивает сетку.
    """
    return (сейчас.hour * 60 + сейчас.minute) // шаг


def _запустить(имена):
    for имя in имена:
        try:
            r = requests.post(
                f"https://api.github.com/repos/{CRON_GH_REPO}/actions/workflows/{имя}/dispatches",
                headers={"Authorization": f"Bearer {CRON_GH_TOKEN}",
                         "Accept": "application/vnd.github+json"},
                json={"ref": "main"}, timeout=40)
            print(f"[расписание] {имя} → {r.status_code}", flush=True)
        except Exception as exc:
            print(f"[расписание] {имя} не запустилась: {exc}", flush=True)


@app.route("/cron/<secret>", methods=["GET", "POST"])
def cron_tick(secret):
    if not CRON_SECRET or secret != CRON_SECRET:
        return jsonify(ok=False), 404
    if not CRON_GH_TOKEN:
        return jsonify(ok=False, error="нет токена GitHub"), 500

    сейчас = datetime.now(MSK)
    пора = []
    for имя, (шаг, часы) in РАСПИСАНИЕ.items():
        if сейчас.hour not in часы:
            continue
        слот = _слот(шаг, сейчас)
        with _СЛОТЫ_ЗАМОК:
            if _СЛОТЫ.get(имя) == слот:
                continue
            _СЛОТЫ[имя] = слот
        пора.append(имя)

    # Запускаем в стороне от ответа: будильник не должен ждать, пока GitHub
    # примет десяток запросов, иначе он посчитает вызов неудачным.
    if пора:
        threading.Thread(target=_запустить, args=(пора,), daemon=True).start()

    return jsonify(ok=True, время=сейчас.strftime("%d.%m %H:%M"), запущено=пора)


# Метка версии: по ней видно, доехал ли новый код до сервера. Render
# иногда не пересобирает сервис, а без панели управления это не проверить.
VERSION = "2026-09-28-v31-member-confirm-button"


@app.route("/health")
@app.route("/")
def health():
    return jsonify(ok=True, bot="sandow-lead-bot", leads=len(LAST_LEAD),
                   version=VERSION, расписание=len(РАСПИСАНИЕ),
                   будильник=bool(CRON_SECRET and CRON_GH_TOKEN),
                   хранилище=GH_REPO,
                   # Флаги нужны сторожу: миграция 18.09.2026 перенесла на новый
                   # сервис не все переменные, и бот молча перестал писать журналы —
                   # снаружи это было неотличимо от исправной работы.
                   токен_хранилища=bool(GH_TOKEN),
                   вебхук_1с=bool(ONEC_WEBHOOK),
                   чат_заявок=bool(ORDERS_CHAT))


def _self_ping():
    """Держит бесплатный сервис Render в тонусе — сам стучится на свой
    публичный адрес каждые ~10 минут, тем же приёмом, что уже работает
    у sandow-cron (память cloud-scheduler-own-heartbeat, 02.09.2026).
    Не зависит ни от ноутбука, ни от расписания GitHub Actions (оно
    достоверно опаздывает на часы вместо минут — известное свойство
    платформы, не наша поломка). Правка Ольги 28.09.2026: бот теперь
    на отдельном Render-аккаунте только для себя, поэтому круглосуточный
    self-ping не делит лимит часов ни с кем ещё."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        print("[self-ping] RENDER_EXTERNAL_URL не задан — самопробуждение выключено", flush=True)
        return
    while True:
        try:
            requests.get(f"{url}/health", timeout=30)
        except Exception as exc:
            print(f"[self-ping] {exc}", flush=True)
        time.sleep(600)


threading.Thread(target=_self_ping, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
