# -*- coding: utf-8 -*-
"""
Arbuzik VPN — Telegram-бот на aiogram 3 (всё в одном файле).

Подключён к панели H1 VLESS (germany-d1.h1cloud.net).
Оплата: CryptoBot (крипта, автоматически) и карта (перевод, подтверждает админ).
Mini App: miniapp.html + встроенный веб-сервер + туннель cloudflared.

Запуск: start.bat (или `venv\\Scripts\\python.exe bot.py`).
"""
import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import qrcode
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo,
)

# ============================================================
#                        НАСТРОЙКИ
# ============================================================

# На Render значения можно задать переменными окружения (Env)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8815266530:AAEYbUEvbUi5Aho_ZfETOIaA8gkoo0LOBDo")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7706026760"))

# Панель H1 VLESS
PANEL_URL = os.environ.get("PANEL_URL", "http://germany-d1.h1cloud.net:25363")
PANEL_TOKEN = os.environ.get(
    "PANEL_TOKEN",
    "2e03f56856784339b918d068f1759e03e7608d4365ba42cfa00aec940ec7244a")

# Ссылка подписки (HTTPS — работает в Happ, v2tunes, Hiddify и т.д.)
SUB_BASE = os.environ.get("SUB_BASE", "https://179-254-115-60.sslip.io/sub/")
INCY_SUFFIX = "?format=xray"  # параметр для приложения Incy

# Оплата через CryptoBot (Crypto Pay API)
CRYPTOBOT_TOKEN = os.environ.get(
    "CRYPTOBOT_TOKEN", "636063:AAQXvXF4uJsA5nlJDlNZ1XGD3bJ6wdH8I2z")
CRYPTOBOT_FIAT = "RUB"  # выставлять счёт в рублях

# Бесплатная (пробная) подписка
TRIAL_DAYS = 3
TRIAL_GB = 10
TRIAL_DEVICES = 2

# Лимиты платной подписки
PAID_GB = 100          # трафик ГБ на всех тарифах
PAID_DEVICES = 5       # по умолчанию для /give (у тарифов свои лимиты)

# Тарифы: (название, дней, устройств, цена в ₽)
TARIFFS = [
    ("1 месяц", 30, 3, 85),
    ("3 месяца", 90, 3, 340),
    ("6 месяцев", 180, 5, 500),
]

# Как оплачивать картой (впишите свои реквизиты!)
PAYMENT_INFO = (
    "💳 <b>Оплата переводом:</b>\n"
    "Карта: <code>0000 0000 0000 0000</code> (Иван И.)\n\n"
    "В комментарии к переводу укажите свой ID: <code>{user_id}</code>\n"
    "Сумма: <b>{amount} ₽</b>\n\n"
    "После оплаты нажмите «✅ Я оплатил» — администратор подтвердит, "
    "и подписка активируется автоматически."
)

# Mini App
MINIAPP_PORT = int(os.environ.get("PORT", "8080"))   # Render задаёт PORT сам
MINIAPP_HOST = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
MINIAPP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")
SSH = r"C:\Program Files\Git\usr\bin\ssh.exe"  # туннель нужен только локально

BRAND = "Арбузик VPN 🍉"
WELCOME = (
    f"Привет! Это <b>{BRAND}</b>\n\n"
    "Быстрый и безопасный VPN 🇩🇪\n\n"
    "🎁 Получи бесплатную подписку на 3 дня в разделе «Бесплатная»\n"
    "💳 Или купи подписку — от 85 ₽\n\n"
    "Выбери действие на клавиатуре ниже 👇"
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("arbuzik")

MSK = timezone(timedelta(hours=3))
MINIAPP_URL: str | None = None   # публичный URL, заполняет туннель
BOT: Bot | None = None
LOOP: asyncio.AbstractEventLoop | None = None


# ============================================================
#                        ПАНЕЛЬ H1 VLESS
# ============================================================

class PanelError(Exception):
    pass


class PanelAPI:
    def __init__(self):
        self.base = PANEL_URL.rstrip("/") + "/api"
        self.headers = {"Authorization": "Bearer " + PANEL_TOKEN}

    async def _request(self, method: str, path: str, json_body: dict | None = None,
                       retries: int = 3) -> dict:
        last_err = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=25) as c:
                    r = await c.request(method, self.base + path,
                                        headers=self.headers, json=json_body)
                try:
                    data = r.json()
                except ValueError:
                    raise PanelError(f"Панель вернула не-JSON (HTTP {r.status_code})")
                if r.status_code >= 400 or not data.get("ok", False):
                    raise PanelError(str(data.get("error", f"HTTP {r.status_code}")))
                return data
            except PanelError:
                raise  # ошибка логики панели — повторять бессмысленно
            except (httpx.HTTPError, OSError) as e:
                last_err = e  # сетевой сбой — повторяем
                if attempt < retries - 1:
                    await asyncio.sleep(3)
        raise PanelError(f"Панель недоступна: {last_err}")

    async def create(self, name: str, days: int, traffic_gb: int, device_limit: int) -> dict:
        data = await self._request("POST", "/create", {
            "name": name, "days": days,
            "traffic_gb": traffic_gb, "device_limit": device_limit,
        })
        return data["client"]

    async def get(self, name: str) -> dict | None:
        try:
            data = await self._request("GET", f"/clients/{name}")
            return data["client"]
        except PanelError as e:
            if "not_found" in str(e) or "user_not_found" in str(e):
                return None
            raise

    async def extend(self, name: str, days: int | None = None,
                     traffic_gb: int | None = None, device_limit: int | None = None,
                     expires_at: int | None = None) -> dict:
        body: dict = {}
        if days is not None:
            body["days"] = days
        if traffic_gb is not None:
            body["traffic_gb"] = traffic_gb
        if device_limit is not None:
            body["device_limit"] = device_limit
        if expires_at is not None:
            body["expires_at"] = expires_at
        data = await self._request("PATCH", f"/clients/{name}", body)
        return data["client"]

    async def delete(self, name: str) -> None:
        await self._request("DELETE", f"/clients/{name}")


