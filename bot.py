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
#
# 02.10.2026, правка Ольги: запись на подарочную тренировку — каждый час
# с 10:00, последний вариант 20:00. Было шесть вариантов через два часа
# (10:00, 12:00, 14:00, 16:00, 17:30, 19:30) — человек, которому удобно
# в 11 или в 18, не находил своего времени и уходил.
#
# ВАЖНО про связку: у тренеров свой список окон — SLOT_LIST ниже (кнопки
# «отметьте свободные окна»). Он НЕ совпадает с SLOTS_TRAINING, и в этом
# месте они должны совпадать: координатор подбирает тренера на выбранное
# время сверкой `slot in free` (_free_trainers_for). Пока списки разные,
# для часов, которых нет у тренеров (11, 13, 15, 18, 20), кандидаты не
# найдутся — координатор увидит слепую кнопку «Подтвердить время» вместо
# выбора тренера. Решение о том, расширять ли окна тренерам, — за Ольгой.
SLOTS_TRAINING = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00",
                  "16:00", "17:00", "18:00", "19:00", "20:00"]
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

# Просьба Ольги 29.09.2026: живой тестовый прогон бота на предмет расхождений
# со схемой — рабочую группу, координатора и 1С трогать нельзя, только её
# личный чат. Тот же аккаунт, что и FALLBACK_CHAT (в приватном чате с ботом
# chat_id совпадает с Telegram id пользователя), поэтому её собственный
# разговор с ботом опознаётся сам, без отдельного переключателя «режим теста».
OLGA_TEST_ID = FALLBACK_CHAT


def is_live_test(chat_id):
    return bool(OLGA_TEST_ID) and str(chat_id) == str(OLGA_TEST_ID)


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


def send_to_orders(subject_chat_id=None, **payload):
    """Отправка в рабочую группу с запасным выходом.

    14.08 бот оказался удалён из группы заявок, и заявка ушла в никуда —
    молча. Теперь при недоступной группе сообщение падает в личный чат
    Ольги с пометкой тревоги: потерять заявку тихо больше нельзя.

    subject_chat_id — чат клиента, о котором это сообщение (не обязателен
    для системных уведомлений). Если это её собственный тестовый разговор
    (is_live_test), сообщение уходит ей же с пометкой «ТЕСТ», а не в
    рабочую группу — просьба Ольги 29.09.2026 не беспокоить никого во
    время живой проверки бота.
    """
    if is_live_test(subject_chat_id):
        payload["text"] = "🧪 ТЕСТ (в рабочую группу не отправлено):\n\n" + payload.get("text", "")
        return api("sendMessage", chat_id=subject_chat_id, **payload)
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


def gh_update_json(path, mutate, message, attempts=5):
    """Как gh_write_json, но безопасно при гонке: перед каждой попыткой
    перечитывает файл заново и применяет mutate(data) к свежей копии,
    а не пишет один раз поверх снимка, снятого до этого. Нужен для файлов,
    которые правит сразу много людей почти одновременно (окна тренеров
    вечером, до 19 человек за несколько минут) — обычный read-once-write-once
    ловил 409 и терял запись (инцидент 28-29.09 с followup_24h.yml)."""
    for attempt in range(attempts):
        try:
            url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
            r = requests.get(url, headers=_gh_headers(), timeout=30)
            if r.status_code == 200:
                payload = r.json()
                data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
                sha = payload["sha"]
            else:
                data, sha = {}, None
            mutate(data)
            body = {"message": message,
                    "content": base64.b64encode(
                        json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()}
            if sha:
                body["sha"] = sha
            w = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
            if w.status_code in (200, 201):
                return data
            if w.status_code != 409:
                print(f"[gh] обновление {path}: {w.status_code} {w.text[:120]}", flush=True)
                return None
        except Exception as exc:
            print(f"[gh] обновление {path}: {exc}", flush=True)
        time.sleep(1 + attempt)
    print(f"[gh] обновление {path}: не удалось после {attempts} попыток", flush=True)
    return None


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


TRAINER_GH_PATH = "data/trainer_subscribers.json"


def trainer_name_for(chat_id):
    """Имя зарегистрированного тренера по его chat_id, или None."""
    data = gh_read_json(TRAINER_GH_PATH, default={}) or {}
    rec = data.get(str(chat_id))
    return rec.get("name") if rec else None


def register_trainer(chat_id, user, name):
    """Саморегистрация тренера — раздел 11 сценария, правка Ольги 29.09:
    регистрируются ВСЕ тренеры, а не пилотная тройка (бесплатные ВПТ ведут
    все). Читаем-пишем без повтора при 409 — по той же логике, что и
    save_subscriber: коллизия маловероятна (регистрация разовая), а раз в
    несколько лет случившийся конфликт чинится повторным нажатием кнопки."""
    data = gh_read_json(TRAINER_GH_PATH, default={}) or {}
    data[str(chat_id)] = {
        "name": name,
        "username": user.get("username", ""),
        "registered_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M"),
    }
    gh_write_json(TRAINER_GH_PATH, data, f"тренер зарегистрирован: {name}")


def step_trainer_register(chat_id, user):
    existing = trainer_name_for(chat_id)
    if existing:
        return api("sendMessage", chat_id=chat_id,
            text=f"Вы уже зарегистрированы как {existing}. Каждый вечер в 21:00 "
                 "буду спрашивать свободные окна на завтра.")
    buttons = [(name, f"trainer_pick:{i}") for i, name in enumerate(_TRAINERS)]
    grid = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    api("sendMessage", chat_id=chat_id,
        text="Привет! Выберите своё имя из списка — один раз, дальше буду "
             "узнавать вас сам:",
        reply_markup=kb(grid))


