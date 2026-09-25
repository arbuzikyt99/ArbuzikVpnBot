# -*- coding: utf-8 -*-
"""
Arbuzik VPN — Telegram-бот на aiogram 3 (всё в одном файле).

Подключён к панели H1 VLESS (germany-d1.h1cloud.net).
Оплата: CryptoBot (крипта, автоматически).
Mini App: miniapp.html + встроенный веб-сервер + туннель cloudflared.

Запуск: start.bat (или `venv\\Scripts\\python.exe bot.py`).
"""
import asyncio
import base64
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
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, WebAppInfo,
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

# Ссылка подписки — через HTTPS-адрес бота (прокси на панель, стабильно)
# На Render хост подставится автоматически, если SUB_BASE не задан вручную
SUB_BASE = os.environ.get("SUB_BASE", "")  # финальное значение — в main()
INCY_SUFFIX = "?format=xray"  # параметр для приложения Incy

# Оплата через CryptoBot (Crypto Pay API)
CRYPTOBOT_TOKEN = os.environ.get(
    "CRYPTOBOT_TOKEN", "636063:AAQXvXF4uJsA5nlJDlNZ1XGD3bJ6wdH8I2z")
CRYPTOBOT_FIAT = "RUB"  # выставлять счёт в рублях

# Сохранение базы на GitHub (приватный репозиторий), чтобы данные не
# терялись при деплое. Токен передаётся только через переменные окружения.
GH_TOKEN = os.environ.get("GITHUB_DB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_DB_REPO", "arbuzikyt99/arbuzikvpnbot-db")

# Бесплатная подписка: сколько раз можно получить
TRIAL_LIMIT = 2

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

# Mini App
MINIAPP_PORT = int(os.environ.get("PORT", "8080"))   # Render задаёт PORT сам
MINIAPP_HOST = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
MINIAPP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")
SSH = r"C:\Program Files\Git\usr\bin\ssh.exe"  # туннель нужен только локально