api = PanelAPI()


# ============================================================
#                        CRYPTOBOT (оплата)
# ============================================================

class CryptoError(Exception):
    pass


class CryptoPay:
    BASE = "https://pay.crypt.bot/api"

    async def _call(self, method: str, params: dict | None = None,
                    retries: int = 3) -> dict:
        params = params or {}
        last_err = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=25) as c:
                    r = await c.get(self.BASE + f"/{method}", params=params,
                                    headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN})
                data = r.json()
                if not data.get("ok"):
                    raise CryptoError(str(data.get("error", data)))
                return data["result"]
            except (httpx.HTTPError, OSError) as e:
                last_err = e
                if attempt < retries - 1:
                    await asyncio.sleep(3)
        raise CryptoError(f"CryptoBot недоступен: {last_err}")

    async def create_invoice(self, amount_rub: float, description: str,
                             payload: str) -> dict:
        """Счёт в рублях (конвертация по курсу CryptoBot)."""
        return await self._call("createInvoice", {
            "currency_type": "fiat",
            "fiat": CRYPTOBOT_FIAT,
            "amount": f"{amount_rub:.2f}",
            "description": description[:1024],
            "payload": payload,
            "expires_in": 3600,
        })

    async def get_invoices(self, invoice_ids: list[int]) -> list[dict]:
        """Статусы счетов (API отдаёт только active или paid)."""
        ids = ",".join(str(i) for i in invoice_ids)
        items: list[dict] = []
        for status in ("active", "paid"):
            try:
                res = await self._call("getInvoices",
                                       {"invoice_ids": ids, "status": status})
                items.extend(res.get("items", []))
            except CryptoError:
                continue
        return items


# ============================================================
#                        БАЗА ДАННЫХ (SQLite)
# ============================================================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users(
                tg_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                client_name TEXT,
                trial_taken INTEGER DEFAULT 0,
                created_at INTEGER,
                notified_3d INTEGER DEFAULT 0,
                notified_expired INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                amount REAL,
                days INTEGER,
                devices INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                method TEXT DEFAULT 'card',
                invoice_id INTEGER,
                created_at INTEGER,
                confirmed_at INTEGER
            )
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(payments)").fetchall()]
        if "method" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN method TEXT DEFAULT 'card'")
        if "invoice_id" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN invoice_id INTEGER")
        if "devices" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN devices INTEGER DEFAULT 0")


