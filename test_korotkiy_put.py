# -*- coding: utf-8 -*-
"""Проверка короткого пути к номеру (сценарий 22.11.2026, версия v109).

Что здесь проверяется — именно то, что человек проходит до заявки:

  1. Кнопка «Хочу попробовать» ведёт сразу на второй экран с подарком и
     экскурсией, без вопросов о цели и направлении до номера.
  2. Номер приняли — спрашивается формат: тренировка или экскурсия.
  3. Тренировка: задача → направление (зал / бокс / не решил) → прежний
     календарь дней и часов, без сужения до трёх вариантов.
  4. Час выбран — заявка уходит в 1С с источником, а карточка в чат заявок,
     в карточке остаётся напоминание уточнить здоровье при звонке.
  5. Экскурсия не запускает подбор тренера и спрашивает интересы необязательно.

Сеть и хранилище подменяются: ни одной настоящей заявки не уходит.
Запуск: python test_korotkiy_put.py
"""
import copy
import json
import os
from datetime import datetime

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:test")
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ.setdefault("ONEC_WEBHOOK", "")

import bot  # noqa: E402

ПОНЕДЕЛЬНИК = datetime(2026, 10, 12, 12, 0, tzinfo=bot.MSK)
ХРАНИЛИЩЕ = {}
ОТПРАВЛЕНО = []
В_1С = []
КАРТОЧКИ = []


def фейк_gh_read(path, default=None):
    данные = ХРАНИЛИЩЕ.get(path)
    return copy.deepcopy(данные) if данные is not None else default


def фейк_gh_write(path, data, message):
    ХРАНИЛИЩЕ[path] = copy.deepcopy(data)


def фейк_gh_update(path, mutate, message, attempts=5):
    данные = copy.deepcopy(ХРАНИЛИЩЕ[path]) if path in ХРАНИЛИЩЕ else {}
    mutate(данные)
    ХРАНИЛИЩЕ[path] = данные
    return данные


def фейк_api(method, **kw):
    ОТПРАВЛЕНО.append((method, kw))
    return {"ok": True, "result": {"message_id": 1}}


def фейк_send_to_orders(subject_chat_id=None, subject_phone=None, **payload):
    КАРТОЧКИ.append(payload)
    return {"ok": True, "result": {"message_id": 1}}


bot.api = фейк_api
bot.gh_read_json = фейк_gh_read
bot.gh_write_json = фейк_gh_write
bot.gh_update_json = фейк_gh_update
bot.save_subscriber = lambda *a, **k: None
bot.archive_dialog = lambda *a, **k: None
bot.log_lead_event = lambda *a, **k: None
bot.send_to_orders = фейк_send_to_orders
bot.send_to_1c = lambda user, phone, goal, direction, source="", метки=None, fmt="": \
    В_1С.append({"phone": phone, "source": source, "fmt": fmt}) or ""
bot.push_1c_followup = lambda *a, **k: None
bot.schedule_dropoff_watch = lambda *a, **k: None
bot.too_soon = lambda uid: False
bot.is_live_test = lambda chat_id: False
bot._now = lambda now=None: now or ПОНЕДЕЛЬНИК
# Подбор тренера в этой проверке не участвует: она про путь клиента и карточку.
bot.авто_тренер = lambda *a, **k: {"тренер": None, "причина": "нет_окон",
                                   "разбор": {}, "годные": [], "альтернативы": []}
bot._free_trainers_for = lambda *a, **k: []
bot.coordinator_chat_id = lambda: None
bot.bookable_dates = lambda fmt, chat_id=None: ["2026-10-12", "2026-10-13"]
bot.slots_on = lambda iso, fmt, chat_id=None: ["10:00", "11:00", "12:00"]

ЧАТ = 777
провалов = 0


def проверить(условие, описание):
    global провалов
    print(('  ОК   ' if условие else '  СБОЙ ') + описание)
    if not условие:
        провалов += 1


