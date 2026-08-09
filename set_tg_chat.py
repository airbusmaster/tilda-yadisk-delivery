#!/usr/bin/env python3
"""Определяет chat_id владельца и вписывает его в config.env.

Порядок: вписать TG_BOT_TOKEN в config.env -> написать боту любое
сообщение в Telegram -> запустить этот скрипт.
Токен читается из конфига и никуда не выводится.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "config.env"


def read_cfg():
    cfg = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = read_cfg()
    token = cfg.get("TG_BOT_TOKEN", "")
    if not token:
        sys.exit("TG_BOT_TOKEN не заполнен в config.env")

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    data = json.load(urllib.request.urlopen(url, timeout=20))
    if not data.get("ok"):
        sys.exit(f"Telegram отверг токен: {data.get('description')}")

    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            chats[chat["id"]] = who

    if not chats:
        sys.exit("Обновлений нет — напишите боту любое сообщение в Telegram и повторите")

    chat_id, who = list(chats.items())[-1]
    text = CONFIG.read_text(encoding="utf-8")
    text = re.sub(r"^TG_CHAT_ID=.*$", f"TG_CHAT_ID={chat_id}", text, flags=re.M)
    CONFIG.write_text(text, encoding="utf-8")
    print(f"Записан TG_CHAT_ID={chat_id} ({who})")

    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": "Бот подключён к автовыдаче цифровых товаров. "
                    "Сюда буду присылать отчёты об отправленных заказах и алерты.",
        }).encode(), timeout=20,
    ).read()
    print("Тестовое сообщение отправлено в Telegram")


if __name__ == "__main__":
    import urllib.parse
    main()