def upsert_user(tg_id: int, username: str) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO users(tg_id, username, created_at) VALUES(?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username
        """, (tg_id, username or "", int(time.time())))


def get_user(tg_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


def set_client(tg_id: int, client_name: str) -> None:
    with conn() as c:
        c.execute("UPDATE users SET client_name = ? WHERE tg_id = ?", (client_name, tg_id))


def take_trial(tg_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE users SET trial_taken = 1 WHERE tg_id = ?", (tg_id,))


def set_notify_flags(tg_id: int, notified_3d: int | None = None,
                     notified_expired: int | None = None) -> None:
    sets, args = [], []
    if notified_3d is not None:
        sets.append("notified_3d = ?")
        args.append(notified_3d)
    if notified_expired is not None:
        sets.append("notified_expired = ?")
        args.append(notified_expired)
    if not sets:
        return
    args.append(tg_id)
    with conn() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE tg_id = ?", args)


def all_users_with_client():
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE client_name IS NOT NULL").fetchall()


def all_users():
    with conn() as c:
        return c.execute("SELECT tg_id FROM users").fetchall()


def add_payment(tg_id: int, amount: float, days: int, devices: int = 0,
                method: str = "card", invoice_id: int | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO payments(tg_id, amount, days, devices, method, invoice_id, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (tg_id, amount, days, devices, method, invoice_id, int(time.time())),
        )
        return cur.lastrowid


def get_payment(pid: int):
    with conn() as c:
        return c.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()


def set_payment_status(pid: int, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE payments SET status = ?, confirmed_at = ? WHERE id = ?",
                  (status, int(time.time()), pid))


def has_pending_payment(tg_id: int) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE tg_id = ? AND status = 'pending'",
            (tg_id,),
        ).fetchone()
        return row["n"] > 0


def pending_crypto_payments():
    with conn() as c:
        return c.execute(
            "SELECT * FROM payments WHERE method = 'crypto' AND status = 'pending' "
            "AND invoice_id IS NOT NULL"
        ).fetchall()


def payments_stats() -> dict:
    with conn() as c:
        total = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM payments "
            "WHERE status = 'paid'").fetchone()
        month = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM payments "
            "WHERE status = 'paid' AND confirmed_at >= ?",
            (int(time.time()) - 30 * 86400,)).fetchone()
        buyers = c.execute(
            "SELECT COUNT(DISTINCT tg_id) AS n FROM payments WHERE status = 'paid'"
        ).fetchone()
        return {"paid_count": total["n"], "earned_total": total["s"],
                "paid_count_30d": month["n"], "earned_30d": month["s"],
                "buyers": buyers["n"]}


def users_stats() -> dict:
    now = int(time.time())
    with conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        with_sub = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE client_name IS NOT NULL").fetchone()["n"]
        trials = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE trial_taken = 1").fetchone()["n"]
        new_today = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (now - 86400,)).fetchone()["n"]
        new_7d = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (now - 7 * 86400,)).fetchone()["n"]
        return {"total": total, "with_sub": with_sub, "trials": trials,
                "new_today": new_today, "new_7d": new_7d}


# ============================================================
#                    КЛАВИАТУРЫ И УТИЛИТЫ
# ============================================================

def esc(s: str) -> str:
    return html.escape(str(s or ""))


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y %H:%M")


def gb(bytes_: int) -> str:
    return f"{bytes_ / 1024**3:.2f}"


def sub_link(uuid: str) -> str:
    return SUB_BASE + uuid


def incy_link(uuid: str) -> str:
    return SUB_BASE + uuid + INCY_SUFFIX


def vless_link(client: dict) -> str:
    links = client.get("inbound_links") or []
    return links[0]["link"] if links else ""


def client_name_for(tg_id: int) -> str:
    return f"tg{tg_id}"


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="📋 Моя подписка"), KeyboardButton(text="🎁 Бесплатная")],
            [KeyboardButton(text="🔑 Мой ключ"), KeyboardButton(text="💳 Купить подписку")],
            [KeyboardButton(text="🍉 Мини-приложение"), KeyboardButton(text="📱 Приложения")],
            [KeyboardButton(text="💬 Поддержка")],
        ],
    )


def miniapp_kb() -> InlineKeyboardMarkup | None:
    if not MINIAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🍉 Открыть приложение", web_app={"url": MINIAPP_URL}),
    ]])


def sub_links_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔗 Ссылка подписки", callback_data="copysub")],
        [InlineKeyboardButton(text="🍏 Для Incy", callback_data="copyincy"),
         InlineKeyboardButton(text="🖼 QR-код", callback_data="qrsub")],
    ]
    mkb = miniapp_kb()
    if mkb:
        rows.append(mkb.inline_keyboard[0])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 QR-код ключа", callback_data="qrkey")],
    ])


def tariffs_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{name} · {devices} устр. — {price} ₽",
            callback_data=f"tariff:{i}")]
        for i, (name, days, devices, price) in enumerate(TARIFFS)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_method_kb(tariff_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Оплатить криптой (CryptoBot)",
                              callback_data=f"cbuy:{tariff_idx}")],
        [InlineKeyboardButton(text="💳 Оплатить картой (перевод)",
                              callback_data=f"kbuy:{tariff_idx}")],
    ])


def pay_url_kb(pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Перейти к оплате в CryptoBot", url=pay_url)],
    ])


def paid_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="i_paid")],
    ])


def admin_confirm_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"payok:{payment_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"payno:{payment_id}")],
    ])


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True,
                               keyboard=[[KeyboardButton(text="❌ Отмена")]])


def qr_photo(data: str) -> BufferedInputFile:
    img = qrcode.make(data, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="qr.png")


# ============================================================
#                    ВЫДАЧА ПОДПИСОК
# ============================================================

async def ensure_client(tg_id: int, days: int, traffic_gb: int, device_limit: int) -> dict:
    """Вернуть клиента панели; создать при отсутствии."""
    user = get_user(tg_id)
    if user and user["client_name"]:
        client = await api.get(user["client_name"])
        if client:
            return client
    name = client_name_for(tg_id)
    client = await api.create(name, days=days, traffic_gb=traffic_gb,
                              device_limit=device_limit)
    set_client(tg_id, name)
    return client


async def grant_days(tg_id: int, days: int, traffic_gb: int, device_limit: int) -> dict:
    """Выдать/продлить подписку: активную продлевает, истёкшую — от сегодня."""
    client = await ensure_client(tg_id, days, traffic_gb, device_limit)
    now = int(time.time())
    if client["expires_at"] < now:  # истекла — новая дата от текущего момента
        client = await api.extend(client["name"], expires_at=now + days * 86400,
                                  traffic_gb=traffic_gb, device_limit=device_limit)
    else:                            # активна — добавляем дни
        client = await api.extend(client["name"], days=days,
                                  traffic_gb=traffic_gb, device_limit=device_limit)
    return client


async def fulfill_payment(p) -> None:
    """Провести оплату: продлить подписку, уведомить юзера и админа."""
    devices = p["devices"] or PAID_DEVICES or 0
    client = await grant_days(p["tg_id"], p["days"], PAID_GB, devices)
    set_payment_status(p["id"], "paid")
    method = "CryptoBot 🪙" if p["method"] == "crypto" else "карта 💳"
    if BOT:
        try:
            await BOT.send_message(
                p["tg_id"],
                f"🎉 <b>Оплата подтверждена!</b> ({method})\n\n"
                f"Подписка продлена на <b>{p['days']} дн.</b>\n"
                f"Действует до: <b>{fmt_ts(client['expires_at'])}</b> (МСК)\n"
                f"Устройств: <b>{devices}</b>\n\n"
                f"Спасибо, что вы с {BRAND} 🍉",
            )
        except Exception:
            pass
        try:
            await BOT.send_message(
                ADMIN_ID,
                f"✅ Оплата #{p['id']} проведена ({method}): "
                f"{p['amount']:.0f} ₽ от <code>{p['tg_id']}</code>. "
                f"До {fmt_ts(client['expires_at'])}",
            )
        except Exception:
            pass


def sub_card(client: dict) -> str:
    left_days = client.get("left_days", 0)
    if client["expires_at"] < time.time():
        status = "⛔ <b>Истекла</b>"
    elif left_days <= 3:
        status = "⏳ <b>Заканчивается</b>"
    else:
        status = "✅ <b>Активна</b>"
    used = client.get("traffic_used_bytes", 0)
    limit = client.get("traffic_limit_bytes", 0)
    limit_txt = "∞" if not limit else gb(limit)
    return (
        f"{BRAND}\n\n"
        f"Статус: {status}\n"
        f"Осталось дней: <b>{max(client['left_days'], 0)}</b>\n"
        f"Действует до: <b>{fmt_ts(client['expires_at'])}</b> (МСК)\n\n"
        f"Трафик: <b>{gb(used)} / {limit_txt} ГБ</b>\n"
        f"Устройства: <b>{client.get('devices_count', 0)} / {client.get('device_limit', 0)}</b>\n\n"
        f"Ссылка обновляется автоматически — при продлении ничего менять не нужно."
    )


# ============================================================
#                 MINI APP: веб-сервер + API
# ============================================================

def validate_init_data(init_data: str) -> int | None:
    """Проверка подписи initData от Telegram.WebApp. Возвращает tg_id."""
    if not init_data:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        return json.loads(pairs.get("user", "{}")).get("id")
    except Exception:
        return None


class MiniAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("miniapp: " + fmt, *args)

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Render пингует HEAD-запросами: отвечаем 200, чтобы сервис считался живым
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            try:
                with open(MINIAPP_HTML, encoding="utf-8") as f:
                    body = f.read().encode()
            except OSError:
                body = b"<h1>miniapp.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        tg_id = validate_init_data(q.get("init_data", [""])[0])
        if not tg_id:
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if parsed.path == "/api/me":
            asyncio.run(self._me(tg_id))
        elif parsed.path == "/api/invoice":
            asyncio.run(self._invoice(tg_id, int(q.get("tariff", ["0"])[0])))
        else:
            self._json({"ok": False, "error": "not_found"}, 404)

    async def _me(self, tg_id: int):
        tariffs = [{"name": n, "days": d, "devices": dv, "price": p}
                   for n, d, dv, p in TARIFFS]
        user = get_user(tg_id)
        resp: dict = {"ok": True, "tariffs": tariffs, "sub": None,
                      "links": None, "status": "none"}
        if user and user["client_name"]:
            try:
                c = await api.get(user["client_name"])
            except PanelError:
                c = None
            if c:
                limit = c.get("traffic_limit_bytes", 0)
                resp["sub"] = {
                    "days": max(c.get("left_days", 0), 0),
                    "until": c["expires_at"],
                    "used": f"{c.get('traffic_used_bytes', 0) / 1024**3:.2f}",
                    "limit": "∞" if not limit else f"{limit / 1024**3:.0f}",
                    "devices": f"{c.get('devices_count', 0)} / {c.get('device_limit', 0)}",
                }
                resp["status"] = "active" if c["expires_at"] > time.time() else "expired"
                resp["links"] = {
                    "sub": sub_link(c["uuid"]),
                    "incy": incy_link(c["uuid"]),
                }
        self._json(resp)

    async def _invoice(self, tg_id: int, tariff_idx: int):
        if not (0 <= tariff_idx < len(TARIFFS)):
            self._json({"ok": False, "error": "Неверный тариф"}, 400)
            return
        if has_pending_payment(tg_id):
            self._json({"ok": False,
                        "error": "У вас уже есть необработанная заявка."}, 400)
            return
        name, days, devices, price = TARIFFS[tariff_idx]
        try:
            inv = await CryptoPay().create_invoice(
                price, f"Арбузик VPN — {name} ({days} дн.)", payload=f"tg{tg_id}")
        except CryptoError as e:
            self._json({"ok": False, "error": f"CryptoBot: {e}"}, 502)
            return
        pid = add_payment(tg_id, price, days, devices, method="crypto",
                          invoice_id=inv["invoice_id"])
        self._json({"ok": True, "pay_url": inv.get("pay_url", ""),
                    "payment_id": pid})


def start_web_server(port: int) -> None:
    server = ThreadingHTTPServer((MINIAPP_HOST, port), MiniAppHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="miniapp-http")
    t.start()
    log.info("Mini App сервер: http://%s:%d", MINIAPP_HOST, port)


# ============================================================
#                 ТУННЕЛЬ cloudflared (HTTPS)
# ============================================================

URL_RE = re.compile(r"https://[a-zA-Z0-9.-]+\.(?:free\.pinggy\.net|run\.pinggy-free\.link|lhr\.life)")
TUNNEL_LIFETIME = 55 * 60  # бесплатный pinggy-туннель живёт 60 минут — пересоздаём раньше

# провайдеры бесплатных ssh-туннелей: (аргументы ssh после опций, имя для лога)
TUNNEL_PROVIDERS = [
    (["-p", "443", "-R0:127.0.0.1:{port}", "-N", "free.pinggy.io"], "pinggy.io"),
    (["-R80:127.0.0.1:{port}", "-N", "nokey@localhost.run"], "localhost.run"),
]


def tunnel_loop(port: int):
    """Локальный режим: держит HTTPS-туннель живым, перебирая провайдеров.

    pinggy даёт URL на 60 минут (пересоздаём за 5 минут до конца);
    если провайдер не дал URL (лимит/недоступен) — пробуем следующий.
    При смене URL автоматически обновляется кнопка меню Telegram.
    """
    global MINIAPP_URL
    provider_idx = 0

    def run_once(args: list[str]) -> tuple[subprocess.Popen | None, str | None]:
        argv = [SSH, "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
        argv += [a.replace("{port}", str(port)) for a in args]
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        found: list[str] = []

        def reader():
            for raw in iter(proc.stderr.readline, b""):
                if found:
                    continue
                m = URL_RE.search(raw.decode("utf-8", "ignore"))
                if m:
                    found.append(m.group(0))

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 90
        while time.time() < deadline and not found:
            if proc.poll() is not None:
                return proc, None
            time.sleep(0.5)
        return proc, (found[0] if found else None)

    while True:
        args, name = TUNNEL_PROVIDERS[provider_idx % len(TUNNEL_PROVIDERS)]
        provider_idx += 1
        try:
            proc, url = run_once(args)
        except OSError as e:
            log.error("ssh-туннель (%s) не запустился: %s", name, e)
            time.sleep(120)
            continue
        started = time.time()
        if url:
            if url != MINIAPP_URL:
                MINIAPP_URL = url
                log.info("Mini App доступен (%s): %s", name, MINIAPP_URL)
                if LOOP and BOT:
                    asyncio.run_coroutine_threadsafe(apply_menu_button(), LOOP)
            # ждём смерти процесса или планового пересоздания
            while proc.poll() is None and time.time() - started < TUNNEL_LIFETIME:
                time.sleep(5)
        else:
            log.warning("Туннель %s: URL не получен за 90с", name)
            time.sleep(60)  # пауза перед следующим провайдером
        if proc and proc.poll() is None:
            proc.terminate()
        log.info("Пересоздаю туннель…")
        time.sleep(5)


def keepalive_loop(url: str):
    """Рендер-режим: пингуем свой URL, чтобы бесплатный инстанс не засыпал."""
    import urllib.request
    while True:
        try:
            urllib.request.urlopen(url + "/", timeout=30)
            log.info("keepalive: %s — ок", url)
        except Exception as e:
            log.warning("keepalive: %s", e)
        time.sleep(600)  # раз в 10 минут (лимит сна — 15 минут)


async def apply_menu_button():
    try:
        await BOT.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🍉 Арбузик",
                                         web_app=WebAppInfo(url=MINIAPP_URL)))
        log.info("Кнопка меню Telegram обновлена")
    except Exception as e:
        log.error("set_chat_menu_button: %s", e)


# ============================================================
#                      ХЕНДЛЕРЫ БОТА
# ============================================================

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME, reply_markup=main_kb())
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Вы администратор. Команды: /stats /user /give /paid /send /reply")


@router.message(F.text == "📋 Моя подписка")
async def my_sub(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user["client_name"]:
        await message.answer(
            "У вас ещё нет подписки 🙁\n\n"
            "🎁 Нажмите «Бесплатная» — получите 3 дня бесплатно\n"
            "💳 Или «Купить подписку» — от 85 ₽",
            reply_markup=main_kb(),
        )
        return
    try:
        client = await api.get(user["client_name"])
    except PanelError as e:
        log.error("panel error: %s", e)
        await message.answer("⚠️ Не удалось связаться с сервером. Попробуйте позже.")
        return
    if client is None:
        await message.answer("⚠️ Подписка не найдена на сервере. Напишите в поддержку.")
        return
    await message.answer(sub_card(client), reply_markup=sub_links_kb())


@router.message(F.text == "🎁 Бесплатная")
async def free_trial(message: Message):
    tg_id = message.from_user.id
    upsert_user(tg_id, message.from_user.username)
    user = get_user(tg_id)
    if user["trial_taken"]:
        await message.answer(
            "🎁 Бесплатная подписка выдаётся один раз.\n"
            "Хотите продолжить — нажмите «💳 Купить подписку», это всего от 85 ₽ 😉"
        )
        return
    try:
        client = await ensure_client(tg_id, TRIAL_DAYS, TRIAL_GB, TRIAL_DEVICES)
    except PanelError as e:
        log.error("trial create failed: %s", e)
        await message.answer("⚠️ Не получилось создать подписку. Попробуйте позже или напишите в поддержку.")
        return
    take_trial(tg_id)
    await message.answer(
        f"🎉 <b>Бесплатная подписка активирована!</b>\n\n"
        f"Дней: <b>{TRIAL_DAYS}</b> · Трафик: <b>{TRIAL_GB} ГБ</b>\n\n"
        f"1) Установите приложение Happ или Incy (раздел «📱 Приложения»)\n"
        f"2) Добавьте подписку по ссылке:\n<code>{esc(sub_link(client['uuid']))}</code>\n\n"
        f"Для Incy используйте:\n<code>{esc(incy_link(client['uuid']))}</code>",
        reply_markup=sub_links_kb(),
    )
    await message.answer_photo(
        qr_photo(sub_link(client["uuid"])),
        caption="Отсканируйте QR в приложении — подписка добавится сама 📲",
    )


@router.message(F.text == "🔑 Мой ключ")
async def my_key(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user["client_name"]:
        await message.answer("Сначала получите подписку: 🎁 «Бесплатная» или 💳 «Купить подписку».")
        return
    try:
        client = await api.get(user["client_name"])
    except PanelError:
        await message.answer("⚠️ Не удалось связаться с сервером. Попробуйте позже.")
        return
    if client is None:
        await message.answer("⚠️ Ключ не найден на сервере. Напишите в поддержку.")
        return
    text = (
        f"{BRAND} — ваш ключ:\n\n"
        f"<b>Ключ (vless):</b>\n<code>{esc(vless_link(client))}</code>\n\n"
        f"<b>Ссылка подписки (Happ, v2tunes, Hiddify):</b>\n"
        f"<code>{esc(sub_link(client['uuid']))}</code>\n\n"
        f"<b>Для Incy:</b>\n<code>{esc(incy_link(client['uuid']))}</code>\n\n"
        f"💡 Лучше использовать ссылку подписки — серверы обновляются автоматически."
    )
    await message.answer(text, reply_markup=key_kb())


@router.message(F.text == "🍉 Мини-приложение")
async def miniapp_menu(message: Message):
    mkb = miniapp_kb()
    if mkb:
        await message.answer(
            "🍉 <b>Мини-приложение Арбузик VPN</b>\n\n"
            "Внутри: статус подписки, покупка за криптовалюту, ссылки.\n"
            "Нажмите кнопку ниже 👇",
            reply_markup=mkb,
        )
    else:
        await message.answer(
            "⚠️ Мини-приложение сейчас недоступно (туннель не поднялся). Попробуйте позже."
        )


@router.message(F.text == "💳 Купить подписку")
async def buy(message: Message):
    if has_pending_payment(message.from_user.id):
        await message.answer(
            "⏳ У вас уже есть необработанная заявка на оплату.\n"
            "Если оплатили давно и ничего не пришло — напишите в 💬 Поддержку."
        )
        return
    lines = [f"{BRAND} — тарифы:", ""]
    for name, days, devices, price in TARIFFS:
        lines.append(f"▫️ <b>{name}</b> — {price} ₽ ({days} дн., {devices} устр.)")
    lines.append(f"\nТрафик: <b>{PAID_GB} ГБ</b>")
    lines.append("Оплата: 🪙 крипта (автоматически) или 💳 карта (перевод)")
    lines.append("\nВыберите тариф 👇")
    await message.answer("\n".join(lines), reply_markup=tariffs_kb())


@router.callback_query(F.data.startswith("tariff:"))
async def tariff_chosen(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    name, days, devices, price = TARIFFS[idx]
    await cb.message.answer(
        f"Тариф: <b>{name}</b> — {price} ₽ ({days} дн., {devices} устр.)\n\n"
        f"Выберите способ оплаты:",
        reply_markup=pay_method_kb(idx),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cbuy:"))
async def crypto_buy(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    name, days, devices, price = TARIFFS[idx]
    if has_pending_payment(cb.from_user.id):
        await cb.answer("У вас уже есть необработанная заявка ⏳", show_alert=True)
        return
    try:
        inv = await CryptoPay().create_invoice(
            price, f"Арбузик VPN — {name} ({days} дн.)", payload=f"tg{cb.from_user.id}")
    except CryptoError as e:
        log.error("invoice failed: %s", e)
        await cb.answer("CryptoBot недоступен, попробуйте позже", show_alert=True)
        return
    add_payment(cb.from_user.id, price, days, devices,
                method="crypto", invoice_id=inv["invoice_id"])
    await cb.message.answer(
        f"🪙 <b>Счёт создан в CryptoBot</b>\n\n"
        f"Тариф: {name} — {price} ₽\n"
        f"Счёт действует 1 час. После оплаты подписка активируется "
        f"автоматически в течение ~1 минуты.",
        reply_markup=pay_url_kb(inv["pay_url"]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("kbuy:"))
async def card_buy(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    name, days, devices, price = TARIFFS[idx]
    await cb.message.answer(
        PAYMENT_INFO.format(user_id=cb.from_user.id, amount=price)
        + f"\n\nТариф: <b>{name}</b> ({days} дн., {devices} устр.)",
        reply_markup=paid_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "i_paid")
async def i_paid(cb: CallbackQuery):
    if has_pending_payment(cb.from_user.id):
        await cb.answer("Заявка уже отправлена, ждите подтверждения ⏳", show_alert=True)
        return
    price, days, devices = TARIFFS[1][3], TARIFFS[1][1], TARIFFS[1][2]
    msg_text = cb.message.text or cb.message.caption or ""
    for n, d, dv, p in TARIFFS:
        if f"{p} ₽" in msg_text:
            price, days, devices = p, d, dv
            break
    pid = add_payment(cb.from_user.id, price, days, devices, method="card")
    user = get_user(cb.from_user.id)
    uname = esc("@" + user["username"]) if user and user["username"] else "без username"
    await cb.bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на оплату #{pid} (карта)</b>\n\n"
        f"Пользователь: {uname} (ID <code>{cb.from_user.id}</code>)\n"
        f"Тариф: <b>{days} дн. — {price} ₽</b>",
        reply_markup=admin_confirm_kb(pid),
    )
    await cb.answer("✅ Заявка отправлена администратору!", show_alert=True)
    await cb.message.answer(
        "✅ Заявка отправлена! После подтверждения оплаты подписка активируется автоматически.\n"
        "Обычно это занимает несколько минут."
    )


@router.callback_query(F.data.startswith("payok:"))
async def pay_ok(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для администратора.", show_alert=True)
        return
    pid = int(cb.data.split(":")[1])
    p = get_payment(pid)
    if not p or p["status"] != "pending":
        await cb.answer("Заявка уже обработана.", show_alert=True)
        return
    try:
        await fulfill_payment(p)
    except PanelError as e:
        log.error("grant failed: %s", e)
        await cb.answer("Ошибка панели! Подписка не выдана.", show_alert=True)
        return
    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА</b>",
        reply_markup=None,
    )
    await cb.answer("Готово, подписка выдана.")


@router.callback_query(F.data.startswith("payno:"))
async def pay_no(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для администратора.", show_alert=True)
        return
    pid = int(cb.data.split(":")[1])
    p = get_payment(pid)
    if not p or p["status"] != "pending":
        await cb.answer("Заявка уже обработана.", show_alert=True)
        return
    set_payment_status(pid, "declined")
    try:
        await cb.bot.send_message(
            p["tg_id"],
            "❌ Заявка на оплату отклонена. Если это ошибка — напишите в 💬 Поддержку.",
        )
    except Exception:
        pass
    await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>ОТКЛОНЕНО</b>",
                               reply_markup=None)
    await cb.answer("Заявка отклонена.")


@router.callback_query(F.data == "copysub")
async def copy_sub(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["client_name"]:
        await cb.answer("Сначала получите подписку.", show_alert=True)
        return
    client = await api.get(user["client_name"])
    if not client:
        await cb.answer("Ключ не найден.", show_alert=True)
        return
    await cb.message.answer(
        f"🔗 Ссылка подписки (нажмите, чтобы скопировать):\n"
        f"<code>{esc(sub_link(client['uuid']))}</code>")
    await cb.answer()


@router.callback_query(F.data == "copyincy")
async def copy_incy(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["client_name"]:
        await cb.answer("Сначала получите подписку.", show_alert=True)
        return
    client = await api.get(user["client_name"])
    if not client:
        await cb.answer("Ключ не найден.", show_alert=True)
        return
    await cb.message.answer(f"🍏 Ссылка для Incy:\n<code>{esc(incy_link(client['uuid']))}</code>")
    await cb.answer()


@router.callback_query(F.data == "qrsub")
async def qr_sub(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["client_name"]:
        await cb.answer("Сначала получите подписку.", show_alert=True)
        return
    client = await api.get(user["client_name"])
    if not client:
        await cb.answer("Ключ не найден.", show_alert=True)
        return
    await cb.message.answer_photo(qr_photo(sub_link(client["uuid"])),
                                  caption="QR ссылки подписки 📲")
    await cb.answer()


@router.callback_query(F.data == "qrkey")
async def qr_key(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["client_name"]:
        await cb.answer("Сначала получите подписку.", show_alert=True)
        return
    client = await api.get(user["client_name"])
    if not client:
        await cb.answer("Ключ не найден.", show_alert=True)
        return
    await cb.message.answer_photo(qr_photo(vless_link(client)),
                                  caption="QR ключа (vless) 📲")
    await cb.answer()


@router.message(F.text == "📱 Приложения")
async def apps_info(message: Message):
    await message.answer(
        "📱 <b>Приложения для {brand}</b>\n\n"
        "<b>Happ</b> (Windows, Android, iOS) — рекомендуем 👍\n"
        "Сайт: happ.su\n\n"
        "<b>Incy</b> (iOS, Android, TV) — новый, быстрый\n"
        "GitHub: github.com/INCY-DEV/incy-platforms\n\n"
        "<b>v2tunes / v2rayNG</b> (Android)\n"
        "Google Play\n\n"
        "<b>Как подключиться:</b>\n"
        "1️⃣ Установите приложение\n"
        "2️⃣ Профиль → Добавить по ссылке → вставьте ссылку подписки (раздел «🔑 Мой ключ»)\n"
        "3️⃣ В списке появится «{brand}» — выберите сервер 🇩🇪 или 🇪🇺 Автовыбор\n\n"
        "Для Incy используйте ссылку с пометкой «для Incy».".replace("{brand}", BRAND)
    )


# ---------- поддержка ----------

class SupportStates(StatesGroup):
    waiting = State()


@router.message(F.text == "💬 Поддержка")
async def support_start(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting)
    await message.answer(
        "💬 Опишите проблему одним сообщением — я передам его администратору.\n"
        "Ответ придёт вам в этот чат.",
        reply_markup=cancel_kb(),
    )


@router.message(SupportStates.waiting, F.text == "❌ Отмена")
async def support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb())


@router.message(SupportStates.waiting)
async def support_msg(message: Message, state: FSMContext):
    await state.clear()
    uname = esc("@" + message.from_user.username) if message.from_user.username else ""
    await message.bot.send_message(
        ADMIN_ID,
        f"💬 <b>Обращение в поддержку</b>\n"
        f"От: {uname} (ID <code>{message.from_user.id}</code>)\n\n"
        f"{esc(message.text)}\n\n"
        f"Ответить: /reply {message.from_user.id} &lt;текст&gt;",
    )
    await message.answer("✅ Сообщение отправлено! Ответ придёт сюда.",
                         reply_markup=main_kb())


# ============================================================
#                         АДМИН
# ============================================================

class AdminFilter(BaseFilter):
    async def __call__(self, event) -> bool:
        return event.from_user is not None and event.from_user.id == ADMIN_ID


admin_router = Router()
admin_router.message.filter(AdminFilter())


@admin_router.message(Command("stats"))
async def admin_stats(message: Message):
    us = users_stats()
    ps = payments_stats()
    active = expired = 0
    for row in all_users_with_client():
        try:
            c = await api.get(row["client_name"])
        except PanelError:
            continue
        if not c:
            continue
        if c["expires_at"] > time.time():
            active += 1
        else:
            expired += 1
    with conn() as c2:
        crypto = c2.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments "
            "WHERE status='paid' AND method='crypto'").fetchone()
        card = c2.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments "
            "WHERE status='paid' AND method='card'").fetchone()
    await message.answer(
        f"📊 <b>Статистика {BRAND}</b>\n\n"
        f"👤 Пользователей в боте: <b>{us['total']}</b>\n"
        f"   · новых за сутки: {us['new_today']}\n"
        f"   · новых за 7 дней: {us['new_7d']}\n"
        f"🎁 Пробных получено: <b>{us['trials']}</b>\n"
        f"🔑 С подпиской: <b>{us['with_sub']}</b>\n"
        f"   · активных: <b>{active}</b>\n"
        f"   · истёкших: <b>{expired}</b>\n\n"
        f"💵 Купили подписку: <b>{ps['buyers']}</b> чел.\n"
        f"💸 Выручка всего: <b>{ps['earned_total']:.0f} ₽</b>\n"
        f"   · CryptoBot: {crypto['s']:.0f} ₽ ({crypto['n']} оплат)\n"
        f"   · Картой: {card['s']:.0f} ₽ ({card['n']} оплат)\n"
        f"💸 Выручка за 30 дней: <b>{ps['earned_30d']:.0f} ₽</b>"
    )


@admin_router.message(Command("user"))
async def admin_user(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /user &lt;tg_id&gt;")
        return
    tg_id = int(parts[1])
    u = get_user(tg_id)
    if not u:
        await message.answer("Пользователь не найден.")
        return
    with conn() as c:
        paid = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments "
            "WHERE tg_id=? AND status='paid'", (tg_id,)).fetchone()
    client_text = "—"
    if u["client_name"]:
        try:
            cl = await api.get(u["client_name"])
            if cl:
                client_text = (
                    f"{cl['status']} · до {fmt_ts(cl['expires_at'])} · "
                    f"{gb(cl.get('traffic_used_bytes', 0))}/{gb(cl.get('traffic_limit_bytes', 0))} ГБ"
                )
        except PanelError:
            client_text = "ошибка панели"
    await message.answer(
        f"👤 <b>Пользователь {tg_id}</b>\n"
        f"Username: {esc(u['username'] or '—')}\n"
        f"В боте с: {fmt_ts(u['created_at'])}\n"
        f"Пробная: {'да' if u['trial_taken'] else 'нет'}\n"
        f"Подписка: {client_text}\n"
        f"Оплат: {paid['n']} на {paid['s']:.0f} ₽"
    )


@admin_router.message(Command("give"))
async def admin_give(message: Message):
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /give &lt;tg_id&gt; &lt;дней&gt; [трафик_гб]")
        return
    tg_id, days = int(parts[1]), int(parts[2])
    traffic = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else PAID_GB
    try:
        client = await grant_days(tg_id, days, traffic, PAID_DEVICES)
    except PanelError as e:
        await message.answer(f"Ошибка: {esc(str(e))}")
        return
    try:
        await message.bot.send_message(
            tg_id,
            f"🎉 Администратор продлил вам подписку на <b>{days} дн.</b>!\n"
            f"Действует до: <b>{fmt_ts(client['expires_at'])}</b> (МСК)",
        )
        sent = " (пользователь уведомлён)"
    except Exception:
        sent = " (не уведомлён — бот не может писать пользователю)"
    await message.answer(
        f"✅ Выдано {days} дн. пользователю {tg_id}{sent}\n"
        f"Действует до: {fmt_ts(client['expires_at'])}"
    )


@admin_router.message(Command("paid"))
async def admin_paid(message: Message):
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: /paid &lt;tg_id&gt; &lt;сумма ₽&gt; [дней]")
        return
    tg_id = int(parts[1])
    amount = float(parts[2].replace(",", "."))
    days = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 30
    pid = add_payment(tg_id, amount, days, method="card")
    set_payment_status(pid, "paid")
    await message.answer(
        f"💰 Записана оплата {amount:.0f} ₽ от {tg_id} ({days} дн.).\n"
        f"Подписку продлите: /give {tg_id} {days}"
    )


@admin_router.message(Command("send"))
async def admin_broadcast(message: Message):
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        await message.answer("Формат: /send &lt;текст рассылки&gt;")
        return
    users = all_users()
    ok = fail = 0
    for row in users:
        try:
            await message.bot.send_message(row["tg_id"], text[1])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(f"📣 Рассылка завершена: доставлено {ok}, ошибок {fail}.")


@admin_router.message(Command("reply"))
async def admin_reply(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: /reply &lt;tg_id&gt; &lt;текст&gt;")
        return
    tg_id = int(parts[1])
    try:
        await message.bot.send_message(tg_id, f"💬 <b>Ответ поддержки:</b>\n\n{esc(parts[2])}")
        await message.answer("✅ Отправлено.")
    except Exception:
        await message.answer("❌ Не удалось отправить (пользователь не писал боту?)")


# ============================================================
#                    ФОНОВЫЕ ЗАДАЧИ
# ============================================================

async def crypto_checker():
    """Раз в 20 секунд проверяет неоплаченные счета CryptoBot."""
    while True:
        try:
            rows = pending_crypto_payments()
            if rows:
                ids = [r["invoice_id"] for r in rows]
                invoices = await CryptoPay().get_invoices(ids)
                by_id = {inv["invoice_id"]: inv for inv in invoices}
                for r in rows:
                    inv = by_id.get(r["invoice_id"])
                    if inv and inv.get("status") == "paid":
                        await fulfill_payment(r)
                    elif not inv and time.time() - r["created_at"] > 3700:
                        # счёт истёк и исчез из active/paid
                        set_payment_status(r["id"], "declined")
        except Exception as e:
            log.error("crypto_checker: %s", e)
        await asyncio.sleep(20)


async def expiry_checker(bot: Bot):
    """Раз в час проверяет подписки: напоминает за 3 дня и об истечении."""
    while True:
        try:
            for row in all_users_with_client():
                try:
                    c = await api.get(row["client_name"])
                except PanelError:
                    continue
                if not c:
                    continue
                left = c["expires_at"] - time.time()
                tg_id = row["tg_id"]
                if left <= 0 and not row["notified_expired"]:
                    await bot.send_message(
                        tg_id,
                        f"⛔ <b>Подписка истекла</b>\n\n"
                        f"Продлите, чтобы продолжать пользоваться {BRAND}:\n"
                        f"💳 «Купить подписку» — от {TARIFFS[0][3]} ₽",
                    )
                    set_notify_flags(tg_id, notified_expired=1)
                elif 0 < left <= 3 * 86400 and not row["notified_3d"]:
                    await bot.send_message(
                        tg_id,
                        f"⏳ <b>Подписка скоро закончится</b>\n\n"
                        f"Осталось дней: <b>{max(c['left_days'], 0)}</b> (до {fmt_ts(c['expires_at'])})\n"
                        f"Не забудьте продлить: 💳 «Купить подписку»",
                    )
                    set_notify_flags(tg_id, notified_3d=1)
                elif left > 3 * 86400 and (row["notified_3d"] or row["notified_expired"]):
                    set_notify_flags(tg_id, notified_3d=0, notified_expired=0)
        except Exception as e:
            log.error("expiry_checker: %s", e)
        await asyncio.sleep(3600)


# ============================================================
#                         ЗАПУСК
# ============================================================

async def main():
    global BOT, LOOP, MINIAPP_URL
    init_db()
    BOT = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    LOOP = asyncio.get_running_loop()

    on_render = bool(os.environ.get("RENDER_EXTERNAL_URL"))

    # Mini App: веб-сервер; адрес — стабильный на Render, туннель — локально
    start_web_server(MINIAPP_PORT)
    if on_render:
        MINIAPP_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
        log.info("Mini App (Render): %s", MINIAPP_URL)
        threading.Thread(target=keepalive_loop, args=(MINIAPP_URL,),
                         daemon=True, name="keepalive").start()
    else:
        threading.Thread(target=tunnel_loop, args=(MINIAPP_PORT,),
                         daemon=True, name="tunnel").start()

    dp = Dispatcher()
    dp.include_routers(admin_router, router)
    me = await BOT.get_me()
    log.info("Бот запущен: @%s", me.username)
    if on_render and MINIAPP_URL:
        await apply_menu_button()  # стабильный URL — ставим кнопку сразу
    tasks = [asyncio.create_task(crypto_checker()),
             asyncio.create_task(expiry_checker(BOT))]
    try:
        await dp.start_polling(BOT)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