def нажать(data, message_id=5, чат=None):
    ОТПРАВЛЕНО.clear()
    bot.on_button({"id": "cb", "data": data,
                   "message": {"chat": {"id": чат or ЧАТ, "type": "private"},
                               "message_id": message_id},
                   "from": {"id": чат or ЧАТ, "first_name": "Тест"}})


def последнее():
    for method, kw in reversed(ОТПРАВЛЕНО):
        if method in ("sendMessage", "editMessageText"):
            return kw
    return {}


def подписи(markup):
    return [b["text"] for ряд in (markup or {}).get("inline_keyboard", []) for b in ряд]


def данные(markup):
    return [b.get("callback_data") for ряд in (markup or {}).get("inline_keyboard", []) for b in ряд]


print('1. «Хочу попробовать» — второй экран с выбором и согласием')
bot.STATE.clear()
ОТПРАВЛЕНО.clear()
нажать("seg:new")
т = последнее().get("text", "")
kb = последнее().get("reply_markup", {})
# 22.11.2026, тексты Ольги дословно: экран говорит про бесплатную тренировку и
# план, про зал 1100 м² и предлагает выбрать вариант двумя кнопками.
проверить("Отличный выбор" in т, 'экран начинается с «Отличный выбор»')
проверить("платить не нужно" in т, 'сказано, что платить не нужно')
проверить("1100" in т and "не стоим" in т, 'сказано про зал 1100 м² без очереди')
проверить("обработку персональных данных" in т, 'согласие внизу экрана')
проверить({"dr:training", "dr:tour"} <= set(данные(kb)), 'две кнопки: тренировка и зал')
подписи_экрана = подписи(kb)
проверить("Забронировать тренировку" in подписи_экрана, 'кнопка «Забронировать тренировку»')
проверить("Посмотреть зал" in подписи_экрана, 'кнопка «Посмотреть зал»')
проверить(bot.STATE[ЧАТ].get("direct_flow") is True, 'человек идёт по новой ветке')

print('2. Нажал «Забронировать тренировку» — просят номер, согласие на месте')
нажать("dr:training")
т = последнее().get("text", "")
kb = последнее().get("reply_markup", {})
проверить("Оставьте номер" in т, 'просят номер сразу после выбора')
проверить("обработку персональных данных" in т, 'согласие стоит там, где отправляют контакт')
проверить(kb.get("keyboard") and kb["keyboard"][0][0].get("request_contact") is True,
          'клавиатура «Отправить мой номер» одним нажатием')
проверить(bot.STATE[ЧАТ].get("fmt") == "training", 'формат записан по нажатой кнопке')

print('3. Номер приняли — сразу задача, вопрос о здоровье не задаётся')
ОТПРАВЛЕНО.clear()
bot.finish(ЧАТ, {"id": ЧАТ, "first_name": "Тест"}, "+7 916 123-45-67")
т = последнее().get("text", "")
проверить("здоров" not in т.lower() and "давление" not in т.lower(),
          'клиенту не задают вопрос про здоровье')
проверить("главное" in т, 'спрошена задача словами, а не анкетой')
проверить(any("79161234567" in "".join(ch for ch in str(з["phone"]) if ch.isdigit())
              for з in В_1С), 'заявка ушла в 1С с номером гостя')

print('3а. Задача, направление, прежний календарь')
нажать("drg:shape")
проверить("Где хотите попробовать" in последнее().get("text", ""), 'спрошено направление')
нажать("drd:gym")
kb = последнее().get("reply_markup", {})
дни = [x for x in данные(kb) if str(x).startswith("tp:")]
проверить(len(дни) == 2, f'показан прежний календарь дней (получено {len(дни)})')
проверить(bot.STATE[ЧАТ].get("goal") == "shape", 'задача записана в состояние')
проверить(bot.STATE[ЧАТ].get("dir") == "gym", 'направление записано в состояние')
проверить(all(x == "drd:any" or not str(x).startswith("d2:") for x in данные(kb)),
          'в новой ветке старых кнопок направления (d2:) нет')
