#!/usr/bin/env python3
"""Ежедневный бэкап базы выданных ссылок. Держим 14 последних копий.

Потеря data.sqlite = потеря всех ссылок, которые уже разосланы покупателям.
Запускается из cron: 0 4 * * * /usr/bin/python3 /opt/delivery/backup.py
"""
import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DST = BASE / "backups"
DST.mkdir(exist_ok=True)

name = DST / ("data-%s.sqlite" % time.strftime("%Y%m%d-%H%M"))
src = sqlite3.connect(BASE / "data.sqlite")
dst = sqlite3.connect(name)
with dst:
    src.backup(dst)
dst.close()
src.close()

copies = sorted(DST.glob("data-*.sqlite"))
for old in copies[:-14]:
    old.unlink()

print("бэкап:", name, "всего копий:", len(copies[-14:]))
