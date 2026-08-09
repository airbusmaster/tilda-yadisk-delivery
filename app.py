#!/usr/bin/env python3
"""Автовыдача цифровых товаров после оплаты в Тильде.

Webhook Тильды (после оплаты) -> сопоставление товаров по артикулу ->
персональные ссылки -> письмо покупателю -> отчёт владельцу в Telegram.

Файлы лежат в публичной папке Яндекс.Диска, но покупатель туда не ходит:
сервис сам качает файл с Диска и отдаёт его своим потоком. Так сделано
потому, что downloader.disk.yandex.ru отвечает «403 Invalid Referer» на любой
запрос с заголовком Referer — включая реферер вашего же домена. Браузер тащит
реферер почтового веб-клиента (e.mail.ru, mail.google.com) через редирект,
и покупатель видит «ссылка не работает», а в логах при этом чистый 302.

Настройки — в config.env рядом с файлом, товары — в catalog.json.
"""
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data.sqlite"
CATALOG_PATH = BASE / "catalog.json"
CONFIG_PATH = BASE / "config.env"

YADISK_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
YADISK_META = "https://cloud-api.yandex.net/v1/disk/public/resources"

log = logging.getLogger("delivery")


# --------------------------------------------------------------------------- config

def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    for key, default in [
        ("PORT", "8091"),
        ("SHOP_NAME", "Магазин"),
        ("PUBLIC_BASE_URL", "https://files.example.com"),
        ("YADISK_PUBLIC_KEY", ""),
        ("LINK_TTL_HOURS", "720"),
        ("MAX_DOWNLOADS", "20"),
        ("SMTP_HOST", "smtp.yandex.ru"),
        ("SMTP_PORT", "465"),
        ("SMTP_USER", ""),
        ("SMTP_PASSWORD", ""),
        ("MAIL_FROM_NAME", ""),
        ("SUPPORT_EMAIL", ""),
        ("MAIL_SUBJECT", "Ваш заказ готов к скачиванию"),
        ("MAIL_SIGNOFF", "С уважением,"),
        ("IMAP_HOST", "imap.yandex.ru"),
        ("SAVE_TO_SENT", "1"),
        ("WEBHOOK_SECRET", ""),
        ("ADMIN_SECRET", ""),
        ("TG_BOT_TOKEN", ""),
        ("TG_CHAT_ID", ""),
        ("NUDGE_AFTER_HOURS", "24"),
        ("DRY_RUN", "0"),
    ]:
        cfg.setdefault(key, default)
    if not cfg["MAIL_FROM_NAME"]:
        cfg["MAIL_FROM_NAME"] = cfg["SHOP_NAME"]
    if not cfg["SUPPORT_EMAIL"]:
        cfg["SUPPORT_EMAIL"] = cfg["SMTP_USER"]
    return cfg


CFG = load_config()
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

