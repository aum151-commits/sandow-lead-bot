"""Summarize an exported tg_funnel.json without network access or personal data.

Usage: python report_direct_response.py path/to/tg_funnel.json --since 2026-11-22
The cohort is selected by its first offer timestamp, in Moscow time.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))


def _timestamp(value):
    try:
        result = datetime.fromisoformat(str(value))
        return result.replace(tzinfo=MOSCOW) if result.tzinfo is None else result.astimezone(MOSCOW)
    except (ValueError, TypeError):
        return None


def summarize(data, since=None, until=None):
    """Count unique offered users; missing/unknown attendance is never inferred."""
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    counts = {key: 0 for key in ("offered", "phone", "training", "tour", "booked", "confirmed",
                                  "followup", "phone_after_followup", "booked_after_followup")}
    by_format = {name: {key: 0 for key in ("selected", "booked", "confirmed")}
                 for name in ("training", "tour")}
    start_cohort = {"started": 0, "offered": 0, "phone": 0}
    for rec in data.values():
        if not isinstance(rec, dict):
            continue
        state = rec.get("state") or {}
        if not isinstance(state, dict):
            continue
        events = rec.get("events") or []
        if not isinstance(events, list):
            continue
        events = [event for event in events if isinstance(event, dict)]
        if (state.get("segment") == "member" or rec.get("closed") == "member" or
                rec.get("test") or state.get("test") or
                any(event.get("event") == "member" for event in events)):
            continue
        times = {}
        for event in events:
            name, at = event.get("event"), _timestamp(event.get("at"))
            if at and isinstance(name, str) and (name not in times or at < times[name]):
                times[name] = at
        started = times.get("start")
        if started and not (since and started < since) and not (until and started >= until):
            start_cohort["started"] += 1
            start_cohort["offered"] += int("offer" in times)
            start_cohort["phone"] += int("phone" in times)
        offered = times.get("offer")
        if not offered or (since and offered < since) or (until and offered >= until):
            continue
        counts["offered"] += 1
        counts["phone"] += int("phone" in times)
        # A saved booking is not proof of attendance or purchase.
        booked = "booking" in times or "completed" in times
        confirmed = "confirmed" in times
        counts["booked"] += int(booked)
        counts["confirmed"] += int(confirmed)
        sent = times.get("followup_sent") or times.get("followup")
        booked_at = times.get("booking") or times.get("completed")
        counts["followup"] += int(sent is not None)
        counts["phone_after_followup"] += int(bool(sent and times.get("phone") and times["phone"] > sent))
        counts["booked_after_followup"] += int(bool(sent and booked_at and booked_at > sent))
        fmt = state.get("fmt") if state.get("direct_selected_format") else None
        if fmt not in by_format:
            choices = [e for e in events if e.get("event") == "format" and e.get("value") in by_format]
            if choices:
                fmt = choices[-1]["value"]
        if fmt in by_format:
            counts[fmt] += 1
            by_format[fmt]["selected"] += 1
            by_format[fmt]["booked"] += int(booked)
            by_format[fmt]["confirmed"] += int(confirmed)
    return {"counts": counts, "by_format": by_format, "start_cohort": start_cohort}


def render(summary):
    c = summary["counts"]
    def rate(numerator, denominator):
        return f"{100 * numerator / denominator:.1f}%" if denominator else "нет данных"
    s = summary["start_cohort"]
    lines = ["Гости с первым запуском в выбранном периоде (кроме выявленных членов клуба):",
             f"Запустили бота: {s['started']}",
             f"Дошли до предложения: {s['offered']}",
             f"Оставили номер: {s['phone']} ({rate(s['phone'], s['started'])})",
             "", "Гости с первым показом предложения в выбранном периоде:",
             f"Увидели предложение: {c['offered']}",
             f"Оставили номер: {c['phone']} ({rate(c['phone'], c['offered'])})",
             f"Завершили запись: {c['booked']}",
             f"Получили подтверждение: {c['confirmed']}",
             f"Отправлено возвратных сообщений: {c['followup']}",
             f"Оставили номер после сообщения: {c['phone_after_followup']}",
             f"Завершили запись после сообщения: {c['booked_after_followup']}"]
    for key, label in (("training", "Тренировка"), ("tour", "Экскурсия")):
        row = summary["by_format"][key]
        lines.append(f"{label}: выбрали {row['selected']}, записались {row['booked']}, подтверждено {row['confirmed']}")
    lines += ["После сообщения — последовательность по времени, не доказательство его влияния.",
              "Визиты и покупки: не определяются по переписке и подтверждениям.",
              "Нажатия мессенджера требуют отдельного сопоставления с сайтом; это не запуски бота."]
    return "\n".join(lines)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сводка по сохранённым шагам бота, без доступа к сети",
                                     add_help=False)
    parser._positionals.title = "Входной файл"
    parser._optionals.title = "Параметры"
    parser.add_argument("-h", "--help", action="help", help="Показать справку")
    parser.add_argument("input", type=Path, help="Выгрузка tg_funnel.json")
    parser.add_argument("--since", help="Начало периода по первому предложению, ГГГГ-ММ-ДД")
    parser.add_argument("--until", help="Конец периода, не включительно, ГГГГ-ММ-ДД")
    args = parser.parse_args()
    try:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=MOSCOW) if args.since else None
        until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=MOSCOW) if args.until else None
        if since and until and until <= since:
            parser.error("Конец периода должен быть позже начала")
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
        print(render(summarize(data, since, until)))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Не удалось собрать сводку: {exc}\n")


if __name__ == "__main__":
    main()