# --------------------------------------------------- календарь окон тренеров

TRAINER_SLOTS_PATH = "data/trainer_slots.json"
# Окна тренеров — ШИРЕ, чем у гостя: с 07:00 до 22:00, каждый час (16 окон).
# Причина (Ольга 02.10.2026): часть записей открыта действующим членам клуба —
# у них доступ 24/7 и присутствие менеджера не требуется, поэтому раннее утро
# и поздний вечер тоже рабочие. Раньше было шесть окон через два часа
# (10:00, 12:00, 14:00, 16:00, 17:30, 19:30) — они не покрывали ни утро, ни
# вечер и не совпадали с тем, что выбирает гость.
#
# СВЯЗКА, которую нельзя терять: этот список обязан включать КАЖДЫЙ час из
# SLOTS_TRAINING (10:00–20:00). Координатор подбирает тренера на время гостя
# сверкой `slot in free` (_free_trainers_for); если часа нет в кнопках тренера,
# тренер не сможет отметить его свободным, и координатор вместо выбора тренера
# получит слепую кнопку «Подтвердить время».
SLOT_LIST = ["07:00", "08:00", "09:00", "10:00", "11:00", "12:00", "13:00",
             "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00",
             "21:00", "22:00"]

# Действующий член клуба выбирает время ШИРЕ, чем новый гость: с 07:00 до 22:00,
# тот же набор, что у тренеров (Ольга 02.10.2026). Причина: у члена клуба доступ
# 24/7, присутствие менеджера и координатора для раннего утра и позднего вечера
# не требуется — а новый гость приходит на первую ознакомительную тренировку,
# и её ведём в рабочие часы (10:00–20:00).
# Держим ровно равным SLOT_LIST: если у члена будет час, которого нет в кнопках
# тренера, тренер не сможет отметить его свободным и координатор не найдёт
# кандидата. Проверка инварианта — скриптом после каждой правки сеток.
SLOTS_MEMBER = list(SLOT_LIST)


def _trainer_day_rec(data, date, name):
    return (data.get("days", {}).get(date, {}).get(name)
            or {"free": [], "day_off": False, "booked": {}})


def _free_trainers_for(date, slot):
    """Тренеры, у которых этот слот в этот день отмечен свободным и ещё
    не занят гостем — для выбора координатором при подтверждении записи
    (раздел 11 сценария, правка Ольги 29.09)."""
    if not date or not slot:
        return []
    days = (gh_read_json(TRAINER_SLOTS_PATH, default={}) or {}).get("days", {})
    day = days.get(date, {})
    return [nm for nm, rec in day.items()
            if not rec.get("day_off") and slot in rec.get("free", [])
            and slot not in rec.get("booked", {})]


def trainer_chat_id_for(name):
    """chat_id зарегистрированного тренера по имени — чтобы прислать ему
    карточку гостя. None, если ещё не регистрировался."""
    data = gh_read_json(TRAINER_GH_PATH, default={}) or {}
    for cid, rec in data.items():
        if rec.get("name") == name:
            return int(cid)
    return None


def render_trainer_slot_kb(date, name):
    data = gh_read_json(TRAINER_SLOTS_PATH, default={}) or {}
    rec = _trainer_day_rec(data, date, name)
    free = set(rec.get("free", []))
    booked = rec.get("booked", {})
    rows = []
    for i in range(0, len(SLOT_LIST), 3):
        row = []
        for s in SLOT_LIST[i:i + 3]:
            if s in booked:
                row.append((f"👤 {s}", "noop"))
            else:
                label = ("✅ " if s in free else "") + s
                row.append((label, f"tslot:{date}:{s}"))
        rows.append(row)
    off_label = "✅ Занят весь день" if rec.get("day_off") else "Занят весь день"
    rows.append([(off_label, f"tslot_off:{date}")])
    rows.append([("На неделю вперёд — так же все 7 дней", f"tslot_week:{date}")])
    return rows


def send_evening_slot_prompt():
    """21:00 каждый день (раздел 11 сценария) — просим окна на завтра.
    Триггер — внутренний таймер бота, не GitHub Actions schedule (он
    ненадёжен, см. _self_ping)."""
    tomorrow = (datetime.now(MSK) + timedelta(days=1)).strftime("%Y-%m-%d")
    trainers = gh_read_json(TRAINER_GH_PATH, default={}) or {}
    # уже закрыт «на неделю вперёд» (или чем-то ещё) — второй раз не спрашиваем,
    # обещание «ежедневных сообщений не будет» должно выполняться буквально
    already = (gh_read_json(TRAINER_SLOTS_PATH, default={}) or {}).get("days", {}).get(tomorrow, {})
    for chat_id, rec in trainers.items():
        name = rec.get("name")
        if not name or name in already:
            continue
        api("sendMessage", chat_id=int(chat_id),
            text=f"Отметьте свободные окна на завтра, {tomorrow}:",
            reply_markup=kb(render_trainer_slot_kb(tomorrow, name)))


def send_morning_trainer_reminder():
    """9:00 — одно напоминание тем, кто ещё не отметил окна на сегодня."""
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    trainers = gh_read_json(TRAINER_GH_PATH, default={}) or {}
    slots = gh_read_json(TRAINER_SLOTS_PATH, default={}) or {}
    day = slots.get("days", {}).get(today, {})
    for chat_id, rec in trainers.items():
        name = rec.get("name")
        if not name or name in day:
            continue
        api("sendMessage", chat_id=int(chat_id),
            text=f"Не забудьте отметить окна на сегодня, {today}:",
            reply_markup=kb(render_trainer_slot_kb(today, name)))