# Индекс "нормализованное название -> артикул" для подстраховки, если Тильда
# не передала артикул в составе заказа.
def _norm(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", s)


TITLE_INDEX = {_norm(v["title"]): k for k, v in CATALOG.items()}


def ttl_hours():
    return int(CFG["LINK_TTL_HOURS"])


def ttl_words():
    """«30 дней» вместо «720 часов» — так понятнее в письме."""
    h = ttl_hours()
    if h % 24 == 0 and h >= 48:
        d = h // 24
        tail = "дней"
        if d % 10 == 1 and d % 100 != 11:
            tail = "день"
        elif d % 10 in (2, 3, 4) and d % 100 not in (12, 13, 14):
            tail = "дня"
        return f"{d} {tail}"
    return f"{h} часа"


# --------------------------------------------------------------------------- db

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_key   TEXT PRIMARY KEY,
                email       TEXT,
                name        TEXT,
                amount      TEXT,
                status      TEXT,
                created_at  INTEGER,
                raw         TEXT
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token       TEXT PRIMARY KEY,
                order_key   TEXT,
                sku         TEXT,
                title       TEXT,
                file        TEXT,
                expires_at  INTEGER,
                downloads   INTEGER DEFAULT 0,
                max_downloads INTEGER,
                created_at  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_order ON tokens(order_key);
            """
        )
        # nudged — признак «уже предупредили владельца, что клиент не скачал»
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "nudged" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN nudged INTEGER DEFAULT 0")


# --------------------------------------------------------------------------- helpers

def tg_notify(text):
    token, chat = CFG["TG_BOT_TOKEN"], CFG["TG_CHAT_ID"]
    if not token or not chat:
        log.warning("Telegram не настроен, уведомление пропущено: %s", text)
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": "true"}
        ).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20
        ).read()
    except Exception as e:
        log.error("Не удалось отправить в Telegram: %s", e)


def flatten_form(pairs):
    """Разбирает form-поля вида payment[products][0][name] в дерево."""
    root = {}
    for key, value in pairs:
        parts = re.findall(r"[^\[\]]+", key)
        node = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
    return root


def _listify(obj):
    """{"0": {...}, "1": {...}} -> [{...}, {...}]"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        if all(k.isdigit() for k in obj.keys()) and obj:
            return [obj[k] for k in sorted(obj, key=int)]
        return [obj]
    return []


def parse_payload(body, content_type):
    """Тильда шлёт либо JSON, либо form-urlencoded — принимаем оба."""
    text = body.decode("utf-8", "replace")
    ctype = (content_type or "").lower()
    if "application/json" in ctype or text.lstrip().startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            pass
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    data = flatten_form(pairs)
    # payment иногда приезжает строкой с JSON внутри
    pay = data.get("payment")
    if isinstance(pay, str):
        try:
            data["payment"] = json.loads(pay)
        except Exception:
            pass
    return data


def extract_order(data):
    """Достаёт email, имя, список товаров и id заказа из payload Тильды."""
    def pick(*names):
        for n in names:
            for key in data:
                if key.lower() == n.lower() and data[key]:
                    return str(data[key]).strip()
        return ""

    payment = data.get("payment") or {}
    if isinstance(payment, str):
        try:
            payment = json.loads(payment)
        except Exception:
            payment = {}

    email = pick("email", "Email", "e-mail")
    name = pick("name", "Name", "imya")
    amount = str(payment.get("amount", "") or pick("amount"))
    order_id = str(payment.get("orderid", "") or payment.get("orderId", "") or "")

    products = _listify(payment.get("products"))
    items = []
    for p in products:
        if not isinstance(p, dict):
            continue
        items.append({
            "name": str(p.get("name", "")).strip(),
            "sku": str(p.get("sku", "") or p.get("externalid", "")).strip(),
            "quantity": str(p.get("quantity", "1")),
        })

    if not order_id:
        # Запасной ключ идемпотентности, если Тильда не прислала номер заказа
        basis = f"{email}|{amount}|{'|'.join(i['name'] for i in items)}"
        order_id = "auto-" + uuid.uuid5(uuid.NAMESPACE_URL, basis).hex[:16]

    return {
        "order_key": order_id,
        "email": email,
        "name": name,
        "amount": amount,
        "items": items,
        "payment": payment,
    }


def match_sku(item):
    """Артикул из заказа, иначе — поиск по названию товара."""
    sku = item.get("sku", "")
    if sku and sku in CATALOG:
        return sku
    return TITLE_INDEX.get(_norm(item.get("name")))


# --------------------------------------------------------------------------- Яндекс.Диск

def _path_variants(file_path):
    """Имена файлов, залитых с macOS, лежат на Диске в NFD («й» = «и» + U+0306),
    а в каталоге записаны в обычном NFC — на такой путь Диск отвечает 404."""
    variants = [file_path]
    for form in ("NFC", "NFD"):
        variant = unicodedata.normalize(form, file_path)
        if variant not in variants:
            variants.append(variant)
    return variants


def yadisk_temp_link(file_path):
    """Временная ссылка Яндекс.Диска на файл в публичной папке (живёт ~4 часа)."""
    last_error = None
    for variant in _path_variants(file_path):
        url = YADISK_API + "?" + urllib.parse.urlencode(
            {"public_key": CFG["YADISK_PUBLIC_KEY"], "path": variant}
        )
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)["href"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            last_error = e
    raise last_error


_meta_cache = {}
_meta_lock = threading.Lock()


def yadisk_size(file_path):
    """Размер файла в байтах. Нужен, чтобы честно предупредить о весе на странице."""
    with _meta_lock:
        hit = _meta_cache.get(file_path)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
    size = 0
    for variant in _path_variants(file_path):
        url = YADISK_META + "?" + urllib.parse.urlencode(
            {"public_key": CFG["YADISK_PUBLIC_KEY"], "path": variant, "fields": "size"}
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                size = int(json.load(r).get("size") or 0)
            break
        except Exception:
            continue
    with _meta_lock:
        _meta_cache[file_path] = (time.time(), size)
    return size


def human_size(n):
    if not n:
        return ""
    mb = n / 1048576
    if mb >= 1024:
        return f"{mb / 1024:.1f} ГБ"
    return f"{mb:.0f} МБ"


# --------------------------------------------------------------------------- почта

MAIL_TEXT = """Здравствуйте{name_part}!

Спасибо за заказ. Ваши материалы готовы к скачиванию:

{items}

Откройте ссылку и нажмите кнопку «Скачать» — файл сохранится на устройство
и останется у вас навсегда. Файлы большие, лучше качать по Wi-Fi.
Ссылки действуют {ttl}.

Если ссылка не открылась или файл не скачался — просто ответьте на это
письмо, поможем.

{signoff}
{shop}
"""

MAIL_HTML = """<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
font-size:16px;line-height:1.55;color:#222;">
<p>Здравствуйте{name_part}!</p>
<p>Спасибо за заказ. Ваши материалы готовы к скачиванию:</p>
<ul style="padding-left:18px;">{items}</ul>
<p style="color:#666;font-size:14px;">Откройте ссылку и нажмите кнопку «Скачать» —
файл сохранится на устройство и останется у вас навсегда. Файлы большие, лучше
качать по Wi-Fi. Ссылки действуют {ttl}.</p>
<p style="color:#666;font-size:14px;">Если ссылка не открылась или файл не
скачался — просто ответьте на это письмо, поможем.</p>
<p>{signoff}<br>{shop}</p>
</body></html>
"""


def save_to_sent(msg):
    """Кладём копию письма в «Отправленные» — чтобы было что переслать клиенту."""
    if CFG["SAVE_TO_SENT"] != "1" or not CFG["SMTP_PASSWORD"]:
        return
    try:
        m = imaplib.IMAP4_SSL(CFG["IMAP_HOST"], 993)
        m.login(CFG["SMTP_USER"], CFG["SMTP_PASSWORD"])
        m.append("Sent", "\\Seen", imaplib.Time2Internaldate(time.time()),
                 msg.as_bytes())
        m.logout()
    except Exception as e:
        log.warning("Не удалось сохранить копию в «Отправленные»: %s", e)


def send_mail(to_email, name, links):
    """links: список (title, url, size_bytes)."""
    ttl = ttl_words()
    name_part = f", {name}" if name else ""

    text_items = "\n".join(
        f"— {t}{' (' + human_size(s) + ')' if s else ''}\n  {u}" for t, u, s in links
    )
    html_items = "".join(
        f'<li style="margin-bottom:10px;">{html.escape(t)}'
        f'{" — " + human_size(s) if s else ""}<br>'
        f'<a href="{html.escape(u)}">Скачать</a></li>' for t, u, s in links
    )

    msg = EmailMessage()
    msg["Subject"] = CFG["MAIL_SUBJECT"]
    msg["From"] = formataddr((CFG["MAIL_FROM_NAME"], CFG["SMTP_USER"]))
    msg["To"] = to_email
    msg["Reply-To"] = CFG["SMTP_USER"]
    # Без Date и Message-ID копия в «Отправленных» выглядит битой, а часть
    # спам-фильтров снижает рейтинг письму
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=CFG["SMTP_USER"].split("@")[-1])
    msg.set_content(MAIL_TEXT.format(name_part=name_part, items=text_items, ttl=ttl,
                                     signoff=CFG["MAIL_SIGNOFF"], shop=CFG["SHOP_NAME"]))
    msg.add_alternative(
        MAIL_HTML.format(name_part=html.escape(name_part), items=html_items, ttl=ttl,
                         signoff=html.escape(CFG["MAIL_SIGNOFF"]),
                         shop=html.escape(CFG["SHOP_NAME"])),
        subtype="html",
    )

    if CFG["DRY_RUN"] == "1":
        log.info("DRY_RUN: письмо для %s не отправлено:\n%s", to_email, msg)
        return

    port = int(CFG["SMTP_PORT"])
    if port == 465:
        with smtplib.SMTP_SSL(CFG["SMTP_HOST"], port,
                              context=ssl.create_default_context(), timeout=60) as s:
            s.login(CFG["SMTP_USER"], CFG["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(CFG["SMTP_HOST"], port, timeout=60) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(CFG["SMTP_USER"], CFG["SMTP_PASSWORD"])
            s.send_message(msg)
    save_to_sent(msg)


# --------------------------------------------------------------------------- обработка заказа

def issue_links(order_key, items):
    """Создаёт свежие токены для заказа. Старые токены заказа удаляются."""
    ttl_sec = ttl_hours() * 3600
    now = int(time.time())
    links = []
    with db() as conn:
        conn.execute("DELETE FROM tokens WHERE order_key=?", (order_key,))
        for item in items:
            sku = match_sku(item)
            entry = CATALOG[sku]
            token = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO tokens(token,order_key,sku,title,file,expires_at,"
                "downloads,max_downloads,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (token, order_key, sku, entry["title"], entry["file"], now + ttl_sec,
                 0, int(CFG["MAX_DOWNLOADS"]), now),
            )
            links.append((entry["title"],
                          f"{CFG['PUBLIC_BASE_URL'].rstrip('/')}/d/{token}",
                          yadisk_size(entry["file"])))
    return links


def process_order(data):
    order = extract_order(data)
    key = order["order_key"]

    with db() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_key=?", (key,)).fetchone()
        if row and row["status"] == "sent":
            log.info("Заказ %s уже отправлен, пропускаем", key)
            return "duplicate"
        if row:
            # Прошлая попытка не дошла до письма — пробуем ещё раз, а не молчим
            log.info("Заказ %s был в статусе %s, обрабатываем заново", key, row["status"])
            conn.execute(
                "UPDATE orders SET email=?,name=?,amount=?,status='processing',raw=? "
                "WHERE order_key=?",
                (order["email"], order["name"], order["amount"],
                 json.dumps(data, ensure_ascii=False)[:20000], key),
            )
        else:
            conn.execute(
                "INSERT INTO orders(order_key,email,name,amount,status,created_at,raw)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, order["email"], order["name"], order["amount"], "processing",
                 int(time.time()), json.dumps(data, ensure_ascii=False)[:20000]),
            )

    def fail(reason):
        with db() as conn:
            conn.execute("UPDATE orders SET status=? WHERE order_key=?", (reason, key))
        tg_notify(
            f"⚠️ Заказ <b>{html.escape(key)}</b> не отправлен автоматически\n"
            f"Причина: {html.escape(reason)}\n"
            f"Почта: {html.escape(order['email'] or '—')}\n"
            f"Товары: {html.escape(', '.join(i['name'] for i in order['items']) or '—')}\n"
            f"Нужно отправить руками."
        )
        return reason

    if not order["email"]:
        return fail("не пришёл email покупателя")
    if not order["items"]:
        return fail("в заказе нет товаров")

    unknown = [i["name"] for i in order["items"] if not match_sku(i)]
    if unknown:
        return fail("нет файла для товара: " + ", ".join(unknown))

    links = issue_links(key, order["items"])

    try:
        send_mail(order["email"], order["name"], links)
    except Exception as e:
        log.exception("Ошибка отправки письма")
        return fail(f"не удалось отправить письмо: {e}")

    with db() as conn:
        conn.execute("UPDATE orders SET status='sent', nudged=0 WHERE order_key=?", (key,))

    tg_notify(
        f"✅ Заказ <b>{html.escape(key)}</b> отправлен\n"
        f"Кому: {html.escape(order['name'] or '—')} &lt;{html.escape(order['email'])}&gt;\n"
        f"Гиды: {html.escape(', '.join(t for t, _, _ in links))}\n"
        f"Сумма: {html.escape(order['amount'] or '—')}"
    )
    log.info("Заказ %s: письмо отправлено на %s", key, order["email"])
    return "sent"


def resend_order(order_key):
    """Перевыпуск ссылок и повторное письмо — по кнопке из админки."""
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_key=?",
                             (order_key,)).fetchone()
    if not order:
        return "заказ не найден"
    data = json.loads(order["raw"] or "{}")
    items = extract_order(data)["items"] if data else []
    if not items:
        with db() as conn:
            items = [{"name": r["title"], "sku": r["sku"]}
                     for r in conn.execute("SELECT DISTINCT sku,title FROM tokens "
                                           "WHERE order_key=?", (order_key,))]
    if not items or any(not match_sku(i) for i in items):
        return "не удалось определить товары заказа"

    links = issue_links(order_key, items)
    try:
        send_mail(order["email"], order["name"], links)
    except Exception as e:
        log.exception("Повторная отправка не удалась")
        return f"письмо не ушло: {e}"
    with db() as conn:
        conn.execute("UPDATE orders SET status='sent', nudged=0 WHERE order_key=?",
                     (order_key,))
    tg_notify(f"🔁 Заказ <b>{html.escape(order_key)}</b>: ссылки перевыпущены и "
              f"отправлены на {html.escape(order['email'] or '—')}")
    log.info("Заказ %s: ссылки перевыпущены, письмо на %s", order_key, order["email"])
    return ""