BRAND = "Арбузик VPN 🍉"
SUPPORT_USERNAME = "@ArbuzikV_bot"  # юзернейм поддержки (для документации)
PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti--Arbuzik-VPN-09-25"
TERMS_URL = "https://telegra.ph/Polzovatelskoe-soglashenie--Arbuzik-VPN-09-25"
BOT_DESCRIPTION = (
    "Сервис «Арбузик VPN»: доступ к частной сети по подписке. "
    "Тарифы от 85 ₽, пробный период. Информация: /info  ·  verplatega"
)
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
                trial_count INTEGER DEFAULT 0,
                created_at INTEGER,
                notified_3d INTEGER DEFAULT 0,
                notified_expired INTEGER DEFAULT 0
            )
        """)
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "trial_count" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN trial_count INTEGER DEFAULT 0")
            # миграция: учитываем ранее выданные пробные
            c.execute("UPDATE users SET trial_count = 1 WHERE trial_taken = 1")
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
        c.execute("UPDATE users SET trial_taken = 1, trial_count = trial_count + 1 "
                  "WHERE tg_id = ?", (tg_id,))


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


def method_stats() -> dict:
    with conn() as c:
        out = {}
        for m in ("crypto", "card"):
            row = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments "
                "WHERE status='paid' AND method=?", (m,)).fetchone()
            out[m] = {"count": row["n"], "sum": row["s"]}
        return out


def recent_payments(limit: int = 10):
    with conn() as c:
        return c.execute(
            "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


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
            [KeyboardButton(text="ℹ️ Инфо"), KeyboardButton(text="💬 Поддержка")],
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


def pay_url_kb(pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Перейти к оплате в CryptoBot", url=pay_url)],
    ])


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True,
                               keyboard=[[KeyboardButton(text="❌ Отмена")]])


def info_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Политика конфиденциальности", url=PRIVACY_URL)],
        [InlineKeyboardButton(text="📄 Пользовательское соглашение", url=TERMS_URL)],
        [InlineKeyboardButton(text="💳 Тарифы и цены", callback_data="info_tariffs")],
        [InlineKeyboardButton(text="💬 Контакты поддержки", callback_data="info_support")],
    ])


def qr_photo(data: str) -> BufferedInputFile:
    img = qrcode.make(data, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="qr.png")


# ============================================================
#                    ВЫДАЧА ПОДПИСОК
# ============================================================

async def ensure_client(tg_id: int, days: int, traffic_gb: int, device_limit: int) -> dict:
    """Вернуть клиента панели; создать при отсутствии.

    Если клиент с таким именем уже есть в панели (например, после сброса
    базы бота) — подключаем существующий, не создавая заново.
    """
    user = get_user(tg_id)
    if user and user["client_name"]:
        client = await api.get(user["client_name"])
        if client:
            return client
    name = client_name_for(tg_id)
    try:
        client = await api.create(name, days=days, traffic_gb=traffic_gb,
                                  device_limit=device_limit)
    except PanelError as e:
        if "already_exists" in str(e) or "exists" in str(e) or "conflict" in str(e).lower():
            client = await api.get(name)
            if client is None:
                raise
        else:
            raise
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
    backup_now()
    method = "CryptoBot 🪙" if p["method"] == "crypto" else "ручная оплата"
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

        # публичный прокси подписки: /sub/<uuid>[?format=...] → панель
        if parsed.path.startswith("/sub/"):
            asyncio.run(self._sub_proxy(parsed))
            return

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
        elif parsed.path == "/api/admin":
            asyncio.run(self._admin(tg_id))
        else:
            self._json({"ok": False, "error": "not_found"}, 404)

    async def _sub_proxy(self, parsed):
        """Прокси подписки: панель отдаёт конфиги, мы — HTTPS с внешним адресом.

        Пробрасываем User-Agent и x-hwid, чтобы панель корректно считала
        устройства клиента.
        """
        uuid = parsed.path[len("/sub/"):].strip("/")
        if not uuid:
            self._json({"ok": False, "error": "not_found"}, 404)
            return
        url = PANEL_URL.rstrip("/") + "/sub/" + uuid
        if parsed.query:
            url += "?" + parsed.query
        fwd = {}
        for h in ("user-agent", "accept", "x-hwid", "x-device-id"):
            v = self.headers.get(h)
            if v:
                fwd[h] = v
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
                r = await c.get(url, headers=fwd)
        except (httpx.HTTPError, OSError) as e:
            log.error("sub proxy: %s", e)
            self._json({"ok": False, "error": "panel unreachable"}, 502)
            return
        body = r.content
        self.send_response(r.status_code)
        for h in ("content-type", "profile-title", "profile-update-interval",
                  "subscription-userinfo", "announce"):
            if h in r.headers:
                self.send_header(h, r.headers[h])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    async def _me(self, tg_id: int):
        tariffs = [{"name": n, "days": d, "devices": dv, "price": p}
                   for n, d, dv, p in TARIFFS]
        user = get_user(tg_id)
        resp: dict = {"ok": True, "tariffs": tariffs, "sub": None,
                      "links": None, "status": "none",
                      "is_admin": tg_id == ADMIN_ID}
        if user and user["client_name"]:
            try:
                c = await api.get(user["client_name"])
            except PanelError:
                c = None
            if c:
                limit = c.get("traffic_limit_bytes", 0)
                used_gb = c.get("traffic_used_bytes", 0) / 1024**3
                resp["sub"] = {
                    "days": max(c.get("left_days", 0), 0),
                    "until": c["expires_at"],
                    "used": f"{used_gb:.2f}",
                    "limit": "∞" if not limit else f"{limit / 1024**3:.0f}",
                    "used_gb": round(used_gb, 2),
                    "limit_gb": None if not limit else round(limit / 1024**3, 1),
                    "devices": f"{c.get('devices_count', 0)} / {c.get('device_limit', 0)}",
                }
                resp["status"] = "active" if c["expires_at"] > time.time() else "expired"
                resp["links"] = {
                    "sub": sub_link(c["uuid"]),
                    "incy": incy_link(c["uuid"]),
                }
        self._json(resp)

    async def _admin(self, tg_id: int):
        """Статистика для админа. Доступ только у ADMIN_ID."""
        if tg_id != ADMIN_ID:
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        us = users_stats()
        ps = payments_stats()
        ms = method_stats()
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
        pays = [
            {
                "id": p["id"], "tg": p["tg_id"], "amount": p["amount"],
                "days": p["days"], "method": p["method"], "status": p["status"],
                "date": p["created_at"],
            }
            for p in recent_payments(10)
        ]
        self._json({
            "ok": True,
            "is_admin": True,
            "users": us,
            "subs": {"active": active, "expired": expired},
            "money": {
                "total": ps["earned_total"], "month": ps["earned_30d"],
                "buyers": ps["buyers"], "paid_count": ps["paid_count"],
                "crypto": ms["crypto"], "card": ms["card"],
            },
            "payments": pays,
        })

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
#          СОХРАНЕНИЕ БАЗЫ НА GITHUB (приватный репо)
# ============================================================

class GithubDB:
    """Бэкап bot.db в приватный репозиторий через GitHub Contents API.

    Файловая система Render сбрасывается при каждом деплое — благодаря
    этому база пользователей и оплат переживает перезапуски.
    """

    API = "https://api.github.com"

    def __init__(self, token: str, repo: str, path: str):
        self.token = token
        self.repo = repo
        self.path = path          # путь к bot.db на диске
        self.remote_path = "bot.db"
        self._sha: str | None = None
        self._last_hash = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json"}

    def download(self) -> bool:
        """Скачать последнюю базу из репо (если есть) в локальный файл."""
        if not self.token:
            return False
        try:
            with httpx.Client(timeout=30) as c:
                r = c.get(f"{self.API}/repos/{self.repo}/contents/{self.remote_path}",
                          headers=self._headers())
            if r.status_code == 200:
                data = r.json()
                self._sha = data["sha"]
                content = base64.b64decode(data["content"])
                with open(self.path, "wb") as f:
                    f.write(content)
                log.info("База восстановлена из GitHub (%d байт)", len(content))
                return True
            if r.status_code == 404:
                log.info("В репозитории ещё нет базы — начинаем с чистой")
                return False
            log.error("GitHub download: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("GitHub download: %s", e)
        return False

    def upload(self, force: bool = False) -> None:
        """Залить базу в репо, если она изменилась с прошлой загрузки."""
        if not self.token:
            return
        try:
            with open(self.path, "rb") as f:
                content = f.read()
            h = hash(content)
            if h == self._last_hash and not force:
                return
            body: dict = {
                "message": f"db backup {time.strftime('%Y-%m-%d %H:%M')}",
                "content": base64.b64encode(content).decode(),
            }
            if self._sha:
                body["sha"] = self._sha
            with httpx.Client(timeout=30) as c:
                r = c.put(f"{self.API}/repos/{self.repo}/contents/{self.remote_path}",
                          headers=self._headers(), json=body)
            if r.status_code in (200, 201):
                data = r.json()
                self._sha = data["content"]["sha"]
                self._last_hash = h
                log.info("База сохранена на GitHub (%d байт)", len(content))
            else:
                log.error("GitHub upload: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("GitHub upload: %s", e)

    def loop(self, interval: int = 300):
        """Раз в interval секунд сохраняет базу, если она менялась."""
        while True:
            time.sleep(interval)
            self.upload()


def backup_now() -> None:
    if GH_TOKEN:
        try:
            GithubDB(GH_TOKEN, GH_REPO, DB_PATH).upload(force=True)
        except Exception as e:
            log.error("backup_now: %s", e)


async def rebuild_users_from_panel():
    """Восстановить привязку пользователей к клиентам панели.

    Клиенты бота называются tg<tg_id> — если база была потеряна,
    восстанавливаем соответствие по списку клиентов панели.
    """
    try:
        data = await api._request("GET", "/clients")
        clients = data.get("clients") or []
    except PanelError as e:
        log.warning("rebuild: панель недоступна (%s)", e)
        return 0
    n = 0
    for cl in clients:
        name = cl.get("name", "")
        if name.startswith("tg") and name[2:].isdigit():
            tg_id = int(name[2:])
            with conn() as c:
                row = c.execute("SELECT client_name FROM users WHERE tg_id = ?",
                                (tg_id,)).fetchone()
                if row and row["client_name"] == name:
                    continue
                c.execute("""
                    INSERT INTO users(tg_id, client_name, created_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(tg_id) DO UPDATE SET client_name = excluded.client_name
                """, (tg_id, name, int(time.time())))
            n += 1
    if n:
        log.info("Восстановлено привязок из панели: %d", n)
    return n


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
    if user["trial_count"] >= TRIAL_LIMIT:
        await message.answer(
            f"🎁 Бесплатная подписка выдаётся максимум {TRIAL_LIMIT} раза — "
            f"вы уже использовали все.\n"
            "Продлить: «💳 Купить подписку», от 85 ₽ 😉"
        )
        return
    left = TRIAL_LIMIT - user["trial_count"]
    try:
        client = await ensure_client(tg_id, TRIAL_DAYS, TRIAL_GB, TRIAL_DEVICES)
    except PanelError as e:
        log.error("trial create failed: %s", e)
        await message.answer("⚠️ Не получилось создать подписку. Попробуйте позже или напишите в поддержку.")
        return
    take_trial(tg_id)
    backup_now()
    remain_txt = f" (осталось бесплатных: {left - 1})" if left - 1 else " (это была последняя бесплатная)"
    await message.answer(
        f"🎉 <b>Бесплатная подписка активирована!</b>{remain_txt}\n\n"
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
    lines.append("Оплата: 🪙 криптовалюта через CryptoBot (автоматически)")
    lines.append("\nВыберите тариф 👇")
    await message.answer("\n".join(lines), reply_markup=tariffs_kb())


@router.callback_query(F.data.startswith("tariff:"))
async def tariff_chosen(cb: CallbackQuery):
    """Выбор тарифа → сразу создаём счёт в CryptoBot."""
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
        f"Тариф: {name} — {price} ₽ ({days} дн., {devices} устр.)\n"
        f"Счёт действует 1 час. После оплаты подписка активируется "
        f"автоматически в течение ~1 минуты.",
        reply_markup=pay_url_kb(inv["pay_url"]),
    )
    await cb.answer()


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


# ---------- информация (документы, тарифы, поддержка) ----------

@router.message(Command("info"))
@router.message(F.text == "ℹ️ Инфо")
async def info_menu(message: Message):
    await message.answer(
        f"{BRAND}\n\n"
        "ℹ️ <b>Информация о сервисе</b>\n\n"
        "Ниже — документы, тарифы и контакты. "
        "Всегда доступно по команде /info.",
        reply_markup=info_kb(),
    )


@router.callback_query(F.data == "info_tariffs")
async def info_tariffs(cb: CallbackQuery):
    lines = ["💳 <b>Тарифы Арбузик VPN</b>", ""]
    for name, days, devices, price in TARIFFS:
        lines.append(f"▫️ <b>{name}</b> — {price} ₽ ({days} дн., до {devices} устройств)")
    lines.append(f"Трафик: <b>{PAID_GB} ГБ</b>")
    lines.append(f"🎁 Бесплатный пробный период: {TRIAL_DAYS} дн. (не более {TRIAL_LIMIT} раз)")
    lines.append("\nОплата — криптовалютой через CryptoBot. "
                 "Цена и кнопка оплаты показываются перед платежом.")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "info_support")
async def info_support(cb: CallbackQuery):
    await cb.message.answer(
        "💬 <b>Поддержка</b>\n\n"
        f"Телеграм-бот поддержки: {SUPPORT_USERNAME}\n"
        "Опишите проблему в этом боте (кнопка «💬 Поддержка») — "
        "ответ придёт вам в чат.\n\n"
        "Документы: /info",
    )
    await cb.answer()


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
    global BOT, LOOP, MINIAPP_URL, SUB_BASE
    # база: сначала восстановить из GitHub, потом открывать
    if GH_TOKEN:
        GithubDB(GH_TOKEN, GH_REPO, DB_PATH).download()
    init_db()
    BOT = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    LOOP = asyncio.get_running_loop()

    on_render = bool(os.environ.get("RENDER_EXTERNAL_URL"))

    # Адрес подписки: на Render проксируем через свой HTTPS-адрес,
    # если SUB_BASE не задан или указывает на нерабочий sslip-домен
    if on_render:
        ext = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
        if not SUB_BASE or "sslip.io" in SUB_BASE:
            SUB_BASE = ext + "/sub/"
            log.info("SUB_BASE (Render): %s", SUB_BASE)

    # кто-то мог получить подписку, пока бот был выключен — восстанавливаем
    restored = await rebuild_users_from_panel()
    if restored:
        backup_now()

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
    try:
        await BOT.set_my_description(BOT_DESCRIPTION)
        await BOT.set_my_commands([
            BotCommand(command="start", description="Начало работы"),
            BotCommand(command="info", description="Информация: документы, тарифы, поддержка"),
        ])
        log.info("Описание и команды бота обновлены")
    except Exception as e:
        log.error("set_my_description: %s", e)
    if on_render and MINIAPP_URL:
        await apply_menu_button()  # стабильный URL — ставим кнопку сразу
    if GH_TOKEN:
        threading.Thread(target=GithubDB(GH_TOKEN, GH_REPO, DB_PATH).loop,
                         daemon=True, name="db-backup").start()
    tasks = [asyncio.create_task(crypto_checker()),
             asyncio.create_task(expiry_checker(BOT))]
    try:
        await dp.start_polling(BOT)
    finally:
        for t in tasks:
            t.cancel()
        backup_now()


if __name__ == "__main__":
    asyncio.run(main())
