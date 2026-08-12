# -*- coding: utf-8 -*-
"""Проверка готовности связки с 1С:Фитнес клуб.

Запускать, когда придёт ключ приложения. Скрипт проверит доступ, покажет
идентификаторы клубов и по флагу создаст тестовую заявку.

    python check_1c.py --apikey <ключ>
    python check_1c.py --apikey <ключ> --test-lead 79990000000
"""
import argparse
import json
import os
import re
import sys

import requests

ENV = r"D:\Проекты\yandex-business-automation\.env"
BASE = "https://app.1c.fitness/fitnessapi/hs/api/v3"


def creds():
    login = password = None
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            m = re.match(r"\s*FITNESS_1C_LOGIN\s*=\s*(\S+)", line)
            if m:
                login = m.group(1)
            m = re.match(r"\s*FITNESS_1C_PASSWORD\s*=\s*(\S+)", line)
            if m:
                password = m.group(1)
    return login, password


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apikey", required=True, help="ключ приложения из 1С")
    ap.add_argument("--login", help="логин 1С, если не из .env")
    ap.add_argument("--password", help="пароль 1С, если не из .env")
    ap.add_argument("--test-lead", help="создать тестовую заявку на этот номер")
    a = ap.parse_args()

    login = a.login or creds()[0]
    password = a.password or creds()[1]
    if not (login and password):
        print("не нашёл логин и пароль 1С")
        return 1
    auth = (login, password)
    head = {"apikey": a.apikey}
    print(f"учётка: {login}")

    # 1. доступ и список клубов
    r = requests.get(f"{BASE}/clubs/", auth=auth, headers=head, timeout=40)
    print(f"\nclubs: HTTP {r.status_code}")
    if r.status_code == 401:
        print("  доступа нет. Проверьте ключ и роли пользователя:")
        print("  «Удаленный доступ внешнее приложение», «Запуск внешнего соединения»")
        return 1
    try:
        data = r.json()
        print(json.dumps(data, ensure_ascii=False, indent=1)[:1200])
        for c in (data.get("data") or {}).get("clubs", []) or []:
            print(f"  КЛУБ: {c.get('name')} -> club_id = {c.get('id') or c.get('club_id')}")
    except Exception:
        print(r.text[:400])

    # 2. тестовая заявка
    if a.test_lead:
        body = {
            "phone": re.sub(r"\D", "", a.test_lead),
            "name": "Проверка связи",
            "comment": "Тестовая заявка из скрипта проверки. Можно удалить.",
            "marketing": {"source": "Telegram-бот, сайт",
                          "utm_source": "telegram", "utm_medium": "bot"},
        }
        r = requests.post(f"{BASE}/lead/", json=body, auth=auth, headers=head, timeout=40)
        print(f"\nlead: HTTP {r.status_code}")
        print(r.text[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