# --------------------------------------------------------------------------- сторож

def watchdog():
    """Раз в полчаса смотрит: письмо ушло сутки назад, а покупатель ни разу
    не скачал. Значит, у него что-то не получилось и он молчит."""
    hours = int(CFG["NUDGE_AFTER_HOURS"])
    while True:
        time.sleep(1800)
        try:
            cutoff = int(time.time()) - hours * 3600
            with db() as conn:
                rows = conn.execute(
                    "SELECT o.order_key, o.email, o.name,"
                    " (SELECT COALESCE(SUM(downloads),0) FROM tokens t"
                    "   WHERE t.order_key=o.order_key) AS dl"
                    " FROM orders o WHERE o.status='sent' AND COALESCE(o.nudged,0)=0"
                    "   AND o.created_at < ?", (cutoff,)).fetchall()
                for r in rows:
                    conn.execute("UPDATE orders SET nudged=1 WHERE order_key=?",
                                 (r["order_key"],))
            for r in rows:
                if r["dl"] == 0:
                    tg_notify(
                        f"🔔 Заказ <b>{html.escape(r['order_key'])}</b>: письмо ушло "
                        f"{hours} ч назад, но покупатель так и не скачал ни одного файла.\n"
                        f"Кому: {html.escape(r['name'] or '—')} "
                        f"&lt;{html.escape(r['email'] or '—')}&gt;\n"
                        f"Стоит написать первым."
                    )
        except Exception:
            log.exception("Сторож споткнулся")