нажать(дни[0])
kb = последнее().get("reply_markup", {})
слоты = [x for x in данные(kb) if str(x).startswith("sl:")]
проверить(len(слоты) == 3, f'показаны часы выбранного дня (получено {len(слоты)})')

print('4. Час выбран — карточка и заявка доходят до менеджеров')
ОТПРАВЛЕНО.clear()
КАРТОЧКИ.clear()
bot.on_button({"id": "cb", "data": слоты[0],
               "message": {"chat": {"id": ЧАТ, "type": "private"}, "message_id": 6},
               "from": {"id": ЧАТ, "first_name": "Тест"}})
всё = " ".join(str(к.get("text") or "") for к in КАРТОЧКИ)
проверить(bool(КАРТОЧКИ), 'карточка записи ушла в чат заявок')
проверить("ЗАПИСЬ" in всё and "ТРЕНИРОВКА" in всё, 'в карточке сказано, что это тренировка')
цифры = "".join(ch for ch in всё if ch.isdigit())
проверить("79161234567" in цифры, 'в карточке есть телефон гостя')
проверить("Цель" in всё, 'в карточке есть цель, собранная в боте')
проверить("Направление" in всё, 'в карточке есть направление')
проверить("Уточнить при звонке" in всё, 'в карточке есть строка «Уточнить при звонке»')
проверить("спина, суставы, давление" in всё, 'напоминание называет, что уточнить')
# 05.11.2026, правка Ольги: тренировку подтверждает координатор зала, а не
# менеджер отдела продаж. Поэтому в карточке группы кнопки подтверждения нет —
# она уходит координатору в личку, менеджеру остаётся «Беру в работу».
проверить("координатор" in всё.lower(), 'сказано, кто подтверждает время')
коды = [b.get("callback_data") for к in КАРТОЧКИ
        for ряд in ((к.get("reply_markup") or {}).get("inline_keyboard") or [])
        for b in ряд]
проверить(not any(str(x).startswith("confirmvisit:") for x in коды),
          'менеджеру тренировку подтверждать не предлагают')
проверить(any(str(x).startswith("take:") for x in коды), 'на карточке есть «Беру в работу»')

print('5. Экскурсия: без подбора тренера, интересы необязательны')
ЭКСКУРСИЯ = 888
bot.STATE.pop(ЭКСКУРСИЯ, None)
ОТПРАВЛЕНО.clear()
bot.step_quick_offer(ЭКСКУРСИЯ)
bot.finish(ЭКСКУРСИЯ, {"id": ЭКСКУРСИЯ, "first_name": "Гость"}, "+7 916 000-11-22")
подборов = {"n": 0}
настоящий = bot.авто_тренер
bot.авто_тренер = lambda *a, **k: подборов.__setitem__("n", подборов["n"] + 1) or настоящий(*a, **k)
нажать("dr:tour", message_id=7, чат=ЭКСКУРСИЯ)
т = последнее().get("text", "")
kb = последнее().get("reply_markup", {})
проверить("первую очередь" in т, 'спрошены интересы экскурсии')
проверить("dri:skip" in данные(kb), 'есть «Пока не знаю — продолжить»')
нажать("dri:skip", message_id=8, чат=ЭКСКУРСИЯ)
проверить("познакомиться с клубом" in последнее().get("text", ""),
          'без интересов путь продолжается к выбору дня')
проверить(подборов["n"] == 0, 'на экскурсии тренера не подбирают')

print('6. Пустой календарь не ломает путь')
bot.slots_on = lambda iso, fmt, chat_id=None: []
bot.bookable_dates = lambda fmt, chat_id=None: []
ОТПРАВЛЕНО.clear()
bot.step_time(ЧАТ, "training")
проверить(ОТПРАВЛЕНО, 'при пустом календаре бот не молчит')

print(f"\nПРОВАЛОВ: {провалов}")
raise SystemExit(1 if провалов else 0)