def send_coordinator_summary():
    """9:30 — координатору таблица «тренер × окна на сегодня-завтра»."""
    coord_id = coordinator_chat_id()
    if not coord_id:
        return
    now = datetime.now(MSK)
    dates = [now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    days = (gh_read_json(TRAINER_SLOTS_PATH, default={}) or {}).get("days", {})
    lines = ["🗓 <b>Окна тренеров на сегодня-завтра</b>"]
    for d in dates:
        lines.append(f"\n<b>{d}</b>")
        day = days.get(d, {})
        if not day:
            lines.append("— никто ещё не отметил")
            continue
        for name, rec in day.items():
            if rec.get("day_off"):
                lines.append(f"{name}: занят весь день")
                continue
            free, booked = rec.get("free", []), rec.get("booked", {})
            parts = [f"{s} (гость {booked[s]})" if s in booked else s
                     for s in SLOT_LIST if s in booked or s in free]
            lines.append(f"{name}: {', '.join(parts) if parts else 'окон нет'}")
    api("sendMessage", chat_id=coord_id, parse_mode="HTML", text="\n".join(lines))


BOT_STATE_PATH = "data/bot_scheduler_state.json"
_DAILY_MARKS = {"evening_prompt": None, "morning_nag": None, "coord_summary": None,
                "followup_24h_hour": None, "op_schedule_refresh": None}


def _load_daily_marks():
    sent = gh_read_json(BOT_STATE_PATH, default={}) or {}
    for k in _DAILY_MARKS:
        _DAILY_MARKS[k] = sent.get(k)


def _mark_daily_sent(key, value):
    _DAILY_MARKS[key] = value
    gh_update_json(BOT_STATE_PATH, lambda d: d.__setitem__(key, value), f"daily-mark {key} {value}")


def send_24h_followups():
    """Раньше — followup_24h.yml (sandow-lp, cron раз в час). Перенесено
    внутрь бота 30.09.2026 по просьбе Ольги: работа не должна зависеть от
    GitHub Actions вообще (у schedule-триггера доказанные многочасовые
    пропуски, см. ЗАДАЧИ-ХВОСТ.md) — бот и так живёт непрерывно (self-ping).
    Логика 1:1 с прежним workflow, включая безопасную от гонки запись
    флага (тот же gh_update_json, что и у окон тренеров)."""
    now = datetime.now(MSK)
    data = gh_read_json(GH_PATH, default={}) or {}
    to_process = []
    for chat_id, rec in data.items():
        if rec.get("segment") != "new" or rec.get("phone") or rec.get("followed_up_24h"):
            continue
        first_seen = rec.get("first_seen")
        if not first_seen:
            continue
        try:
            seen_at = datetime.strptime(first_seen, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        except ValueError:
            continue
        if (now - seen_at).total_seconds() / 3600 < 24:
            continue
        to_process.append((chat_id, rec.get("name") or rec.get("call_name") or ""))

    if not to_process:
        return
    sent_ids = []
    for chat_id, name in to_process:
        hi = f"{name}, возвращаюсь к вам." if name else "Возвращаюсь к вам."
        text = (f"{hi} Самый простой шаг — посмотреть клуб вживую: "
                "20 минут, ни к чему не обязывает. Подобрать время?")
        r = api("sendMessage", chat_id=int(chat_id), text=text,
                reply_markup=kb([[("Подобрать время", "go")]]))
        if r.get("ok"):
            sent_ids.append(chat_id)
    if sent_ids:
        gh_update_json(GH_PATH,
            lambda d, ids=sent_ids: [d[i].__setitem__("followed_up_24h", True) for i in ids if i in d],
            f"tg-бот: догоняющее 24ч — {len(sent_ids)}")


def _daily_scheduler_loop():
    """Внутренний таймер вместо GitHub Actions schedule — тот, как выяснилось
    29.09, реально пропускает срабатывания часами (см. ЗАДАЧИ-ХВОСТ.md).
    Бот и так живёт непрерывно (self-ping), поэтому надёжнее держать
    время внутри самого процесса. Опрос раз в 5 минут достаточен: час
    попадания в окно (21:00 или 9:00) не пропустить, час — для почасовой
    догонялки."""
    time.sleep(30)  # дать процессу подняться, прежде чем читать GH
    _load_daily_marks()
    refresh_op_schedule()  # сразу при старте, не ждать первые 20 минут
    last_op_schedule_refresh = time.time()
    while True:
        try:
            if time.time() - last_op_schedule_refresh > 1200:
                refresh_op_schedule()
                last_op_schedule_refresh = time.time()
            now = datetime.now(MSK)
            today = now.strftime("%Y-%m-%d")
            hour_key = now.strftime("%Y-%m-%d-%H")
            if now.hour == 21 and _DAILY_MARKS["evening_prompt"] != today:
                send_evening_slot_prompt()
                _mark_daily_sent("evening_prompt", today)
            if now.hour == 9 and _DAILY_MARKS["morning_nag"] != today:
                send_morning_trainer_reminder()
                _mark_daily_sent("morning_nag", today)
            if now.hour == 9 and now.minute >= 30 and _DAILY_MARKS["coord_summary"] != today:
                send_coordinator_summary()
                _mark_daily_sent("coord_summary", today)
            if _DAILY_MARKS["followup_24h_hour"] != hour_key:
                send_24h_followups()
                _mark_daily_sent("followup_24h_hour", hour_key)
        except Exception as exc:
            print(f"[daily-scheduler] {exc}", flush=True)
        time.sleep(300)


_ACTIVE_MGR = {"data": None, "ts": 0}


def _active_mgr_data():
    """Расчёт графика ОП. До 30.09.2026 читалось из data/op_active_manager.json,
    который раз в 20 минут писал отдельный workflow op_schedule_sync.yml
    (sandow-lp) — просьба Ольги 30.09: работа бота не должна зависеть от
    GitHub Actions вовсе. Теперь бот качает и считает Google Таблицу сам
    (refresh_op_schedule, внутренний таймер), держит результат в памяти —
    читать из GitHub для этого больше не нужно, точкой отказа меньше."""
    return _ACTIVE_MGR["data"] or {}


def active_manager_name():
    """Кто сейчас «активный» менеджер — годится звонить прямо сейчас
    (отвалившаяся запись, подтверждение кнопкой). Для задачи на БУДУЩИЙ
    визит используйте manager_for_visit — иначе задачу получит тот, кто
    просто оказался активен в момент клика, а не тот, кто работает в
    день/час самого визита (расхождение поймала Ольга 29.09.2026)."""
    return _active_mgr_data().get("active_manager")


_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SHIFT_HOURS_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def resolve_visit_day(period, msk_now=None):
    """period — ключ дня недели (mon..sun) из кнопок step_time. Возвращает
    день месяца (int) ближайшего будущего (или сегодняшнего) вхождения
    этого дня недели, или None, если он проваливается в другой месяц —
    график месяца в op_active_manager.json содержит только текущий месяц,
    доверять числу дня из чужого месяца нельзя (может случайно совпасть
    с другим днём этого месяца)."""
    idx = _WEEKDAY_INDEX.get(period)
    if idx is None:
        return None
    now = msk_now or datetime.now(MSK)
    days_ahead = (idx - now.weekday()) % 7
    target = now.date() + timedelta(days=days_ahead)
    if target.month != now.month or target.year != now.year:
        return None
    return target.day


def resolve_visit_date(period, msk_now=None):
    """То же самое, что resolve_visit_day, но полной датой ГГГГ-ММ-ДД —
    формат, которым ключуется data/trainer_slots.json (не ограничен одним
    месяцем, в отличие от графика ОП, поэтому проверка на смену месяца
    здесь не нужна)."""
    idx = _WEEKDAY_INDEX.get(period)
    if idx is None:
        return None
    now = msk_now or datetime.now(MSK)
    days_ahead = (idx - now.weekday()) % 7
    return (now.date() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _rotate_manager(day_managers, hour):
    """Та же формула ротации, что в op_schedule.py и op_schedule_sync.yml —
    сознательно продублирована: бот не должен тянуть google-зависимости
    ради одной функции, а воркфлоу и так уже считает независимо от
    sandow-lead-bot (см. комментарий в op_schedule_sync.yml)."""
    working = []
    for name, rec in (day_managers or {}).items():
        status = (rec[0] if len(rec) > 0 else "") or ""
        hrs = (rec[1] if len(rec) > 1 else "") or ""
        if not str(status).lower().startswith("раб"):
            continue
        m = _SHIFT_HOURS_RE.match(str(hrs).strip())
        if not m:
            continue
        working.append((name, int(m.group(1)), int(m.group(2))))
    covering = [w for w in working if w[1] <= hour < w[2]]
    if not covering:
        return None
    if len(covering) == 1:
        return covering[0][0]
    win_start = min(w[1] for w in covering)
    win_end = max(w[2] for w in covering)
    span = max(1, win_end - win_start)
    block = span / len(covering)
    idx = min(len(covering) - 1, int((hour - win_start) / block))
    return covering[idx][0]


def manager_for_visit(target_day, target_hour):
    """Кто будет активным менеджером в день/час САМОГО ВИЗИТА, а не в
    момент, когда координатор нажал «подтвердить» (просьба Ольги 29.09.2026).
    target_day/target_hour отсутствуют (своё время текстом, старая запись
    без этих полей) или день вне графика — тихо возвращаем None, вызывающий
    откатывается на active_manager_name()."""
    if target_day is None or target_hour is None:
        return None
    schedule = _active_mgr_data().get("schedule") or {}
    day = schedule.get(str(target_day))
    if not day:
        return None
    return _rotate_manager(day, target_hour)


GOOGLE_SHEETS_SA_KEY = os.environ.get("GOOGLE_SHEETS_SA_KEY", "")
OP_SCHEDULE_SHEET_ID = "1B0PuXN-YuE1MFV5A95B8QtCT3dobipq7"
OP_SCHEDULE_SHEET_NAME = "График ОП"


def refresh_op_schedule():
    """Раньше — op_schedule_sync.yml (sandow-lp), отдельный workflow раз в
    20 минут. Перенесено внутрь бота 30.09.2026 по прямой просьбе Ольги:
    работа не должна зависеть от GitHub Actions вовсе — у schedule-триггера
    доказанные (29.09) многочасовые пропуски срабатываний. Бот качает и
    считает Google Таблицу сам через внутренний таймер (_daily_scheduler_loop),
    результат держит в памяти (_ACTIVE_MGR) — читать из GitHub для этого
    больше не нужно. Доступ — тот же служебный аккаунт Google, расшарен
    лично на адрес, не по публичной ссылке (решение Ольги 28.09: в таблице
    личные рабочие часы менеджеров)."""
    if not GOOGLE_SHEETS_SA_KEY:
        print("[op-schedule] GOOGLE_SHEETS_SA_KEY не задан — расчёт выключен", flush=True)
        return
    try:
        import io as _io
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        from openpyxl import load_workbook

        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SHEETS_SA_KEY),
            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        drive = build("drive", "v3", credentials=creds)
        buf = _io.BytesIO()
        dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=OP_SCHEDULE_SHEET_ID))
        done = False
        while not done:
            _, done = dl.next_chunk()

        wb = load_workbook(_io.BytesIO(buf.getvalue()), data_only=True)
        ws = wb[OP_SCHEDULE_SHEET_NAME]

        managers = []
        col = 3
        while col <= ws.max_column:
            nm = ws.cell(row=1, column=col).value
            if not nm:
                break
            managers.append((nm, col))
            col += 2

        schedule = {}
        for row in range(3, ws.max_row + 1):
            day = ws.cell(row=row, column=1).value
            if not isinstance(day, (int, float)):
                continue
            day_managers = {}
            for nm, c in managers:
                status = str(ws.cell(row=row, column=c).value or "").strip()
                hours = str(ws.cell(row=row, column=c + 1).value or "").strip()
                day_managers[nm] = [status, hours]
            schedule[str(int(day))] = day_managers

        now = datetime.now(MSK)
        today_rec = schedule.get(str(now.day), {})
        active = _rotate_manager(today_rec, now.hour)
        working_today = [nm for nm, (status, hours) in today_rec.items()
                          if status.lower().startswith("раб") and _SHIFT_HOURS_RE.match(hours.strip())]

        result = {
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
            "active_manager": active, "working_today": working_today,
            "computed_at": now.isoformat(), "schedule": schedule,
        }
        _ACTIVE_MGR["data"] = result
        _ACTIVE_MGR["ts"] = time.time()
        # Запись в GitHub — только для наглядности (посмотреть текущий расчёт
        # снаружи), сам бот её больше не читает. Провалится — не страшно,
        # активный менеджер всё равно посчитан и лежит в памяти.
        gh_write_json("data/op_active_manager.json", result,
                      f"график ОП: активный {active or '—'} ({now.strftime('%H:%M')})")
        print(f"[op-schedule] обновлено, активный менеджер: {active or '—'}", flush=True)
    except Exception as exc:
        print(f"[op-schedule] ошибка: {exc}", flush=True)


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
        # Правка Ольги 29.09 (разбор лидогенерации): в первом сообщении не
        # было оффера вообще — а решение «остаться или закрыть» человек
        # принимает в первые секунды. Условия подарка не раскрываем (правило).
        text = ("Здравствуйте! Это «Сандов Фитнес» на Нижегородской 🏆\n"
                "Сейчас у нас <b>год в подарок</b>, а первая тренировка "
                "с тренером — бесплатная.\n"
                "Подберу удобное время за минуту 👇")
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
              "Оставьте, пожалуйста, имя и номер телефона — сейчас у нас "
              "год в подарок, подберу для вас удобное время.\n\n"
              "Подтвержу здесь же, в этом чате.\n\n"
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
            send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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
    """Какие часы показать. Три разных набора (Ольга 02.10.2026):
    экскурсия — SLOTS_TOUR; подарочная тренировка новому гостю — SLOTS_TRAINING
    (рабочие часы 10:00–20:00); тренировка действующему члену клуба —
    SLOTS_MEMBER (07:00–22:00: у него доступ 24/7 и менеджер не нужен).
    Признак члена клуба — ключ member_training_already, он ставится нажатием
    «Записаться на тренировку» (bt:yes / bt:no) и больше нигде не появляется."""
    if fmt != "training":
        slots = SLOTS_TOUR
    elif "member_training_already" in (STATE.get(chat_id) or {}):
        slots = SLOTS_MEMBER
    else:
        slots = SLOTS_TRAINING
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

    # Раздел 11 сценария (правка Ольги 29.09): если на это время есть
    # свободные тренеры по календарю окон — координатор выбирает конкретного
    # тренера одним нажатием, видя картину, а не подтверждает вслепую.
    # Данных нет (тренер не отметился, «своё время» текстом) — старое
    # поведение, слепая кнопка «Подтвердить», координатор разбирается сама.
    visit_date, visit_slot = st.get("visit_date"), st.get("visit_slot")
    candidates = _free_trainers_for(visit_date, visit_slot) if fmt == "training" else []
    if candidates:
        tail = f"\nСвободны на {time_pref} — выберите тренера:"
        group_tail = dm_tail = tail
        markup = kb([[(nm, f"at:{chat_id}:{_TRAINERS.index(nm)}")]
                      for nm in candidates if nm in _TRAINERS])
    elif fmt == "training":
        # Тренировку с тренером назначает координатор зала — упоминаем её по
        # нику прямо в карточке (решение Ольги 28.09: проще прямого упоминания
        # в группе, чем городить отдельную личную рассылку через бота).
        group_tail = f"\n{COORDINATOR_TG} — подтвердите время клиенту одним нажатием:"
        dm_tail = "\nПодтвердите время клиенту одним нажатием:"
        markup = kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]])
    else:
        active = active_manager_name()
        who = f"{active} — вы активный менеджер сейчас, подтвердите" if active else "Менеджер — подтвердите"
        group_tail = dm_tail = f"\n{who} время клиенту одним нажатием:"
        markup = kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]])

    r = send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
                        text="\n".join(lines) + group_tail, reply_markup=markup)
    booking_mid = (r.get("result") or {}).get("message_id")
    if fmt == "training" and not is_live_test(chat_id):
        coord_id = coordinator_chat_id()
        if coord_id:
            api("sendMessage", chat_id=coord_id, parse_mode="HTML",
                text="\n".join(lines) + dm_tail, reply_markup=markup)

    with LOCK:
        STATE[chat_id] = {
            "segment": "new", "dir": direction, "fmt": fmt, "phone": phone,
            "time_pref": time_pref, "health": health,
            "booking_mid": booking_mid, "client_name": who_client,
            # Баг найден 29.09: раньше этот словарь целиком перезаписывался
            # и стирал visit_day/visit_hour, которые slot_chosen только что
            # сохранил — manager_for_visit из-за этого фактически не работал
            # с момента добавления. Теперь переносим явно.
            "visit_day": st.get("visit_day"), "visit_hour": st.get("visit_hour"),
            "visit_date": st.get("visit_date"), "visit_slot": st.get("visit_slot"),
        }
    extra = (f"Формат: {kind}. Время: {time_pref}."
             + (f" Особенности здоровья: {health}." if health else ""))
    if not is_live_test(chat_id):
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

    visit_date, visit_slot = st.get("visit_date"), st.get("visit_slot")
    candidates = _free_trainers_for(visit_date, visit_slot)
    if candidates:
        tail = f"\n\nСвободны на {time_pref} — выберите тренера:"
        group_tail = dm_tail = tail
        markup = kb([[(nm, f"at:{chat_id}:{_TRAINERS.index(nm)}")]
                      for nm in candidates if nm in _TRAINERS])
    else:
        group_tail = f"\n\n{COORDINATOR_TG} — подтвердите время клиенту одним нажатием:"
        dm_tail = "\n\nПодтвердите время клиенту одним нажатием:"
        markup = kb([[("✅ Подтвердить время", f"confirmvisit:{chat_id}")]])

    r = send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
        text=f"{card}{group_tail}", reply_markup=markup)
    booking_mid = (r.get("result") or {}).get("message_id")
    if not is_live_test(chat_id):
        coord_id = coordinator_chat_id()
        if coord_id:
            api("sendMessage", chat_id=coord_id, parse_mode="HTML",
                text=f"{card}{dm_tail}", reply_markup=markup)
    with LOCK:
        STATE[chat_id] = {
            "segment": "member", "fmt": "training", "time_pref": time_pref,
            "booking_mid": booking_mid, "client_name": who_client,
            "visit_day": st.get("visit_day"), "visit_hour": st.get("visit_hour"),
            "visit_date": st.get("visit_date"), "visit_slot": st.get("visit_slot"),
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
        # Просьба Ольги 29.09: запоминаем день/час САМОГО ВИЗИТА, пока они
        # ещё структурированы (кнопки), чтобы задача менеджеру потом ушла
        # тому, кто работает именно тогда, а не тому, кто активен в момент
        # клика «подтвердить» (manager_for_visit в coordinator_confirm).
        st["visit_day"] = resolve_visit_day(period)
        st["visit_hour"] = int(hhmm.split(":")[0])
        st["visit_date"] = resolve_visit_date(period)
        st["visit_slot"] = hhmm
        member_flow = "member_training_already" in st
        snapshot = dict(st)
    if member_flow:
        return finalize_member_training(chat_id, message_id, user, snapshot)
    finalize_booking(chat_id, message_id, user, snapshot)


def coordinator_confirm(group_chat_id, message_id, target_chat_id, who, trainer_name=None):
    """Координатор/менеджер нажал «Подтвердить время» (или выбрал конкретного
    тренера, раздел 11) в группе — клиенту уходит финальное подтверждение
    (раздел 2.5), адрес — ссылкой на карту (геометки и фото входа на старте
    нет: нет готового файла и координат — честно заменено ссылкой, не
    выдумано). trainer_name задан — координатор выбрала тренера из списка
    свободных; не задан — старое поведение (слепая кнопка, координатор
    разбирается сама, кто из тренеров свободен)."""
    with LOCK:
        st = STATE.get(target_chat_id, {})
        fmt = st.get("fmt", "training")
        time_pref = st.get("time_pref", "")
        name = st.get("client_name", "")
        phone = st.get("phone", "")
        health = st.get("health", "")
        direction = st.get("dir", "any")
        is_member = st.get("segment") == "member"
        visit_day = st.get("visit_day")
        visit_hour = st.get("visit_hour")
        visit_date = st.get("visit_date")
        visit_slot = st.get("visit_slot")
    maps_url = "https://yandex.ru/maps/?text=" + _url_quote(f"Москва, {CLUB}")
    hi = f"Подтверждаю, {name}" if name else "Подтверждаю"
    if is_member:
        # Действующий член клуба — у неё уже есть браслет и доступ, паспорт
        # и адрес не нужны (это только для гостя, правка 28.09).
        expert_line = trainer_name if trainer_name else "Фитнес-эксперт"
        text = (f"{hi}: {time_pref}. {expert_line} уже знает о встрече, "
                "ждём вас! До скорой связи!")
    elif fmt == "training":
        # Имя тренера — приложением через тире, чтобы не зависеть от
        # склонения («с Дарья Салихова» грамматически неверно, а «с
        # фитнес-экспертом — это будет Дарья Салихова» верно при любом имени).
        expert_line = (f"Вас встретит менеджер и познакомит с фитнес-экспертом"
                        + (f" — это будет {trainer_name}" if trainer_name else "") + ". Накануне напомню!")
        text = (f"{hi}: {time_pref}. Возьмите спортивную форму, кроссовки и "
                f"паспорт — он нужен для оформления гостевого визита.\n"
                f"Адрес: Москва, {CLUB}. Маршрут: {maps_url}\n"
                f"{expert_line}")
    else:
        text = (f"{hi}: {time_pref}. Возьмите с собой паспорт — он нужен для "
                f"оформления визита.\nАдрес: Москва, {CLUB}. Маршрут: {maps_url}\n"
                "Вас встретит менеджер. Накануне напомню. До встречи!")
    # Раздел 11 (правка 29.09): выбранный тренер — закрываем его слот в
    # календаре окон гостем и шлём ему карточку клиента с кнопками статуса
    # ДО сообщения клиенту «{тренер} уже знает о встрече» — иначе клиент
    # узнаёт об этом раньше самого тренера (замечание content-compliance-
    # critic 29.09, доли секунды разницы, но раз дёшево починить — чиним).
    # is_live_test — иначе живой тест Ольги реально забронирует слот
    # настоящего тренера и пришлёт ему карточку выдуманного гостя (та же
    # защита, что и для рабочей группы/координатора/1С, v35).
    if trainer_name and visit_date and visit_slot and not is_live_test(target_chat_id):
        guest = name or "без имени"

        def _book(d, date=visit_date, slot=visit_slot, tname=trainer_name, guest=guest):
            day = d.setdefault("days", {}).setdefault(date, {})
            rec = day.setdefault(tname, {"free": [], "day_off": False, "booked": {}})
            if slot in rec.get("free", []):
                rec["free"].remove(slot)
            rec.setdefault("booked", {})[slot] = guest
        gh_update_json(TRAINER_SLOTS_PATH, _book, f"{trainer_name}: гость {guest} {visit_date} {visit_slot}")

        t_chat = trainer_chat_id_for(trainer_name)
        if t_chat:
            card = [f"🏋️ <b>Новый гость — {time_pref}</b>", f"<b>Имя:</b> {guest}"]
            if phone:
                card.append(f"<b>Телефон:</b> <code>{phone}</code>")
            if health:
                card.append(f"<b>Особенности здоровья:</b> {health}")
            card.append("\nПосле контакта с гостем отметьте статус:")
            api("sendMessage", chat_id=t_chat, parse_mode="HTML", text="\n".join(card),
                reply_markup=kb([[("Связался, подтвердил", f"tstatus:{target_chat_id}:ok"),
                                   ("Перенёс", f"tstatus:{target_chat_id}:moved")]]))

    api("sendMessage", chat_id=target_chat_id, text=text)
    who_name = who.get("first_name", "менеджер")
    confirmed_label = f"✅ Подтверждено: {who_name}" + (f" → {trainer_name}" if trainer_name else "")
    api("editMessageReplyMarkup", chat_id=group_chat_id, message_id=message_id,
        reply_markup=kb([[(confirmed_label, "noop")]]))

    # Правка Ольги 28.09: после подтверждения — явная задача дежурному
    # менеджеру в чат заявок (не в 1С — там задачи ставить нельзя, API
    # закрыт, проверено ранее; заявка туда и так уже падает через
    # send_to_1c/push_1c_followup, менеджер сам заводит себе задачу по
    # этому сообщению, как и раньше).
    if not is_member:
        # Просьба Ольги 29.09: задача на визит должна называть того, кто
        # работает в день/час САМОГО визита, а не того, кто активен сейчас
        # (координатор мог нажать «подтвердить» на следующий день после
        # записи). Известен день/час визита (кнопки, не «своё время»
        # текстом) — считаем по графику визита; иначе откат на «сейчас».
        active = manager_for_visit(visit_day, visit_hour) or active_manager_name()
        kind = "Тренировка с тренером" if fmt == "training" else "Экскурсия"
        who_line = f"Менеджер {active}" if active else "Дежурный менеджер"
        # Видимость для менеджера ОП (раздел 11, добавлено Ольгой 27.09):
        # имя назначенного фитнес-эксперта — в каждом уведомлении.
        expert_note = f"\n<b>Тренер:</b> {trainer_name}" if trainer_name else ""
        send_to_orders(subject_chat_id=target_chat_id, parse_mode="HTML",
            text=(f"📋 <b>ЗАДАЧА: {kind.upper()} НАЗНАЧЕНА</b>\n"
                  f"{who_line} — {time_pref}.{expert_note}\n"
                  f"<b>Клиент:</b> {name or 'без имени'}"
                  + (f" · <code>{phone}</code>" if phone else "") + "\n"
                  f"<b>Направление:</b> {DIRS.get(direction, DIRS['any'])[0]}\n"
                  "Заявка уже в 1С — поставьте себе задачу на встречу."))


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
            send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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
                send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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
        if not (ONEC_WEBHOOK and phone) or is_live_test(chat_id):
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
    send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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
    if is_live_test(user.get("id")):
        return "\n\n🧪 ТЕСТ — в 1С не отправлено"

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
    r = send_to_orders(subject_chat_id=user.get("id"), text=text, parse_mode="HTML",
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

    if data.startswith("trainer_pick:") or data.startswith("trainer_confirm:"):
        idx = int(data.split(":", 1)[1])
        if not (0 <= idx < len(_TRAINERS)):
            return
        name = _TRAINERS[idx]
        if data.startswith("trainer_pick:"):
            existing_data = gh_read_json(TRAINER_GH_PATH, default={}) or {}
            taken_by = next((cid for cid, rec in existing_data.items()
                              if rec.get("name") == name and cid != str(chat_id)), None)
            if taken_by:
                return api("editMessageText", chat_id=chat_id, message_id=mid,
                    text=f"Имя «{name}» уже зарегистрировано другим аккаунтом. Это точно вы?",
                    reply_markup=kb([[("Да, это я", f"trainer_confirm:{idx}"),
                                       ("Нет, я другой", "trainer_register_again")]]))
        register_trainer(chat_id, user, name)
        return api("editMessageText", chat_id=chat_id, message_id=mid,
            text=f"Готово, {name}! Каждый вечер в 21:00 буду спрашивать свободные "
                 "окна на завтра — пара кнопок, минута в день.")

    if data == "trainer_register_again":
        return step_trainer_register(chat_id, user)

    if data.startswith("tslot:"):
        _, date, slot = data.split(":", 2)
        name = trainer_name_for(chat_id)
        if not name:
            return

        def _toggle(d, date=date, slot=slot, name=name):
            day = d.setdefault("days", {}).setdefault(date, {})
            rec = day.setdefault(name, {"free": [], "day_off": False, "booked": {}})
            rec["day_off"] = False
            if slot in rec.get("booked", {}):
                return  # уже занят гостем — трогать нельзя
            if slot in rec["free"]:
                rec["free"].remove(slot)
            else:
                rec["free"].append(slot)
        gh_update_json(TRAINER_SLOTS_PATH, _toggle, f"{name}: слот {slot} {date}")
        return api("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
            reply_markup=kb(render_trainer_slot_kb(date, name)))

    if data.startswith("tslot_off:"):
        date = data.split(":", 1)[1]
        name = trainer_name_for(chat_id)
        if not name:
            return

        def _off(d, date=date, name=name):
            day = d.setdefault("days", {}).setdefault(date, {})
            rec = day.setdefault(name, {"free": [], "day_off": False, "booked": {}})
            rec["day_off"] = not rec.get("day_off", False)
            if rec["day_off"]:
                rec["free"] = []
        gh_update_json(TRAINER_SLOTS_PATH, _off, f"{name}: весь день {date}")
        return api("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
            reply_markup=kb(render_trainer_slot_kb(date, name)))

    if data.startswith("tslot_week:"):
        date = data.split(":", 1)[1]
        name = trainer_name_for(chat_id)
        if not name:
            return

        def _week(d, date=date, name=name):
            base = datetime.strptime(date, "%Y-%m-%d")
            source = d.get("days", {}).get(date, {}).get(
                name, {"free": [], "day_off": False})
            for i in range(7):
                dd = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                day = d.setdefault("days", {}).setdefault(dd, {})
                prev = day.get(name, {})
                day[name] = {
                    "free": list(source.get("free", [])),
                    "day_off": source.get("day_off", False),
                    "booked": prev.get("booked", {}),
                    "week_ahead": True,
                }
        gh_update_json(TRAINER_SLOTS_PATH, _week, f"{name}: на неделю вперёд с {date}")
        return api("sendMessage", chat_id=chat_id,
            text="Готово — эти окна проставлены на 7 дней вперёд. Ежедневных "
                 "напоминаний не будет, пока не напишете иначе.")

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
        send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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

    if data.startswith("at:"):
        _, target_s, idx_s = data.split(":", 2)
        target, idx = int(target_s), int(idx_s)
        if not (0 <= idx < len(_TRAINERS)):
            return
        return coordinator_confirm(chat_id, mid, target, user, trainer_name=_TRAINERS[idx])

    if data.startswith("tstatus:"):
        _, target_s, status = data.split(":", 2)
        target = int(target_s)
        trainer_name = trainer_name_for(chat_id) or "Тренер"
        label = {"ok": "связался(-лась), подтвердил(а) гостю время",
                  "moved": "перенёс(ла) встречу — уточните новое время"}.get(status, status)
        send_to_orders(subject_chat_id=target, parse_mode="HTML",
            text=f"👤 <b>{trainer_name}</b>: {label} (#id{target})")
        return api("editMessageReplyMarkup", chat_id=chat_id, message_id=mid, reply_markup=kb([]))

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
        photo_caption = (f"🧾 <b>СПРАВКА ДЛЯ ВЫЧЕТА — фото паспорта</b>\n{member_card_line(user)}\n"
                          f"Когда справка готова, ответьте клиенту реплаем на это сообщение.\n"
                          f"#id{chat_id}")
        if is_live_test(chat_id):
            api("sendPhoto", chat_id=chat_id, photo=file_id, parse_mode="HTML",
                caption="🧪 ТЕСТ (в рабочую группу не отправлено):\n\n" + photo_caption)
        else:
            api("sendPhoto", chat_id=ORDERS_CHAT, photo=file_id, parse_mode="HTML",
                caption=photo_caption)
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
        send_to_orders(subject_chat_id=chat_id, parse_mode="HTML",
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

        # Раздел 11 сценария: ссылка «t.me/sandowclub_bot?start=trainer» —
        # её постит Ольга в общий чат тренеров. Отдельная от клиентской
        # цепочки развилка: тренер выбирает своё имя один раз, дальше бот
        # узнаёт его по chat_id сам (правка 29.09).
        if src == "trainer":
            with LOCK:
                STATE.pop(chat_id, None)
            return step_trainer_register(chat_id, user)

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
            send_to_orders(subject_chat_id=chat_id, text=note, parse_mode="HTML", reply_to_message_id=lead_mid)
        else:
            send_to_orders(subject_chat_id=chat_id, text=note, parse_mode="HTML")
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
VERSION = "2026-10-02-v43-member-slots-7-22"


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
threading.Thread(target=_daily_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