# --------------------------------------------------------------------------- http

PAGE_CSS = ("font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
            "max-width:520px;margin:10vh auto;padding:0 20px;line-height:1.55;color:#222;")


class Handler(BaseHTTPRequestHandler):
    server_version = "delivery"
    protocol_version = "HTTP/1.1"

    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.address_string()).split(",")[0]

    def log_message(self, fmt, *args):
        ref = self.headers.get("Referer") or "-"
        log.info("%s %s ref=%s", self.client_ip(), fmt % args, ref)

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, code, title, text, extra=""):
        self._send(code, f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title></head>
<body style="{PAGE_CSS}">
<h2 style="font-weight:600;">{html.escape(title)}</h2>
<p>{html.escape(text)}</p>
{extra}
<p style="color:#888;font-size:14px;">Напишите на {html.escape(CFG['SUPPORT_EMAIL'])} —
пришлём новую ссылку.</p></body></html>""", "text/html; charset=utf-8")

    # ---- выдача файла ----------------------------------------------------

    def _token_row(self, token):
        with db() as conn:
            return conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()

    def _token_problem(self, row):
        """Возвращает True, если ответ уже отправлен (ссылка непригодна)."""
        if not row:
            self._page(404, "Ссылка не найдена",
                       "Возможно, в ней опечатка или её уже удалили.")
            return True
        if row["expires_at"] < time.time():
            self._page(410, "Срок ссылки истёк",
                       f"Ссылка действовала {ttl_words()} с момента покупки.")
            return True
        if row["downloads"] >= row["max_downloads"]:
            self._page(429, "Лимит скачиваний исчерпан",
                       f"По этой ссылке файл скачали {row['downloads']} раз.")
            return True
        return False

    def page_download(self, token):
        """Страница с кнопкой. Отдельная страница нужна не только для удобства:
        meta referrer=no-referrer снимает Referer, из-за которого Диск раньше
        отвечал «403 Invalid Referer»."""
        row = self._token_row(token)
        if self._token_problem(row):
            return
        size = yadisk_size(row["file"])
        if size and size > 100 * 1048576:
            size_note = f"Файл большой, {human_size(size)} — лучше качать по Wi-Fi. "
        elif size:
            size_note = f"Размер файла — {human_size(size)}. "
        else:
            size_note = ""
        left = row["max_downloads"] - row["downloads"]
        left_note = f", осталось скачиваний: {left}" if left <= 5 else ""
        extra = f"""
<p style="margin:26px 0;">
  <a href="/f/{html.escape(token)}" rel="noreferrer noopener"
     style="display:inline-block;background:#111;color:#fff;text-decoration:none;
            padding:14px 28px;border-radius:10px;font-size:17px;">Скачать PDF</a>
</p>
<p style="color:#666;font-size:14px;">{size_note}Загрузка идёт в фоне: на iPhone файл
появится в приложении «Файлы» → «Загрузки», на Android — в «Загрузках» браузера.
Если ничего не произошло, нажмите кнопку ещё раз.</p>
<p style="color:#888;font-size:13px;">Ссылка действует {ttl_words()}{left_note}.</p>"""
        self._send(200, f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(row['title'])}</title></head>
<body style="{PAGE_CSS}">
<h2 style="font-weight:600;">{html.escape(row['title'])}</h2>
<p>Ваш файл готов к скачиванию.</p>{extra}
<p style="color:#888;font-size:13px;">Что-то пошло не так — ответьте на письмо
или напишите на {html.escape(CFG['SUPPORT_EMAIL'])}, поможем.</p>
</body></html>""", "text/html; charset=utf-8")

    def stream_file(self, token):
        """Качаем файл с Диска сами и переливаем клиенту. Клиент на Яндекс не ходит,
        поэтому его Referer больше ничего не ломает."""
        row = self._token_row(token)
        if self._token_problem(row):
            return
        try:
            href = yadisk_temp_link(row["file"])
        except Exception as e:
            log.error("Яндекс.Диск не отдал ссылку на %s: %s", row["file"], e)
            tg_notify(f"⚠️ Яндекс.Диск не отдал файл <b>{html.escape(row['file'])}</b>: "
                      f"{html.escape(str(e))}")
            return self._page(502, "Файл временно недоступен",
                              "Попробуйте через несколько минут.")

        req = urllib.request.Request(href)
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        try:
            up = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            log.error("Не удалось начать скачивание %s: %s", row["file"], e)
            return self._page(502, "Файл временно недоступен",
                              "Попробуйте через несколько минут.")

        name = row["file"].lstrip("/")
        quoted = urllib.parse.quote(name)
        total = 0
        start = 0
        cr = up.headers.get("Content-Range")
        if cr:
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
            if m:
                start, total = int(m.group(1)), int(m.group(3))
        else:
            total = int(up.headers.get("Content-Length") or 0)

        self.send_response(up.status)
        self.send_header("Content-Type", "application/pdf")
        if up.headers.get("Content-Length"):
            self.send_header("Content-Length", up.headers["Content-Length"])
        if cr:
            self.send_header("Content-Range", cr)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

        sent = 0
        finished = False
        try:
            while True:
                chunk = up.read(262144)
                if not chunk:
                    finished = True
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log.info("Клиент оборвал загрузку %s на %d байт (токен %s)",
                     row["file"], sent, token[:8])
            self.close_connection = True
        except Exception as e:
            log.error("Обрыв при отдаче %s: %s", row["file"], e)
            self.close_connection = True
        finally:
            up.close()

        # Считаем скачивание только когда файл реально доехал до конца
        complete = finished and total and (start + sent) >= total
        if complete:
            with db() as conn:
                conn.execute("UPDATE tokens SET downloads=downloads+1 WHERE token=?",
                             (token,))
            log.info("Файл %s отдан целиком (%d байт), токен %s, скачивание %d",
                     row["file"], sent, token[:8], row["downloads"] + 1)
        else:
            log.info("Частичная отдача %s: %d байт, токен %s — не засчитано",
                     row["file"], sent, token[:8])

    # ---- админка ---------------------------------------------------------

    def admin_page(self, note=""):
        rows = []
        with db() as conn:
            for o in conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT 30"
            ):
                toks = conn.execute(
                    "SELECT token,title,downloads,max_downloads,expires_at"
                    " FROM tokens WHERE order_key=?", (o["order_key"],)).fetchall()
                rows.append((o, toks))

        now = time.time()
        body = []
        for o, toks in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(o["created_at"]))
            items = "".join(
                f"<div style='color:#555;font-size:14px;'>{html.escape(t['title'])} — "
                f"скачано {t['downloads']}/{t['max_downloads']}"
                f"{', ПРОСРОЧЕН' if t['expires_at'] < now else ''} "
                f"<a href='/d/{t['token']}'>ссылка</a></div>" for t in toks)
            body.append(f"""
<div style="border:1px solid #e5e5e5;border-radius:10px;padding:12px;margin-bottom:12px;">
  <div><b>{html.escape(o['order_key'])}</b> · {when} · {html.escape(o['status'] or '')}</div>
  <div style="color:#555;">{html.escape(o['name'] or '')} &lt;{html.escape(o['email'] or '')}&gt;</div>
  {items}
  <form method="post" action="/admin/{html.escape(CFG['ADMIN_SECRET'])}/resend"
        style="margin-top:8px;">
    <input type="hidden" name="order_key" value="{html.escape(o['order_key'])}">
    <button type="submit" style="background:#111;color:#fff;border:0;border-radius:8px;
        padding:10px 16px;font-size:15px;">Перевыпустить и отправить</button>
  </form>
</div>""")

        note_html = (f"<p style='background:#f0f7f0;padding:10px;border-radius:8px;'>"
                     f"{html.escape(note)}</p>" if note else "")
        self._send(200, f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Заказы · {html.escape(CFG['SHOP_NAME'])}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
max-width:640px;margin:24px auto;padding:0 16px;line-height:1.5;color:#222;">
<h2>Заказы</h2>{note_html}{''.join(body)}</body></html>""",
                   "text/html; charset=utf-8", {"X-Robots-Tag": "noindex, nofollow"})

    # ---- маршруты --------------------------------------------------------

    def do_HEAD(self):
        """Антиспам-сканеры почты и некоторые браузеры сначала дёргают HEAD.
        Отвечаем заголовками, счётчик скачиваний при этом не трогаем."""
        path = urllib.parse.urlparse(self.path).path
        ctype = "text/plain; charset=utf-8"
        code = 404
        if path in ("/health", "/healthz"):
            code = 200
        elif path.startswith("/d/") or path.startswith("/f/"):
            ctype = "text/html; charset=utf-8" if path.startswith("/d/") else "application/pdf"
            code = 200 if self._token_row(path[3:].strip("/")) else 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        admin = CFG["ADMIN_SECRET"]

        if path in ("/health", "/healthz"):
            return self._send(200, "ok")
        if path == "/favicon.ico":
            return self._send(204, b"")
        if path.startswith("/d/"):
            return self.page_download(path[3:].strip("/"))
        if path.startswith("/f/"):
            return self.stream_file(path[3:].strip("/"))
        if admin and path.rstrip("/") == f"/admin/{admin}":
            return self.admin_page()
        return self._send(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        secret = CFG["WEBHOOK_SECRET"]
        admin = CFG["ADMIN_SECRET"]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if admin and path == f"/admin/{admin}/resend":
            fields = dict(urllib.parse.parse_qsl(body.decode("utf-8", "replace")))
            key = fields.get("order_key", "")
            err = resend_order(key) if key else "не передан заказ"
            return self.admin_page(
                f"Заказ {key}: не получилось — {err}" if err
                else f"Заказ {key}: ссылки перевыпущены, письмо отправлено")

        if not secret or path != f"/hook/{secret}":
            log.warning("Неизвестный POST на %s", path)
            return self._send(404, "not found")

        # Тильда ждёт быстрый ответ — отвечаем сразу, потом работаем
        self._send(200, "ok")

        try:
            data = parse_payload(body, self.headers.get("Content-Type"))
            if data.get("test") == "test" or (
                len(data) == 1 and "test" in data
            ):
                log.info("Тестовый запрос от Тильды: %s", data)
                tg_notify("🔧 Тестовый запрос от Тильды получен — вебхук работает")
                return
            result = process_order(data)
            log.info("Итог обработки: %s", result)
        except Exception as e:
            log.exception("Ошибка обработки вебхука")
            tg_notify(f"⚠️ Ошибка обработки заказа: {html.escape(str(e))}\n"
                      f"Проверьте логи: journalctl -u delivery")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(BASE / "app.log", encoding="utf-8")],
    )
    init_db()
    port = int(CFG["PORT"])
    if not CFG["WEBHOOK_SECRET"]:
        log.warning("WEBHOOK_SECRET пуст — вебхук отключён, задайте его в config.env")
    if not CFG["ADMIN_SECRET"]:
        log.warning("ADMIN_SECRET пуст — админка отключена, задайте его в config.env")
    threading.Thread(target=watchdog, daemon=True).start()
    log.info("Сервис слушает 127.0.0.1:%s, товаров в каталоге: %d, "
             "ссылки живут %s, лимит %s", port, len(CATALOG), ttl_words(),
             CFG["MAX_DOWNLOADS"])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
