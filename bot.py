"""Telegram Spy Bot for Telegram Business API."""

import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import json
import time
import logging
import sys
import os
import tempfile
import threading
import hmac
import html
import re
import uuid
import urllib3
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import database as db
from config import BOT_TOKEN, ADMIN_ID, PORT
from config import (
    PLATEGA_API_URL,
    PLATEGA_MERCHANT_ID,
    PLATEGA_SECRET,
    PROJECT_NAME,
    PUBLIC_BASE_URL,
    PRIVACY_POLICY_URL,
    TERMS_URL,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
INSTRUCTION_IMAGE_PATHS = [
    os.path.join(os.path.dirname(__file__), "instruction.jpg"),
    os.path.join(os.path.dirname(__file__), "instruction.png"),
]
PRIVACY_IMAGE_PATHS = [
    os.path.join(os.path.dirname(__file__), "privacy.jpg"),
    os.path.join(os.path.dirname(__file__), "privacy.png"),
]

# Prices in Telegram Stars
PRICE_DAILY = 35
PRICE_WEEKLY = 45
PRICE_MONTHLY = 100
PRICE_YEARLY = 850
PAYMENT_PLANS = {
    "daily": {"days": 1, "stars": PRICE_DAILY, "title": "Подписка 1 день"},
    "weekly": {"days": 7, "stars": PRICE_WEEKLY, "title": "Подписка 7 дней"},
    "monthly": {"days": 30, "stars": PRICE_MONTHLY, "title": "Подписка 30 дней"},
    "yearly": {"days": 365, "stars": PRICE_YEARLY, "title": "Подписка 365 дней"},
}
STAR_PLAN_ORDER = ("daily", "weekly", "monthly", "yearly")
PLATEGA_MONTHLY_PLAN_ID = "platega_monthly"
PLATEGA_MONTHLY_DAYS = 30
PLATEGA_MONTHLY_AMOUNT = 120
PLATEGA_SBP_METHOD = 2
MENU_ACTION_TEXTS = {
    "📊 Статус",
    "Статус",
    "⚙️ Настройки",
    "Настройки",
    "📖 Инструкция",
    "🔒 Приватность",
    "💳 Подписка",
    "💳 Купить подписку",
    "📄 Документы",
    "👥 Рефералка",
    "👥 Пригласить друга",
    "💬 Поддержка",
    "◀️ Назад",
}
BOT_COMMANDS = [
    {"command": "start", "description": "Запуск и подключение бота"},
    {"command": "status", "description": "Подключение и статус подписки"},
    {"command": "settings", "description": "Настройки отслеживания"},
    {"command": "subscription", "description": "Купить или продлить подписку"},
    {"command": "help", "description": "Инструкция и пример работы"},
    {"command": "privacy", "description": "Приватность и безопасность"},
    {"command": "referral", "description": "Пригласить друга"},
    {"command": "documents", "description": "Документы и тарифы"},
    {"command": "support", "description": "Написать в поддержку"},
]
COMMAND_ACTIONS = {
    "/status": "📊 Статус",
    "/settings": "⚙️ Настройки",
    "/subscription": "💳 Подписка",
    "/help": "📖 Инструкция",
    "/privacy": "🔒 Приватность",
    "/referral": "👥 Рефералка",
    "/documents": "📄 Документы",
    "/support": "💬 Поддержка",
}

ALLOWED_UPDATES = [
    "message",
    "my_chat_member",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "pre_checkout_query",
]

BOT_USERNAME = ""
MSK = ZoneInfo("Europe/Moscow")
REQUIRED_CHANNEL_USERNAME = "@DialogDelNews"
REQUIRED_CHANNEL_URL = "https://t.me/DialogDelNews"
BUSINESS_RATE_LIMIT_PER_MINUTE = int(os.getenv("BUSINESS_RATE_LIMIT_PER_MINUTE", "120"))
_BUSINESS_RATE_BUCKETS = {}
_BUSINESS_REPLY_MEDIA_SENT = {}
_BUSINESS_REPLY_MEDIA_LOCK = threading.Lock()
_PENDING_LOCKED_MESSAGES = {}
_TELEGRAM_HTTP = urllib3.PoolManager(num_pools=2, maxsize=8)
_MEDIA_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reply-media")
_MEDIA_JOB_SLOTS = threading.BoundedSemaphore(32)
_PREFETCH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="file-prefetch")
_PREFETCH_JOB_SLOTS = threading.BoundedSemaphore(8)
_FILE_PREFETCHES = OrderedDict()
_FILE_PREFETCH_LOCK = threading.Lock()
FILE_PREFETCH_TTL = 5 * 60
FILE_PREFETCH_LIMIT = 32
FILE_PREFETCH_MAX_BYTES = 2 * 1024 * 1024
_REPLY_MEDIA_UPLOAD_ONLY = OrderedDict()
_REPLY_MEDIA_UPLOAD_ONLY_LOCK = threading.Lock()
REPLY_MEDIA_UPLOAD_ONLY_TTL = 60 * 60
REPLY_MEDIA_UPLOAD_ONLY_LIMIT = 2048
TELEGRAM_FILE_DOWNLOAD_TIMEOUT = int(os.getenv("TELEGRAM_FILE_DOWNLOAD_TIMEOUT", "180"))
TELEGRAM_FILE_UPLOAD_TIMEOUT = int(os.getenv("TELEGRAM_FILE_UPLOAD_TIMEOUT", "180"))
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
MAX_WEBHOOK_BODY_BYTES = 1_000_000


def escape_html(value, max_chars: int | None = None) -> str:
    text = "" if value is None else str(value)
    escaped = html.escape(text, quote=True)
    if not max_chars or len(escaped) <= max_chars:
        return escaped

    suffix = "\n\n... [сокращено]"
    budget = max(0, max_chars - len(suffix))
    parts = []
    length = 0
    for char in text:
        escaped_char = html.escape(char, quote=True)
        if length + len(escaped_char) > budget:
            break
        parts.append(escaped_char)
        length += len(escaped_char)
    return "".join(parts) + suffix


def get_business_owner(connection_id: str):
    if not connection_id:
        logging.warning("Business update without connection_id ignored")
        return None
    owner_id = db.get_owner_by_connection(connection_id)
    if not owner_id:
        logging.warning("Unknown business connection ignored: %s", connection_id)
        return None
    return owner_id


def allow_business_event(owner_id: int, connection_id: str) -> bool:
    now = int(time.time())
    window = now // 60
    key = (owner_id, connection_id, window)
    current = _BUSINESS_RATE_BUCKETS.get(key, 0)
    if current >= BUSINESS_RATE_LIMIT_PER_MINUTE:
        if current == BUSINESS_RATE_LIMIT_PER_MINUTE:
            logging.warning("Business event rate limited owner_id=%s connection_id=%s", owner_id, connection_id)
        _BUSINESS_RATE_BUCKETS[key] = current + 1
        return False
    _BUSINESS_RATE_BUCKETS[key] = current + 1
    if len(_BUSINESS_RATE_BUCKETS) > 5000:
        stale_windows = {bucket_key for bucket_key in _BUSINESS_RATE_BUCKETS if bucket_key[2] < window - 2}
        for bucket_key in stale_windows:
            _BUSINESS_RATE_BUCKETS.pop(bucket_key, None)
    return True


def mark_business_reply_media_sent(key: tuple) -> bool:
    now = int(time.time())
    with _BUSINESS_REPLY_MEDIA_LOCK:
        if key in _BUSINESS_REPLY_MEDIA_SENT:
            return False
        _BUSINESS_REPLY_MEDIA_SENT[key] = now
        if len(_BUSINESS_REPLY_MEDIA_SENT) > 5000:
            cutoff = now - 24 * 60 * 60
            stale_keys = [sent_key for sent_key, sent_at in _BUSINESS_REPLY_MEDIA_SENT.items() if sent_at < cutoff]
            for sent_key in stale_keys:
                _BUSINESS_REPLY_MEDIA_SENT.pop(sent_key, None)
    return True


def forget_business_reply_media(key):
    with _BUSINESS_REPLY_MEDIA_LOCK:
        _BUSINESS_REPLY_MEDIA_SENT.pop(key, None)


def submit_media_task(executor, slots, function, *args):
    if not slots.acquire(blocking=False):
        return None
    try:
        future = executor.submit(function, *args)
    except Exception:
        slots.release()
        raise

    def completed(task):
        try:
            if not task.cancelled() and task.exception() is not None:
                logging.error("Background media task failed: %s", type(task.exception()).__name__)
        finally:
            slots.release()

    future.add_done_callback(completed)
    return future


def create_pending_locked_message(user_id: int, event_type: str, chat_link: str, reply_to_message: dict | None = None, saved_text: str | None = None) -> str:
    token = uuid.uuid4().hex
    pending = {
        "user_id": user_id,
        "event_type": event_type,
        "chat_link": chat_link,
        "reply_to_message": reply_to_message,
        "saved_text": saved_text,
        "created_at": int(time.time()),
    }
    db.save_locked_message(token, user_id, pending)
    _PENDING_LOCKED_MESSAGES[token] = pending
    if len(_PENDING_LOCKED_MESSAGES) > 5000:
        cutoff = int(time.time()) - 24 * 60 * 60
        stale_tokens = [
            pending_token
            for pending_token, pending in _PENDING_LOCKED_MESSAGES.items()
            if pending.get("created_at", 0) < cutoff
        ]
        for pending_token in stale_tokens:
            _PENDING_LOCKED_MESSAGES.pop(pending_token, None)
    return token


def get_pending_locked_message(token, user_id):
    pending = _PENDING_LOCKED_MESSAGES.get(token)
    if pending is None:
        pending = db.get_locked_message(token, user_id)
    if not isinstance(pending, dict) or pending.get("user_id") != user_id:
        return None
    if pending.get("created_at", 0) < time.time() - 14 * 24 * 60 * 60:
        _PENDING_LOCKED_MESSAGES.pop(token, None)
        return None
    return pending


def deliver_locked_message(token, user_id, message_id):
    pending = get_pending_locked_message(token, user_id)
    if not pending or not db.is_sub_active(user_id):
        return False
    if not db.claim_locked_message(token, user_id):
        return False
    is_text = bool(pending.get("saved_text"))
    result = {"ok": False}
    try:
        if is_text:
            result = api("editMessageText", chat_id=user_id, message_id=message_id,
                         text=pending["saved_text"], parse_mode="HTML", reply_markup={"inline_keyboard": []})
            if result.get("error_code") == 400 and "message is not modified" in result.get("description", "").lower():
                result = {"ok": True}
        elif pending.get("reply_to_message"):
            reply = pending["reply_to_message"]
            event_type = pending.get("event_type", "deleted")
            caption = (reply_media_notice_caption(pending.get("chat_link", "чатом"), reply.get("date"))
                       if event_type == "reply_media" else
                       f"🗑️ <b>В чате с {pending.get('chat_link', 'чатом')} удалено сообщение</b>")
            result = send_reply_media(user_id, reply, caption, prefer_upload=event_type == "reply_media")
            if result.get("ok"):
                api("editMessageText", chat_id=user_id, message_id=message_id,
                    text="✅ Сообщение отправлено выше.", reply_markup={"inline_keyboard": []})
        return bool(result.get("ok"))
    except Exception:
        result = {"ok": False, "delivery_uncertain": True}
        raise
    finally:
        if result.get("ok"):
            db.finish_locked_message(token, user_id, delivered=True)
            _PENDING_LOCKED_MESSAGES.pop(token, None)
        elif is_text or not result.get("delivery_uncertain"):
            # Editing the original notification is idempotent; media sends are not.
            db.finish_locked_message(token, user_id, delivered=False)


def deliver_requested_locked_messages(user_id):
    try:
        if not db.is_sub_active(user_id):
            return
        for token, message_id in db.get_requested_locked_messages(user_id):
            try:
                deliver_locked_message(token, user_id, message_id)
            except Exception as exc:
                logging.error("Locked message delivery failed: %s", type(exc).__name__)
    except Exception as exc:
        logging.error("Requested messages lookup failed: %s", type(exc).__name__)


def format_ts_msk(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, MSK).strftime("%d.%m.%Y %H:%M")


def format_db_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value[:10]
    return value.strftime("%d.%m.%Y")


def public_url(path: str) -> str:
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else ""


def legal_operator() -> str:
    return "Администрация Сервиса"


def support_contact() -> str:
    return f"встроенная тикет-система в боте @{BOT_USERNAME or PROJECT_NAME}"


def legal_page(title: str, body: str) -> bytes:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title><style>body{{font:16px/1.55 Arial,sans-serif;color:#202124;max-width:800px;margin:40px auto;padding:0 20px}}h1{{font-size:28px}}h2{{font-size:20px;margin-top:28px}}a{{color:#1769aa}}</style></head>
<body><h1>{safe_title}</h1>{body}</body></html>""".encode("utf-8")


def privacy_policy_page() -> bytes:
    operator = html.escape(legal_operator())
    contact = html.escape(support_contact())
    name = html.escape(PROJECT_NAME)
    return legal_page(
        "Политика конфиденциальности",
        f"""
<p>Дата последнего обновления: 12.09.2026</p>
<p>Настоящая политика описывает обработку данных в сервисе <b>{name}</b> (далее — «Сервис»). Оператор: {operator}.</p>
<h2>Какие данные обрабатываются</h2>
<p>Telegram ID, имя, username, сведения о состоянии подписки, технические данные подключения Telegram Business и обращения в поддержку. При использовании функции отслеживания Сервис временно хранит зашифрованные тексты и идентификаторы медиа удалённых или изменённых сообщений, необходимые для отправки уведомления пользователю.</p>
<h2>Для чего</h2>
<p>Для работы функций Сервиса, предоставления подписки, обработки платежей, ответов поддержки, предотвращения злоупотреблений и выполнения требований закона.</p>
<h2>Передача третьим лицам</h2>
<p>Данные могут обрабатываться Telegram для работы бота и Platega для проведения платежа. Платёжные реквизиты карты Сервис не получает и не хранит.</p>
<h2>Хранение и защита</h2>
<p>Содержимое сообщений и file_id хранятся в зашифрованном виде. Доступ к данным ограничен, а технические меры защиты регулярно пересматриваются.</p>
<h2>Права пользователя</h2>
<p>Пользователь может отключить бота, обратиться за уточнением, исправлением или удалением данных через {contact}. Удаление может быть ограничено данными, которые требуется хранить по закону.</p>
<h2>Изменения</h2>
<p>При изменении Политики актуальная версия всегда публикуется по этому адресу.</p>""",
    )


def terms_page() -> bytes:
    operator = html.escape(legal_operator())
    contact = html.escape(support_contact())
    name = html.escape(PROJECT_NAME)
    return legal_page(
        "Пользовательское соглашение",
        f"""
<p>Дата последнего обновления: 12.09.2026</p>
<p>Соглашение регулирует использование сервиса <b>{name}</b>. Исполнитель: {operator}. Начав пользоваться ботом, пользователь принимает эти условия.</p>
<h2>Предмет</h2>
<p>Сервис предоставляет функции уведомления о доступных через Telegram Business удалённых и изменённых сообщениях, а также доступ к ним в период активной подписки.</p>
<h2>Подписка и оплата</h2>
<p>Актуальные тарифы опубликованы на странице «Тарифы». Оплата в рублях проводится через Platega. После подтверждения платежа платёжной системой подписка активируется автоматически. Telegram Stars оплачиваются средствами Telegram.</p>
<h2>Правила использования</h2>
<p>Пользователь обязан соблюдать законодательство, правила Telegram и не использовать Сервис для нарушения прав и конфиденциальности третьих лиц. Пользователь самостоятельно отвечает за законность подключения своего Telegram Business-аккаунта.</p>
<h2>Ограничения</h2>
<p>Сервис зависит от возможностей Telegram и платёжных провайдеров, поэтому не гарантирует получение данных, если они не были доступны Telegram Bot API. Сервис не предназначен для экстренных или критически важных целей.</p>
<h2>Поддержка и возвраты</h2>
<p>По вопросам доступа, оплаты и возврата пользователь обращается через {contact}. Заявления рассматриваются индивидуально с учётом факта предоставления цифрового доступа и требований законодательства.</p>
<h2>Изменение условий</h2>
<p>Исполнитель может обновлять Соглашение. Новая редакция действует с момента публикации по этому адресу.</p>""",
    )


def tariffs_page() -> bytes:
    return legal_page(
        "Тарифы и оплата",
        f"""
<p>Дата последнего обновления: 12.09.2026</p>
<p>Сервис <b>{html.escape(PROJECT_NAME)}</b> предоставляет доступ к уведомлениям об удалённых и изменённых сообщениях Telegram Business.</p>
<h2>Оплата в рублях</h2>
<p><b>30 дней — 120 ₽.</b> Оплата проводится через Platega по СБП / QR-коду. Подписка активируется автоматически после подтверждения оплаты.</p>
<h2>Оплата Telegram Stars</h2>
<p>1 день — {PRICE_DAILY} Stars.<br>7 дней — {PRICE_WEEKLY} Stars.<br>30 дней — {PRICE_MONTHLY} Stars.<br>365 дней — {PRICE_YEARLY} Stars.</p>
<h2>Поддержка</h2>
<p>Контакт: {html.escape(support_contact())}.</p>""",
    )


class HealthHandler(BaseHTTPRequestHandler):
    def respond(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/health", "/healthz"):
            self.respond(200, b"ok")
        elif path == "/privacy-policy":
            self.respond(200, privacy_policy_page(), "text/html; charset=utf-8")
        elif path == "/terms":
            self.respond(200, terms_page(), "text/html; charset=utf-8")
        elif path == "/tariffs":
            self.respond(200, tariffs_page(), "text/html; charset=utf-8")
        elif path == "/payment/success":
            self.respond(200, legal_page("Оплата принята", "<p>Платёж обрабатывается. Вернитесь в Telegram: подписка будет активирована автоматически после подтверждения от платёжной системы.</p>"), "text/html; charset=utf-8")
        elif path == "/payment/failed":
            self.respond(200, legal_page("Оплата не завершена", "<p>Платёж не был завершён. Можно вернуться в Telegram и создать новый счёт.</p>"), "text/html; charset=utf-8")
        else:
            self.respond(404, b"not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/platega-webhook":
            self.respond(404, b"not found")
            return
        if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
            self.respond(503, b"payment integration is not configured")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_WEBHOOK_BODY_BYTES:
                self.respond(413, b"invalid body")
                return
            merchant_id = self.headers.get("X-MerchantId", "")
            secret = self.headers.get("X-Secret", "")
            if not hmac.compare_digest(merchant_id, PLATEGA_MERCHANT_ID) or not hmac.compare_digest(secret, PLATEGA_SECRET):
                logging.warning("Rejected Platega webhook with invalid credentials")
                self.respond(401, b"invalid credentials")
                return

            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            transaction_id = str(payload.get("id", "")).strip()
            raw_amount = payload.get("amount", 0)
            amount = int(Decimal(str(raw_amount)))
            currency = str(payload.get("currency", "")).upper()
            status = str(payload.get("status", "")).upper()
            raw_method = payload.get("paymentMethod")
            payment_method = int(raw_method) if raw_method is not None else None
            if not transaction_id or not amount or currency != "RUB":
                raise ValueError("invalid Platega callback payload")
            logging.info(
                "Platega callback received: transaction_id=%s status=%s amount=%s currency=%s",
                transaction_id,
                status,
                amount,
                currency,
            )

            if status == "CONFIRMED":
                payment = db.apply_platega_payment(transaction_id, amount, currency, payment_method)
                if payment:
                    user_id, days = payment
                    send(user_id, f"✅ <b>Оплата через Platega прошла!</b>\nПодписка на <b>{days} дней</b> активирована.", keyboard=main_keyboard())
                    deliver_requested_locked_messages(user_id)
                else:
                    logging.warning(
                        "Platega confirmation was not granted: transaction_id=%s amount=%s currency=%s",
                        transaction_id,
                        amount,
                        currency,
                    )
            elif status in ("CHARGEBACK", "CHARGEBACKED"):
                payment = db.refund_platega_payment(transaction_id)
                if payment:
                    user_id, days = payment
                    send(user_id, f"↩️ Платёж через Platega возвращён. <b>{days} дней</b> подписки списано.", keyboard=main_keyboard())
            elif status in ("CANCELED", "EXPIRED", "FAILED"):
                db.cancel_platega_payment(transaction_id, status)
            else:
                logging.info("Ignored Platega status %s for transaction %s", status, transaction_id)
            self.respond(200, b"ok")
        except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
            logging.warning("Invalid Platega webhook payload: %s", exc)
            self.respond(400, b"bad request")
        except Exception as exc:
            logging.exception("Platega webhook error: %s", exc)
            self.respond(500, b"internal error")

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info(f"Healthcheck server started on port {PORT}")


def start_cleanup_worker():
    def worker():
        while True:
            try:
                db.cleanup_temp_tables()
            except Exception as exc:
                logging.error("Cleanup error: %s", exc)
            time.sleep(24 * 60 * 60)

    threading.Thread(target=worker, daemon=True).start()


def get_settings(user_id: int) -> dict:
    return db.get_user_settings(user_id)


def connection_reminder_text():
    return (
        "🔔 <b>Не пропусти важное сообщение</b>\n\n"
        "<i>А если собеседник удалит то, что ты не успел прочитать?</i>\n\n"
        "Подключи <b>DialogDelBot</b> к своим чатам, чтобы пользоваться его возможностями:\n\n"
        "<blockquote>"
        "🗑 Уведомления об удалённых сообщениях\n"
        "✏️ Просмотр изменений в переписке\n"
        "📸 Помощь в сохранении фото, видео, голосовых и кружочков, включая одноразовые медиа\n"
        "🎁 Все пользователи нашего бота крутые и участвуют в розыгрышах"
        "</blockquote>\n\n"
        "Нажми <b>«Как подключить»</b> — мы тебя ждём всей командой!"
    )


_REMINDER_BROADCAST_LOCK = threading.Lock()
_REMINDER_BROADCAST_PENDING = {}
_REMINDER_BROADCAST_RUNNING = False


def send_connection_reminder(user_id):
    return send(user_id, connection_reminder_text(), keyboard={"inline_keyboard": [
        [{"text": "Как подключить", "callback_data": "reminder:connect"}],
    ]})


def prepare_reminder_broadcast(chat_id):
    now_ts = int(time.time())
    db.enroll_connection_reminders(now_ts, ADMIN_ID)
    targets = db.get_connection_reminder_targets(ADMIN_ID)
    send_connection_reminder(chat_id)
    with _REMINDER_BROADCAST_LOCK:
        if _REMINDER_BROADCAST_RUNNING:
            send(chat_id, "Рассылка уже выполняется. Дождись итогового сообщения.")
            return
        _REMINDER_BROADCAST_PENDING.clear()
        if not targets:
            send(chat_id, "Нет подходящих получателей: неподключённых и не заблокировавших бота.")
            return
        token = uuid.uuid4().hex[:16]
        _REMINDER_BROADCAST_PENDING[token] = {"targets": targets, "expires": now_ts + 600}
    send(chat_id, f"<b>Отправить приглашение {len(targets)} пользователям?</b>\n\n"
         "Получатели: без активного подключения сейчас, включая ранее отключившихся. "
         "Администратор, заблокировавшие бота и забаненные не включены.\n\n"
         "Текст показан выше. После ручной отправки следующее автоматическое напоминание придёт не раньше чем через 36 часов.\n"
         "Подтверждение действует 10 минут.", keyboard={"inline_keyboard": [[
             {"text": "Отправить", "callback_data": f"remindsend:{token}"},
             {"text": "Отмена", "callback_data": f"remindcancel:{token}"},
         ]]})


def reminder_delivery_outcome(result):
    if result.get("ok"):
        return "sent"
    if result.get("delivery_uncertain") or not result.get("error_code"):
        return "unknown"
    code = result["error_code"]
    description = str(result.get("description") or "").lower()
    if code == 403 and "bot was blocked by the user" in description:
        return "blocked"
    if code in (400, 403) and any(reason in description for reason in (
        "chat not found", "user is deactivated", "bot can't initiate conversation", "bot cannot initiate conversation",
    )):
        return "unavailable"
    if code == 429:
        return "rate_limited"
    return "rejected"


def run_manual_reminder_broadcast(targets, admin_chat_id):
    global _REMINDER_BROADCAST_RUNNING
    counts = dict.fromkeys(("sent", "blocked", "unavailable", "rate_limited", "rejected", "unknown", "internal", "skipped"), 0)
    try:
        for target in targets:
            result = {}
            try:
                if not db.claim_connection_reminder(target["user_id"], int(time.time()), expected_next_at=target["next_at"]):
                    counts["skipped"] += 1
                    continue
                if not db.connection_reminder_allowed(target["user_id"]):
                    counts["skipped"] += 1
                    continue
                result = send_connection_reminder(target["user_id"])
                outcome = reminder_delivery_outcome(result)
            except Exception:
                outcome = "internal"
                logging.exception("Manual reminder failed for user_id=%s", target["user_id"])
            counts[outcome] += 1
            # Do not retry a possibly delivered message; preserve one outcome per recipient.
            try:
                delay = float((result.get("parameters") or {}).get("retry_after", 0))
            except (TypeError, ValueError):
                delay = 0
            time.sleep(max(0.1, min(delay, 300)))
    finally:
        with _REMINDER_BROADCAST_LOCK:
            _REMINDER_BROADCAST_RUNNING = False
    send(admin_chat_id, f"<b>Рассылка завершена</b>\nПолучателей в списке: {len(targets)}\n"
         f"Отправлено: {counts['sent']}\n"
         f"Бот заблокирован: {counts['blocked']}\n"
         f"Чат/аккаунт недоступен или бот не запущен: {counts['unavailable']}\n"
         f"Ограничение частоты Telegram: {counts['rate_limited']}\n"
         f"Другие отказы Telegram: {counts['rejected']}\n"
         f"Результат неизвестен (сеть/ответ API): {counts['unknown']}\n"
         f"Внутренние ошибки: {counts['internal']}\n"
         f"Пропущено: {counts['skipped']}\n\n"
         "«Отправлено» означает успешный ответ Telegram, а не прочтение. "
         "Пропуски: изменилось подключение/доступность либо напоминание уже отправил автоматический обработчик.")


def run_connection_reminders_once():
    now_ts = int(time.time())
    db.enroll_connection_reminders(now_ts, ADMIN_ID)
    for user_id in db.get_due_connection_reminders(now_ts, ADMIN_ID):
        if not db.claim_connection_reminder(user_id, int(time.time())):
            continue
        if not db.connection_reminder_allowed(user_id):
            continue
        send_connection_reminder(user_id)
        time.sleep(0.1)


def start_connection_reminder_worker():
    def worker():
        while True:
            try:
                run_connection_reminders_once()
            except Exception:
                logging.exception("Connection reminder worker failed")
            time.sleep(60)
    threading.Thread(target=worker, name="connection-reminders", daemon=True).start()


def telegram_post(method, data, content_type, timeout):
    try:
        # Never replay a POST: a lost response can still mean delivery succeeded.
        result = _TELEGRAM_HTTP.request(
            "POST", f"{BASE}/{method}", body=data,
            headers={"Content-Type": content_type},
            timeout=urllib3.Timeout(connect=10, read=timeout),
            retries=False, redirect=False,
        )
        try:
            response = json.loads(result.data.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, UnicodeError):
            # An unparseable response must not trigger an upload fallback.
            logging.error("Invalid Telegram response for %s: HTTP %s", method, result.status)
            return {"ok": False, "description": "Invalid Telegram response", "delivery_uncertain": True}
        if result.status >= 400:
            response["ok"] = False
            response.setdefault("error_code", result.status)
        if not response.get("ok"):
            logging.error("Telegram API returned error for %s: %s", method, response)
        return response
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        # Exception text may contain a URL with the bot token.
        logging.error("Telegram API transport error for %s: %s", method, type(exc).__name__)
        return {"ok": False, "description": type(exc).__name__, "delivery_uncertain": True}


def api(method, **params):
    timeout = 65 if method == "getUpdates" else 20
    result = telegram_post(method, json.dumps(params).encode(), "application/json", timeout)
    if method in ("sendMessage", "sendPhoto", "sendVideo", "sendVoice", "sendVideoNote", "sendDocument", "copyMessage"):
        record_delivery_block(params.get("chat_id"), result, params.get("business_connection_id"))
    return result


def record_delivery_block(chat_id, result, business_connection_id=None):
    if business_connection_id or not isinstance(chat_id, int) or chat_id <= 0:
        return
    if result.get("error_code") == 403 and "bot was blocked by the user" in result.get("description", "").lower():
        try:
            db.set_bot_blocked(chat_id, True, int(time.time()))
        except Exception:
            logging.exception("Could not persist bot block for user_id=%s", chat_id)


def setup_command_menu():
    for scope in ("default", "all_private_chats"):
        for language in ("", "ru"):
            result = api("setMyCommands", commands=BOT_COMMANDS, scope={"type": scope}, language_code=language)
            if not result.get("ok"):
                logging.warning("Command menu setup failed scope=%s language=%s", scope, language)
    result = api("setChatMenuButton", menu_button={"type": "commands"})
    if not result.get("ok"):
        logging.warning("Command menu button setup failed")


def is_required_channel_member(user_id: int) -> bool:
    result = api("getChatMember", chat_id=REQUIRED_CHANNEL_USERNAME, user_id=user_id)
    if not result.get("ok"):
        logging.warning("Required channel membership check failed for user_id=%s: %s", user_id, result)
        return False

    member = result.get("result", {})
    status = member.get("status")
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted" and member.get("is_member"):
        return True
    return False


def subscription_gate_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Подписаться на канал", "url": REQUIRED_CHANNEL_URL}],
            [{"text": "✅ Проверить подписку", "callback_data": "check_required_channel"}],
        ]
    }


def send_subscription_gate(chat_id: int):
    return send(
        chat_id,
        "🔒 <b>Перед запуском подпишись на канал</b>\n\n"
        f"Канал: {REQUIRED_CHANNEL_USERNAME}\n\n"
        "После подписки нажми «Проверить подписку».",
        keyboard=subscription_gate_keyboard(),
    )


def send_start_flow(chat_id: int):
    send(chat_id, "Добро пожаловать в DialogDelBot!", keyboard=main_keyboard())
    return send_instruction(chat_id)


def unlock_start_after_channel(user_id: int, chat_id: int):
    ensure_channel_trial(user_id, chat_id, notify=True)
    return send_start_flow(chat_id)


def ensure_channel_trial(user_id: int, chat_id: int | None = None, notify: bool = False) -> bool:
    try:
        granted = db.grant_channel_trial_once(user_id, 7)
    except Exception as exc:
        logging.error("Failed to grant channel trial user_id=%s: %s", user_id, exc)
        return False
    if granted and notify and chat_id:
        send(chat_id, "✅ <b>Подписка на канал найдена!</b>\nВам выдано <b>7 дней доступа</b>.")
    return granted


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        params["reply_markup"] = keyboard
    result = api("sendMessage", **params)
    if not result.get("ok"):
        logging.error("sendMessage failed for chat_id=%s", chat_id)
    return result


def send_photo(chat_id, photo_path, caption="", keyboard=None):
    if not os.path.exists(photo_path):
        logging.error("sendPhoto file not found: %s", photo_path)
        return {"ok": False}

    boundary = f"----CodexBoundary{int(time.time() * 1000)}"
    body = bytearray()

    def add_field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
        add_field("parse_mode", "HTML")
    if keyboard:
        add_field("reply_markup", json.dumps(keyboard, ensure_ascii=False))

    mime_type = mimetypes.guess_type(photo_path)[0] or "image/png"
    filename = os.path.basename(photo_path)
    with open(photo_path, "rb") as photo_file:
        photo_data = photo_file.read()

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode())
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.extend(photo_data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    result = telegram_post("sendPhoto", bytes(body), f"multipart/form-data; boundary={boundary}", 20)
    record_delivery_block(chat_id, result)
    return result


def send_invoice(chat_id: int, title: str, description: str, payload: str, amount: int):
    result = api("sendInvoice",
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[{"label": title, "amount": amount}],
        reply_markup={"inline_keyboard": [[{"text": f"Оплатить {amount} Stars", "pay": True, "style": "success"}]]},
    )
    if not result.get("ok"):
        logging.error("sendInvoice failed for chat_id=%s payload=%s amount=%s", chat_id, payload, amount)
    return result


def stars_plan_label(payload: str, unit: str = "Stars") -> str:
    plan = PAYMENT_PLANS[payload]
    return f"{plan['title'].replace('Подписка ', '')} — {plan['stars']} {unit}"


def stars_plan_lines(unit: str = "Stars") -> str:
    return "\n".join(f"• {stars_plan_label(payload, unit)}" for payload in STAR_PLAN_ORDER)


def stars_plan_keyboard_rows():
    return [
        [{"text": f"⭐ {stars_plan_label(payload)}", "callback_data": f"buy_{payload}", "style": "success"}]
        for payload in STAR_PLAN_ORDER
    ]


MEDIA_UPLOAD_METHODS = {
    "voice": ("sendVoice", "voice"),
    "video_note": ("sendVideoNote", "video_note"),
    "audio": ("sendAudio", "audio"),
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "document": ("sendDocument", "document"),
    "sticker": ("sendSticker", "sticker"),
}
MEDIA_FILE_SUFFIXES = {
    "voice": ".ogg",
    "video_note": ".mp4",
    "audio": ".mp3",
    "photo": ".jpg",
    "video": ".mp4",
    "animation": ".gif",
    "document": "",
    "sticker": ".webp",
}
MEDIA_WITHOUT_CAPTION = {"video_note", "sticker"}
REPLY_MEDIA_SAVE_TYPES = {"photo", "video", "voice", "video_note"}


def send_local_file(chat_id, file_path, file_type, caption=""):
    if not os.path.exists(file_path):
        logging.error("send_local_file file not found: %s", file_path)
        return {"ok": False}

    method, field_name = MEDIA_UPLOAD_METHODS.get(file_type, ("sendDocument", "document"))
    boundary = f"----CodexBoundary{int(time.time() * 1000)}"
    body = bytearray()

    def add_field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    add_field("chat_id", chat_id)
    if caption and file_type not in MEDIA_WITHOUT_CAPTION:
        add_field("caption", caption)
        add_field("parse_mode", "HTML")

    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as media_file:
        media_data = media_file.read()

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode())
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.extend(media_data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    started_at = time.monotonic()
    response = telegram_post(
        method, bytes(body), f"multipart/form-data; boundary={boundary}",
        TELEGRAM_FILE_UPLOAD_TIMEOUT,
    )
    record_delivery_block(chat_id, response)
    logging.info("Telegram media upload file_type=%s elapsed=%.2fs ok=%s", file_type, time.monotonic() - started_at, response.get("ok"))
    if response.get("ok") and caption and file_type in MEDIA_WITHOUT_CAPTION:
        send(chat_id, caption)
    return response


def fetch_telegram_file_info(file_id, file_type):
    started_at = time.monotonic()
    response = api("getFile", file_id=file_id)
    logging.info("Telegram getFile file_type=%s elapsed=%.2fs ok=%s", file_type, time.monotonic() - started_at, response.get("ok"))
    return response


def prepare_telegram_media(file_id, file_type):
    started_at = time.monotonic()
    response = fetch_telegram_file_info(file_id, file_type)
    info = response.get("result", {})
    if not response.get("ok") or not info.get("file_path"):
        return response
    if (info.get("file_size") or 0) > FILE_PREFETCH_MAX_BYTES:
        return response
    path = download_file_from_info(response, file_type, max_bytes=FILE_PREFETCH_MAX_BYTES, started_at=started_at)
    if path:
        try:
            with open(path, "rb") as source:
                response = dict(response, _prefetched_bytes=source.read())
            logging.info("Telegram media prefetch ready file_type=%s bytes=%s", file_type, len(response["_prefetched_bytes"]))
        finally:
            os.unlink(path)
    return response


def prefetch_reply_media(message):
    media_type, file_id = get_support_media(message)
    # Incoming media may lack the view-once flags later present in a reply.
    # Prepare small files too; delivery still requires an authorized reply.
    if media_type not in REPLY_MEDIA_SAVE_TYPES or not file_id:
        return
    now = time.monotonic()
    with _FILE_PREFETCH_LOCK:
        for key, (expires, future) in list(_FILE_PREFETCHES.items()):
            if expires <= now and future.done():
                _FILE_PREFETCHES.pop(key, None)
        if file_id in _FILE_PREFETCHES:
            return
        if len(_FILE_PREFETCHES) >= FILE_PREFETCH_LIMIT:
            # Only completed lookups may be evicted, so a running one is shared.
            completed = next((key for key, (_, job) in _FILE_PREFETCHES.items() if job.done()), None)
            if completed is None:
                return
            _FILE_PREFETCHES.pop(completed)
        future = submit_media_task(
            _PREFETCH_EXECUTOR, _PREFETCH_JOB_SLOTS, prepare_telegram_media, file_id, media_type,
        )
        if future is not None:
            _FILE_PREFETCHES[file_id] = (now + FILE_PREFETCH_TTL, future)
            logging.info("Telegram getFile prefetch queued file_type=%s", media_type)


def get_telegram_file_info(file_id, file_type):
    with _FILE_PREFETCH_LOCK:
        cached = _FILE_PREFETCHES.get(file_id)
        if cached and cached[0] <= time.monotonic() and cached[1].done():
            _FILE_PREFETCHES.pop(file_id, None)
            cached = None
    if cached:
        started_at = time.monotonic()
        try:
            response = cached[1].result()
        except Exception:
            response = {"ok": False}
        logging.info("Telegram getFile prefetch used file_type=%s wait=%.2fs ok=%s", file_type, time.monotonic() - started_at, response.get("ok"))
        if response.get("ok") and response.get("result", {}).get("file_path"):
            return response
        with _FILE_PREFETCH_LOCK:
            if _FILE_PREFETCHES.get(file_id) == cached:
                _FILE_PREFETCHES.pop(file_id, None)
        # An early lookup may fail before Telegram has made the file available.
    else:
        logging.info("Telegram getFile prefetch miss file_type=%s", file_type)
    return fetch_telegram_file_info(file_id, file_type)


def download_telegram_file(file_id, file_type):
    started_at = time.monotonic()
    response = get_telegram_file_info(file_id, file_type)
    if "_prefetched_bytes" in response:
        with tempfile.NamedTemporaryFile(delete=False, suffix=MEDIA_FILE_SUFFIXES.get(file_type, "")) as output:
            try:
                output.write(response["_prefetched_bytes"])
            except Exception:
                output.close()
                os.unlink(output.name)
                raise
        logging.info("Telegram media prefetch hit file_type=%s", file_type)
        return output.name
    return download_file_from_info(response, file_type, started_at=started_at)


def download_file_from_info(response, file_type, max_bytes=None, started_at=None):
    if started_at is None:
        started_at = time.monotonic()
    if not response.get("ok"):
        logging.error("getFile failed for file_type=%s", file_type)
        return None

    file_path = response.get("result", {}).get("file_path", "")
    if not file_path:
        logging.error("getFile returned empty file_path for file_type=%s", file_type)
        return None

    suffix = MEDIA_FILE_SUFFIXES.get(file_type, "")
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    temp_file.close()

    file_url = f"{TELEGRAM_FILE_BASE}/{urllib.parse.quote(file_path, safe='/')}"
    download_started_at = time.monotonic()
    response = None
    try:
        response = _TELEGRAM_HTTP.request(
            "GET", file_url, preload_content=False,
            timeout=urllib3.Timeout(connect=10, read=TELEGRAM_FILE_DOWNLOAD_TIMEOUT),
            retries=False, redirect=False,
        )
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        with open(temp_path, "wb") as output:
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError("Prefetch size limit exceeded")
                output.write(chunk)
        logging.info(
            "Telegram media download file_type=%s elapsed=%.2fs total_with_getFile=%.2fs bytes=%s",
            file_type, time.monotonic() - download_started_at,
            time.monotonic() - started_at, os.path.getsize(temp_path),
        )
        return temp_path
    except Exception as exc:
        logging.error("Telegram file download failed for file_type=%s: %s", file_type, type(exc).__name__)
        if response is not None:
            response.close()
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return None
    finally:
        if response is not None:
            response.release_conn()


def send_downloaded_file(chat_id, file_id, file_type, caption=""):
    temp_path = download_telegram_file(file_id, file_type)
    if not temp_path:
        return {"ok": False}
    try:
        result = send_local_file(chat_id, temp_path, file_type, caption)
        if not result.get("ok") and result.get("error_code") == 400 and file_type == "photo":
            return send_local_file(chat_id, temp_path, "document", caption)
        return result
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def send_file(chat_id, file_id, file_type, caption="", fallback_to_upload=False):
    method_map = {
        "voice": "sendVoice",
        "video_note": "sendVideoNote",
        "audio": "sendAudio",
        "photo": "sendPhoto",
        "video": "sendVideo",
        "animation": "sendAnimation",
        "document": "sendDocument",
        "sticker": "sendSticker",
    }
    method = method_map.get(file_type, "sendDocument")
    if file_type in ("video_note", "sticker"):
        result = api(method, **{"chat_id": chat_id, file_type: file_id})
        if result.get("ok") and caption:
            send(chat_id, caption)
    else:
        params = {"chat_id": chat_id, file_type: file_id}
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"
        result = api(method, **params)
    if not result.get("ok"):
        logging.error("send_file failed for chat_id=%s file_type=%s", chat_id, file_type)
        if fallback_to_upload and result.get("error_code") == 400:
            return send_downloaded_file(chat_id, file_id, file_type, caption)
    return result


def get_support_media(msg: dict):
    if msg.get("photo"):
        return "photo", msg["photo"][-1]["file_id"]
    if msg.get("document"):
        return "document", msg["document"]["file_id"]
    if msg.get("video"):
        return "video", msg["video"]["file_id"]
    if msg.get("animation"):
        return "animation", msg["animation"]["file_id"]
    if msg.get("voice"):
        return "voice", msg["voice"]["file_id"]
    if msg.get("audio"):
        return "audio", msg["audio"]["file_id"]
    if msg.get("video_note"):
        return "video_note", msg["video_note"]["file_id"]
    if msg.get("sticker"):
        return "sticker", msg["sticker"]["file_id"]
    return None, None


def reply_media_needs_upload(media_type, file_id):
    key = (media_type, file_id)
    with _REPLY_MEDIA_UPLOAD_ONLY_LOCK:
        expires = _REPLY_MEDIA_UPLOAD_ONLY.get(key)
        if expires is None:
            return False
        if expires <= time.monotonic():
            _REPLY_MEDIA_UPLOAD_ONLY.pop(key, None)
            return False
        return True


def remember_reply_media_upload_only(media_type, file_id, result):
    if result.get("ok") or result.get("error_code") != 400:
        return
    if "selfdestructing" not in str(result.get("description", "")).lower():
        return
    with _REPLY_MEDIA_UPLOAD_ONLY_LOCK:
        key = (media_type, file_id)
        _REPLY_MEDIA_UPLOAD_ONLY[key] = time.monotonic() + REPLY_MEDIA_UPLOAD_ONLY_TTL
        _REPLY_MEDIA_UPLOAD_ONLY.move_to_end(key)
        while len(_REPLY_MEDIA_UPLOAD_ONLY) > REPLY_MEDIA_UPLOAD_ONLY_LIMIT:
            _REPLY_MEDIA_UPLOAD_ONLY.popitem(last=False)


def send_reply_media(chat_id, reply_to_message: dict, caption="", prefer_upload: bool = False):
    media_type, media_file_id = get_support_media(reply_to_message or {})
    if not media_type or not media_file_id:
        return {"ok": False, "description": "reply has no supported media"}
    with _FILE_PREFETCH_LOCK:
        cached = _FILE_PREFETCHES.get(media_file_id)
        ready = cached and cached[0] > time.monotonic() and cached[1].done()
        if ready:
            try:
                ready = "_prefetched_bytes" in cached[1].result()
            except Exception:
                ready = False
    if ready:
        return send_downloaded_file(chat_id, media_file_id, media_type, caption)
    # Try server-side reuse before downloading and uploading reply media.
    if media_type in REPLY_MEDIA_SAVE_TYPES:
        if reply_media_needs_upload(media_type, media_file_id):
            logging.info("Reply media known self-destructing file_type=%s: using upload", media_type)
            return send_downloaded_file(chat_id, media_file_id, media_type, caption)
        started_at = time.monotonic()
        result = send_file(chat_id, media_file_id, media_type, caption)
        logging.info(
            "Reply media file_id send file_type=%s elapsed=%.2fs ok=%s error_code=%s",
            media_type,
            time.monotonic() - started_at,
            result.get("ok"),
            result.get("error_code"),
        )
        # A timeout can hide a successful send; retry only an explicit rejection.
        if not result.get("ok") and result.get("error_code") == 400:
            remember_reply_media_upload_only(media_type, media_file_id, result)
            return send_downloaded_file(chat_id, media_file_id, media_type, caption)
        return result
    if prefer_upload:
        return send_downloaded_file(chat_id, media_file_id, media_type, caption)
    return send_file(chat_id, media_file_id, media_type, caption, fallback_to_upload=True)


def deliver_business_reply_media(owner_id, msg, key, queued_at):
    started_at = time.monotonic()
    delivery_started = False
    logging.info("Reply media worker queue_wait=%.2fs", started_at - queued_at)
    try:
        if get_business_owner(msg["business_connection_id"]) != owner_id:
            forget_business_reply_media(key)
            return
        if not db.is_sub_active(owner_id):
            forget_business_reply_media(key)
            send(owner_id, "Подписка закончилась. Продлите её и ответьте на сообщение ещё раз.")
            return
        reply = msg["reply_to_message"]
        delivery_started = True
        result = send_reply_media(
            owner_id, reply,
            reply_media_notice_caption(get_chat_link(msg["chat"]), reply.get("date", msg["date"])),
            prefer_upload=True,
        )
        logging.info("Reply media worker elapsed=%.2fs ok=%s", time.monotonic() - started_at, result.get("ok"))
        if result.get("ok"):
            return
        if not result.get("delivery_uncertain"):
            forget_business_reply_media(key)
        logging.warning("Failed to send business reply media owner_id=%s message_id=%s", owner_id, msg.get("message_id"))
        send(owner_id, "Не удалось подтвердить отправку медиа. Telegram мог задержать ответ или файл уже недоступен.")
    except Exception as exc:
        # Keep the deduplication mark if delivery could already have happened.
        if not delivery_started:
            forget_business_reply_media(key)
        logging.error("Business reply media worker failed: %s", type(exc).__name__)


def message_sender_id(message: dict | None):
    if not message:
        return None
    sender = message.get("from") or {}
    return sender.get("id")


def is_reply_media_save_candidate(reply_to_message: dict | None) -> bool:
    if not reply_to_message:
        return False
    media_type, media_file_id = get_support_media(reply_to_message)
    if media_type not in REPLY_MEDIA_SAVE_TYPES or not media_file_id:
        return False
    media_payload = reply_to_message.get(media_type) or {}
    if isinstance(media_payload, list):
        media_payload = media_payload[-1]
    text_hint = " ".join(
        str(value).lower()
        for value in (
            reply_to_message.get("text"),
            reply_to_message.get("caption"),
            reply_to_message.get("quote", {}).get("text") if isinstance(reply_to_message.get("quote"), dict) else "",
        )
        if value
    )
    return bool(
        reply_to_message.get("has_protected_content")
        or reply_to_message.get("has_media_spoiler")
        or reply_to_message.get("ttl_seconds")
        or reply_to_message.get("is_view_once")
        or reply_to_message.get("self_destruct_type")
        or media_payload.get("ttl_seconds")
        or media_payload.get("is_view_once")
        or media_payload.get("self_destruct_type")
        or "однораз" in text_hint
        or "истек" in text_hint
        or "expired" in text_hint
        or "view once" in text_hint
    )


def reply_media_notice_caption(chat_link: str, unix_ts: int | None = None) -> str:
    text = f"📸 <b>Медиа из ответа в чате с {chat_link}</b>"
    if unix_ts:
        text += f"\n🕐 {format_ts_msk(unix_ts)}"
    return text


def save_support_link_from_result(result: dict, user_id: int):
    if not result or not result.get("ok"):
        return
    message = result.get("result", {})
    message_id = message.get("message_id")
    if message_id:
        db.save_support_message_link(message_id, user_id)


def resolve_user_identifier(value: str):
    value = value.strip()
    if value.isdigit():
        return int(value)
    username = value.lstrip("@").lower()
    if not username:
        return None
    found = next((user for user in db.get_all_users() if (user.get("username") or "").lower() == username), None)
    return found["user_id"] if found else None


def payment_report_view(user_id=None, page=1):
    report = db.get_payment_report(user_id=user_id, page=page)
    title = "Все пользователи" if user_id is None else f"Пользователь {user_id}"
    lines = [f"<b>Статистика оплат</b> · {title}", "За всё время\n"]
    for currency, label in (("XTR", "Stars"), ("RUB", "₽ (Platega)")):
        total = report["totals"].get(currency, {})
        gross = total.get("gross", 0)
        refunded = total.get("refunded", 0)
        lines.append(
            f"<b>{label}</b>\n"
            f"Оплачено: {gross:,} · платежей: {total.get('paid_count', 0)}\n"
            f"Возвращено: {refunded:,} · возвратов: {total.get('refund_count', 0)}\n"
            f"После возвратов: {gross - refunded:,}\n"
            f"Плательщиков: {total.get('payers', 0)}\n"
        )
    lines.append("<i>Суммы по базе, без вычета комиссий. Неоплаченные счета в итог не входят.</i>\n")
    heading = "Последние операции" if report["page"] == 1 else "История операций"
    lines.append(f"<b>{heading}</b> · {report['page']}/{report['pages']} · всего {report['total']}")
    statuses = {"PAID": "Оплачено", "REFUNDED": "Возврат", "PENDING": "Ожидает оплаты",
                "FAILED": "Ошибка", "CANCELED": "Отменено", "CANCELLED": "Отменено"}
    for row in report["rows"]:
        username = "@" + row["username"] if row.get("username") else str(row["user_id"])
        name = escape_html(username, 45)
        raw_date = row.get("event_at")
        date = raw_date.strftime("%d.%m.%Y %H:%M") if raw_date else "—"
        unit = "Stars" if row["currency"] == "XTR" else "₽"
        status = escape_html(statuses.get(row["status"], row["status"]), 40)
        lines.append(
            f"\n<b>{row['amount']:,} {unit} · {status}</b>\n"
            f"{name} · <code>{row['user_id']}</code> · {date}\n"
            f"Тариф: {escape_html(row['plan'], 40)}\n"
            f"ID: <code>{escape_html(row['transaction_id'], 120)}</code>"
        )
        if row.get("refunded_at"):
            lines.append("Возвращено: " + row["refunded_at"].strftime("%d.%m.%Y %H:%M"))
    if not report["rows"]:
        lines.append("Платежей пока нет.")
    target = str(user_id) if user_id is not None else "all"
    nav = []
    if report["page"] > 1:
        nav.append({"text": "← Новее", "callback_data": f"payhist:{target}:{report['page'] - 1}"})
    if report["page"] < report["pages"]:
        nav.append({"text": "Ранее →", "callback_data": f"payhist:{target}:{report['page'] + 1}"})
    keyboard = {"inline_keyboard": ([nav] if nav else []) + [[
        {"text": "Последние / Обновить", "callback_data": f"payhist:{target}:1"},
    ]]}
    return "\n".join(lines), keyboard


ADS_HELP = (
    "Создать размещение (цена в рублях):\n"
    "<code>/ads new Название канала 1500</code>\n"
    "Цена — последнее число. Вариант с | тоже работает.\n"
    "Для бесплатного размещения укажи 0.\n"
    "Изменить расходы: <code>/ads cost ID 2000</code>\n"
    "Отчёт: <code>/ads ID</code>\n"
    "Восстановить по времени (МСК): <code>/ads recover ID ДД.ММ.ГГГГ ЧЧ:ММ ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
    "Удалить из списка: <code>/ads delete ID</code> (с подтверждением)"
)


def parse_ad_cost(value):
    value = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", value):
        raise ValueError("Цена должна быть от 0 до 9999999999.99 руб., максимум две цифры после точки.")
    return Decimal(value)


def ads_list_view(page=1):
    report = db.list_ad_campaigns(page=page)
    lines = ["<b>Рекламные размещения</b>", ADS_HELP, ""]
    buttons = []
    for row in report["rows"]:
        lines.append(
            f"<b>#{row['id']} · {escape_html(row['name'])}</b>\n"
            f"Расходы: {row['cost_rub']:,.2f} ₽ · запусков: {row['visitors']} · новых: {row['new_users']}"
        )
        buttons.append([{"text": f"#{row['id']} · {row['name'][:45]}", "callback_data": f"ads:view:{row['id']}"}])
    if not report["rows"]:
        lines.append("Размещений пока нет.")
    lines.append(f"\nСтраница {report['page']}/{report['pages']}")
    nav = []
    if report["page"] > 1:
        nav.append({"text": "Назад", "callback_data": f"ads:list:{report['page'] - 1}"})
    if report["page"] < report["pages"]:
        nav.append({"text": "Далее", "callback_data": f"ads:list:{report['page'] + 1}"})
    if nav:
        buttons.append(nav)
    buttons.append([{"text": "Обновить", "callback_data": f"ads:list:{report['page']}"}])
    return "\n\n".join(lines), {"inline_keyboard": buttons}


def ad_report_view(campaign_id):
    report = db.get_ad_report(campaign_id)
    back = {"text": "Все размещения", "callback_data": "ads:list:1"}
    if not report:
        return "Размещение не найдено.", {"inline_keyboard": [[back]]}
    new_users, payers = report["new_users"], report["payers"]
    cost = Decimal(str(report["cost_rub"]))
    link = f"https://t.me/{BOT_USERNAME}?start=ad_{report['code']}"
    lines = [
        f"<b>#{report['id']} · {escape_html(report['name'])}</b>",
        "<b>В архиве.</b> Новые заходы по ссылке не учитываются." if report.get("archived_at") else f"Ссылка для рекламы:\n{link}",
        f"Расходы: <b>{cost:,.2f} ₽</b>",
        f"Уникальных запусков: <b>{report['visitors']}</b>\n"
        f"Новых пользователей: <b>{new_users}</b>\n"
        f"Уже были в боте: {report['visitors'] - new_users}",
        f"Из новых:\nПодключили бота: <b>{report['connected']}</b>\n"
        f"Подключены сейчас: {report['active_connections']}\n"
        f"Оплатили хотя бы раз: <b>{payers}</b>",
    ]
    if new_users:
        lines.append(f"Конверсия в оплату: {payers / new_users:.1%}\nЦена нового пользователя: {cost / new_users:,.2f} ₽")
    if payers:
        lines.append(f"Цена плательщика: {cost / payers:,.2f} ₽")
    if report.get("recovered_users"):
        lines.append(f"<b>Восстановлено по времени: {report['recovered_users']}</b>\n"
                     "Эти пользователи включены в итоги как предполагаемые, а не подтверждённые переходы из рекламы.")
    for currency, label in (("XTR", "Stars"), ("RUB", "₽")):
        total = report["totals"].get(currency, {})
        gross, refunded = total.get("gross", 0), total.get("refunded", 0)
        lines.append(f"<b>Оплаты · {label}</b>\nПлатежей: {total.get('paid_count', 0)}\n"
                     f"Получено: {gross:,}\nВозвращено: {refunded:,}\nПосле возвратов: {gross - refunded:,}")
    lines.append(
        "<i>Считаются запуски через ссылку, не клики. Повторы не добавляют людей. "
        "Новые пользователи закреплены за первым источником; подключения и оплаты только от них, за всё время. "
        "Плательщики включают возвраты. Суммы без комиссий; Stars не складываются с рублями.</i>"
    )
    buttons = [[
        {"text": "Обновить", "callback_data": f"ads:view:{campaign_id}"}, back,
    ]]
    if not report.get("archived_at"):
        buttons.append([{"text": "Удалить из списка", "callback_data": f"ads:delete:{campaign_id}"}])
    return "\n\n".join(lines), {"inline_keyboard": buttons}


def ad_delete_view(campaign_id):
    report = db.get_ad_report(campaign_id)
    back = {"text": "Все размещения", "callback_data": "ads:list:1"}
    if not report:
        return "Размещение не найдено.", {"inline_keyboard": [[back]]}
    if report.get("archived_at"):
        return "Размещение уже в архиве.", {"inline_keyboard": [[back]]}
    text = (
        f"Удалить из списка <b>#{campaign_id} · {escape_html(report['name'])}</b>?\n\n"
        "Размещение уйдёт в архив. Ссылка по-прежнему откроет бота, но новые заходы учитываться не будут.\n"
        "Пользователи, подписки и история оплат сохранятся. "
        f"Архивный отчёт доступен через <code>/ads {campaign_id}</code>."
    )
    return text, {"inline_keyboard": [[
        {"text": "Подтвердить удаление", "callback_data": f"ads:archive:{campaign_id}"},
        {"text": "Отмена", "callback_data": f"ads:view:{campaign_id}"},
    ]]}


def ads_command_view(text):
    parts = text.split(maxsplit=2)
    if len(parts) == 1:
        return ads_list_view()
    if len(parts) == 3 and parts[1] == "recover":
        fields = parts[2].split()
        if len(fields) != 5 or not re.fullmatch(r"[0-9]{1,10}", fields[0]):
            raise ValueError("Формат: <code>/ads recover 2 05.10.2026 21:00 06.10.2026 21:00</code> (МСК)")
        try:
            start = datetime.strptime(" ".join(fields[1:3]), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
            end = datetime.strptime(" ".join(fields[3:5]), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        except ValueError:
            raise ValueError("Не удалось разобрать даты. Пример: 05.10.2026 21:00") from None
        return ad_recovery_view(int(fields[0]), start, end)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) <= 10:
        return ad_report_view(int(parts[1]))
    if len(parts) == 3 and parts[1] == "delete" and parts[2].isdigit() and len(parts[2]) <= 10:
        return ad_delete_view(int(parts[2]))
    if len(parts) == 3 and parts[1] == "new":
        fields = parts[2].rsplit("|", 1) if "|" in parts[2] else parts[2].rsplit(None, 1)
        if len(fields) != 2:
            raise ValueError("Укажи название и цену в конце, например:\n<code>/ads new Пример 500</code>")
        name = " ".join(fields[0].split())
        if not 1 <= len(name) <= 100:
            raise ValueError("Название должно содержать от 1 до 100 символов.")
        cost = parse_ad_cost(fields[1])
        campaign_id = db.create_ad_campaign(name, uuid.uuid4().hex[:16], cost)
        return ad_report_view(campaign_id)
    if len(parts) == 3 and parts[1] == "cost":
        fields = parts[2].split()
        if len(fields) != 2 or not fields[0].isdigit() or len(fields[0]) > 10:
            raise ValueError(ADS_HELP)
        campaign_id = int(fields[0])
        if not db.set_ad_campaign_cost(campaign_id, parse_ad_cost(fields[1])):
            raise ValueError("Размещение не найдено.")
        return ad_report_view(campaign_id)
    raise ValueError(ADS_HELP)


def ad_recovery_view(campaign_id, start, end, apply=False):
    if not start < end or (end - start).total_seconds() > 31 * 86400:
        raise ValueError("Конец периода должен быть позже начала; период не больше 31 дня.")
    report = db.recover_ad_campaign(campaign_id, start, end, ADMIN_ID, apply=apply)
    period = f"{start.astimezone(MSK):%d.%m.%Y %H:%M} — {end.astimezone(MSK):%d.%m.%Y %H:%M} МСК"
    lines = [f"<b>Восстановление · #{campaign_id} · {escape_html(report['name'])}</b>", period,
             f"Пользователей в базе до начала: {report['users_before']}\n"
             f"Новых за период (без администратора): {report['total']}\n"
             f"Уже закреплены здесь: {report['already_here']}\n"
             f"Другой рекламный источник, пропускаем: {report['other_source']}"]
    if apply:
        lines.append(f"<b>Добавлено: {report['imported']}</b>. Существующие привязки не изменены.")
    else:
        lines.append(f"<b>Будет добавлено: {report['eligible']}</b>\nИз них подключали бота: {report['connected']}")
    lines.append("Привязка предполагаемая, по первому появлению в базе. Органические заходы тоже могут попасть в отчёт. "
                 "Подписки и платежи не меняются. Подключения и оплаты этих пользователей попадут в статистику размещения.")
    if report["pending"]:
        lines.append("Период ещё не закончился. Сейчас учитываются только уже пришедшие. "
                     "После окончания повтори ту же команду: дубликатов не будет, автоматического дозаполнения нет.")
    buttons = [[{"text": "Открыть отчёт", "callback_data": f"ads:view:{campaign_id}"}]]
    if not apply and report["eligible"]:
        buttons.insert(0, [{"text": "Подтвердить восстановление", "callback_data":
                           f"ads:recover:{campaign_id}:{int(start.timestamp())}:{int(end.timestamp())}"}])
    return "\n\n".join(lines), {"inline_keyboard": buttons}


def filter_users(query: str | None):
    users = [user for user in db.get_all_users() if not user.get("bot_blocked")]
    if not query:
        return users
    normalized = query.strip().lower().lstrip("@")
    if not normalized:
        return users
    result = []
    for user in users:
        user_id = str(user.get("user_id", ""))
        username = (user.get("username") or "").lower()
        first_name = (user.get("first_name") or "").lower()
        if normalized in user_id or normalized in username or normalized in first_name:
            result.append(user)
    return result


def is_user_sub_active_row(user: dict) -> bool:
    if not user:
        return False
    if user.get("sub_type") == "banned":
        return False
    expires = user.get("sub_expires")
    if not expires:
        return user.get("sub_type") not in ("expired", "banned") and int(user.get("sub_remaining_seconds") or 0) > 0
    if isinstance(expires, str):
        try:
            expires = datetime.strptime(expires[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return True
    return datetime.now(MSK) < expires.replace(tzinfo=MSK) if getattr(expires, "tzinfo", None) is None else datetime.now(MSK) < expires.astimezone(MSK)


def get_broadcast_targets(scope: str):
    users = [user for user in db.get_all_users() if user.get("user_id") != ADMIN_ID and not user.get("bot_blocked")]
    if scope == "all":
        return [user for user in users if user.get("sub_type") != "banned"]
    if scope == "sub":
        return [user for user in users if is_user_sub_active_row(user)]
    if scope == "conn":
        connected_ids = set(db.get_connected_owner_ids())
        return [user for user in users if user.get("user_id") in connected_ids and user.get("sub_type") != "banned"]
    return []


def render_user_line(user: dict) -> str:
    uid = user["user_id"]
    name = escape_html(user.get("first_name") or user.get("username") or str(uid), 100)
    username = str(user.get("username") or "")
    if TELEGRAM_USERNAME_RE.fullmatch(username):
        link = f'<a href="https://t.me/{username}">@{username}</a>'
    else:
        link = f'<a href="tg://user?id={uid}">{name}</a>'
    sub = escape_html(user.get("sub_type", "?"), 30)
    exp = format_db_date(user.get("sub_expires"))
    sub_icon = "✅" if db.is_sub_active(uid) else "❌"
    conn_count = db.get_connections_count_for_user(uid)
    ref_count = db.get_referral_count(uid)
    automation_status = "🔗 автоподкл" if conn_count else "➖ не подключен"
    return f"{sub_icon} {link} · <code>{uid}</code> · {sub} до {exp} · {automation_status} · реф: {ref_count}"


def users_page_text_and_keyboard(
    users: list[dict],
    page: int,
    query: str = "",
    connected_only: bool = False,
):
    users = [user for user in users if not user.get("bot_blocked")]
    connected_ids = set(db.get_connected_owner_ids())
    visible_connected = sum(user.get("user_id") in connected_ids for user in users)
    if connected_only:
        users = [user for user in users if user.get("user_id") in connected_ids]
    per_page = 10
    total = len(users)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    chunk = users[start:start + per_page]

    header = "🔗 <b>Пользователи, подключившие бота:</b>\n" if connected_only else "👥 <b>Пользователи:</b>\n"
    if query:
        header += f"🔎 Поиск: <code>{escape_html(query, 100)}</code>\n"
    header += f"📄 Страница {page}/{total_pages} · Всего: {total} · Подключены: {visible_connected}\n\n"

    if not chunk:
        text = header + "Ничего не найдено."
    else:
        text = header + "\n".join(render_user_line(user) for user in chunk)

    encoded_query = urllib.parse.quote(query) if query else ""
    keyboard_rows = []
    mode_callback = "users:all:1:" if connected_only else "users:conn:1:"
    mode_text = "👥 Показать всех" if connected_only else "🔗 Кто подключил бота"
    keyboard_rows.append([{"text": mode_text, "callback_data": mode_callback + encoded_query}])
    keyboard_rows.append([
        {"text": "🎁 Реферальная программа", "callback_data": f"users:ref:{page}:{encoded_query}"},
    ])

    nav_prefix = "users:conn" if connected_only else "users:all"
    nav_row = []
    if page > 1:
        nav_row.append({"text": "⬅️", "callback_data": f"{nav_prefix}:{page-1}:{encoded_query}"})
    if page < total_pages:
        nav_row.append({"text": "➡️", "callback_data": f"{nav_prefix}:{page+1}:{encoded_query}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard = {"inline_keyboard": keyboard_rows} if keyboard_rows else None
    return text, keyboard


def referrals_text(page: int = 1, query: str = "") -> tuple[str, dict | None]:
    leaderboard = db.get_referral_leaderboard(limit=15)
    total_referrals = sum(item.get("referrals_count", 0) for item in leaderboard)
    lines = ["🎁 <b>Реферальная программа</b>", "", f"Всего приглашений в топе: <b>{total_referrals}</b>", ""]
    for index, item in enumerate(leaderboard, start=1):
        user_id = item.get("referrer_id")
        username = item.get("username")
        name = item.get("first_name") or username or str(user_id)
        referred = db.get_referrals_by_referrer(user_id)
        preview = []
        for row in referred[:5]:
            referred_name = row.get("first_name") or row.get("username") or str(row.get("referred_id"))
            if row.get("username"):
                referred_name = f"@{row['username']}"
            preview.append(escape_html(referred_name, 100))
        preview_text = ", ".join(preview) if preview else "нет"
        lines.append(
            f"{index}. {escape_html(name, 100)} (<code>{user_id}</code>) — <b>{item.get('referrals_count', 0)}</b>\n"
            f"   Кого привёл: {preview_text}"
        )
    encoded_query = urllib.parse.quote(query) if query else ""
    keyboard = {"inline_keyboard": [[{"text": "🔙 Назад", "callback_data": f"users:all:{page}:{encoded_query}"}]]}
    return "\n".join(lines), keyboard


def copy_message(chat_id: int, from_chat_id: int, message_id: int):
    result = api(
        "copyMessage",
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )
    if not result.get("ok"):
        logging.error("copyMessage failed for chat_id=%s from_chat_id=%s message_id=%s", chat_id, from_chat_id, message_id)
    return result


def run_broadcast(scope: str, source_chat_id: int, text: str | None = None, source_message_id: int | None = None):
    targets = get_broadcast_targets(scope)
    if not targets:
        return {"sent": 0, "failed": 0, "total": 0}

    sent = 0
    failed = 0
    for user in targets:
        target_id = user["user_id"]
        try:
            if source_message_id is not None:
                result = copy_message(target_id, source_chat_id, source_message_id)
            else:
                result = send(target_id, text or "")
            if not result.get("ok") and result.get("error_code") == 429:
                retry_after = result.get("parameters", {}).get("retry_after", 2)
                time.sleep(int(retry_after))
                if source_message_id is not None:
                    result = copy_message(target_id, source_chat_id, source_message_id)
                else:
                    result = send(target_id, text or "")
            if result.get("ok"):
                sent += 1
            else:
                failed += 1
            time.sleep(0.03)
        except Exception as exc:
            failed += 1
            logging.error("Broadcast error for %s: %s", target_id, exc)
    return {"sent": sent, "failed": failed, "total": len(targets)}


def get_ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


def legal_keyboard() -> dict | None:
    privacy_url = PRIVACY_POLICY_URL or public_url("/privacy-policy")
    terms_url = TERMS_URL or public_url("/terms")
    if not all((privacy_url, terms_url)):
        return None
    return {
        "inline_keyboard": [
            [{"text": "🔒 Политика конфиденциальности", "url": privacy_url}],
            [{"text": "📜 Пользовательское соглашение", "url": terms_url}],
        ]
    }


def platega_is_configured() -> bool:
    return bool(PLATEGA_MERCHANT_ID and PLATEGA_SECRET and PUBLIC_BASE_URL)


def create_platega_payment(user_id: int) -> tuple[str | None, str | None]:
    if not platega_is_configured():
        return None, "Оплата рублями временно недоступна. Попробуй Telegram Stars или напиши в поддержку."

    transaction_id = str(uuid.uuid4())
    try:
        db.create_platega_payment(
            transaction_id=transaction_id,
            user_id=user_id,
            plan_id=PLATEGA_MONTHLY_PLAN_ID,
            amount=PLATEGA_MONTHLY_AMOUNT,
            currency="RUB",
            days=PLATEGA_MONTHLY_DAYS,
            payment_method=PLATEGA_SBP_METHOD,
        )
        request_payload = {
            "paymentMethod": PLATEGA_SBP_METHOD,
            "id": transaction_id,
            "paymentDetails": {"amount": PLATEGA_MONTHLY_AMOUNT, "currency": "RUB"},
            "description": f"{PROJECT_NAME}: подписка на {PLATEGA_MONTHLY_DAYS} дней",
            "return": public_url("/payment/success"),
            "failedUrl": public_url("/payment/failed"),
            "payload": f"subscription:{user_id}:{PLATEGA_MONTHLY_PLAN_ID}",
        }
        req = urllib.request.Request(
            f"{PLATEGA_API_URL}/transaction/process",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-MerchantId": PLATEGA_MERCHANT_ID,
                "X-Secret": PLATEGA_SECRET,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        redirect_url = str(result.get("redirect", "")).strip()
        provider_transaction_id = str(result.get("transactionId", transaction_id)).strip()
        if not redirect_url or provider_transaction_id != transaction_id:
            raise ValueError("Platega returned an invalid payment link")
        return redirect_url, None
    except Exception as exc:
        db.mark_platega_payment_failed(transaction_id)
        logging.error("Platega payment creation failed for user_id=%s: %s", user_id, exc)
        return None, "Не удалось создать счёт. Попробуй ещё раз через минуту или напиши в поддержку."


def expired_details_text(user_id: int, event_type: str = "deleted", chat_link: str | None = None) -> str:
    ref_count = db.get_referral_count(user_id)
    if event_type == "edited":
        title = f"В чате с {chat_link} изменено сообщение" if chat_link else "Сообщение изменено"
        event_text = (
            f"✏️ <b>{title}</b>\n\n"
            "Чтобы видеть содержимое изменённых сообщений, продлите подписку.\n\n"
        )
    elif event_type == "reply_media":
        title = f"Медиа из ответа в чате с {chat_link}" if chat_link else "Медиа из ответа"
        event_text = (
            f"📸 <b>{title}</b>\n\n"
            "Чтобы получить это медиа, продлите подписку.\n\n"
        )
    else:
        title = f"В чате с {chat_link} удалено сообщение" if chat_link else "Сообщение удалено"
        event_text = (
            f"🗑️ <b>{title}</b>\n\n"
            "Чтобы увидеть содержимое удалённого сообщения, продлите подписку.\n\n"
        )

    return (
        event_text
        + "⏰ <b>Ваша подписка закончилась</b>\n\n"
        "Для продолжения выберите вариант:\n\n"
        f"👥 <b>Пригласи друга</b> — получи +3 дня бесплатно\n"
        f"Приглашено: {ref_count} чел.\n\n"
        "⭐ <b>Оплата через Telegram Stars:</b>\n"
        f"{stars_plan_lines()}\n\n"
        "💳 <b>Оплата в рублях через СБП / QR:</b>\n"
        "• 30 дней — 120 ₽"
    )


def expired_payment_keyboard(user_id: int):
    return {
        "inline_keyboard": stars_plan_keyboard_rows() + [
            [{"text": "💳 30 дней — 120 ₽ (СБП / QR)", "callback_data": "buy_platega_monthly", "style": "success"}],
            [{"text": "👥 Пригласить друга", "url": get_ref_link(user_id)}],
        ]
    }


def send_expired_message(
    user_id: int,
    event_type: str,
    chat_link: str,
    locked: bool = False,
    reply_to_message: dict | None = None,
    saved_text: str | None = None,
):
    if locked and event_type in ("deleted", "reply_media", "edited"):
        token = create_pending_locked_message(user_id, event_type, chat_link, reply_to_message, saved_text)
        if event_type == "edited":
            title = f"✏️ <b>В чате с {chat_link} изменено сообщение</b>"
            description = "Чтобы увидеть изменения, нажмите кнопку ниже."
        elif event_type == "reply_media":
            title = f"📸 <b>Медиа из ответа в чате с {chat_link}</b>"
            description = "Чтобы получить это медиа, продлите подписку."
        else:
            title = f"🗑️ <b>В чате с {chat_link} удалено сообщение</b>"
            description = "Чтобы видеть содержимое удалённых сообщений, продлите подписку."
        return send(
            user_id,
            f"{title}\n\n{description}",
            keyboard={"inline_keyboard": [[{"text": "Показать сообщение", "callback_data": f"show_locked:{token}"}]]},
        )

    text = expired_details_text(user_id, event_type, chat_link=chat_link)
    return send(user_id, text, keyboard=expired_payment_keyboard(user_id))


def main_keyboard():
    # Remove keyboards left in existing chats by earlier bot versions.
    return {"remove_keyboard": True}


def settings_keyboard(user_id: int):
    s = get_settings(user_id)
    del_icon = "✅" if s["track_deleted"] else "❌"
    edit_icon = "✅" if s["track_edited"] else "❌"
    return {
        "inline_keyboard": [
            [{"text": f"{del_icon} Удалённые сообщения", "callback_data": f"settings:deleted:{int(not s['track_deleted'])}"}],
            [{"text": f"{edit_icon} Изменённые сообщения", "callback_data": f"settings:edited:{int(not s['track_edited'])}"}],
        ],
    }


def instruction_caption() -> str:
    return (
        "🚀 <b>Добро пожаловать в DialogDelBot!</b>\n\n"
        "Бот помогает не терять важные сообщения в Telegram Business и быстро замечать изменения в диалогах.\n\n"
        "🔥 <b>Что умеет бот</b>\n\n"
        "<blockquote>"
        "🗑 Показывает удалённые сообщения\n"
        "✏️ Показывает изменённые сообщения\n"
        "📸 Помогает сохранить одноразовые фото и видео\n"
        "🎥 Поддерживает кружки, видео, фото, голосовые и файлы\n"
        "⚙️ Даёт гибкие настройки отслеживания"
        "</blockquote>\n\n"
        "❓ <b>Как подключить бота</b>\n\n"
        "<blockquote>"
        "1. Нажмите «📄 Скопировать».\n"
        "2. Нажмите «👌 Подключить».\n"
        "3. Откройте <b>🤖 Автоматизация чатов</b>.\n"
        "4. Вставьте скопированный username бота и добавьте его."
        "</blockquote>\n\n"
        "⚠️ <i>Если раздел не появляется, обновите Telegram до последней версии.</i>"
    )


def send_instruction(chat_id: int):
    bot_username = escape_html(BOT_USERNAME)
    copy_text = f"@{bot_username}" if bot_username else "бот"
    keyboard = {
        "inline_keyboard": [
            [{"text": "📄 Скопировать", "copy_text": {"text": copy_text}}],
            [{"text": "👌 Подключить", "url": "tg://settings/edit", "style": "success"}],
            [{"text": "Пример работы", "url": "https://t.me/DialogDelNews/11", "style": "primary"}],
        ]
    }
    image_path = next((path for path in INSTRUCTION_IMAGE_PATHS if os.path.exists(path)), None)
    if image_path:
        send_photo(chat_id, image_path)
        return send(chat_id, instruction_caption(), keyboard=keyboard)
    return send(chat_id, instruction_caption(), keyboard=keyboard)


def get_user_link(user: dict) -> str:
    if not user:
        return "Неизвестный"
    name = (user.get("first_name") or "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    name = escape_html(name.strip() or "Неизвестный", 150)
    username = str(user.get("username") or "")
    uid = user.get("id")
    if TELEGRAM_USERNAME_RE.fullmatch(username):
        return f'<a href="https://t.me/{username}">{name} (@{username})</a>'
    elif uid:
        return f'<a href="tg://user?id={uid}">{name}</a>'
    return name


def get_chat_link(chat: dict) -> str:
    name = (chat.get("first_name") or "")
    if chat.get("last_name"):
        name += " " + chat["last_name"]
    name = escape_html(name.strip() or "Неизвестный", 150)
    username = str(chat.get("username") or "")
    cid = chat.get("id")
    if TELEGRAM_USERNAME_RE.fullmatch(username):
        return f'<a href="https://t.me/{username}">{name} (@{username})</a>'
    elif cid:
        return f'<a href="tg://user?id={cid}">{name}</a>'
    return name


def handle_update(update: dict):
    global _REMINDER_BROADCAST_RUNNING
    logging.info(f"UPDATE: {list(update.keys())}")

    if "my_chat_member" in update:
        member_update = update["my_chat_member"]
        chat = member_update.get("chat", {})
        status = member_update.get("new_chat_member", {}).get("status")
        if chat.get("type") == "private" and status in ("kicked", "member"):
            db.set_bot_blocked(chat["id"], status == "kicked", member_update["date"])
        return

    incoming = update.get("message", {})
    if (incoming.get("chat", {}).get("type") == "private" and incoming.get("from", {}).get("id") == incoming["chat"]["id"]):
        db.set_bot_blocked(incoming["chat"]["id"], False, incoming.get("date", int(time.time())))

    # ── Успешная оплата ────────────────────────────────────
    if "message" in update and update["message"].get("successful_payment"):
        msg = update["message"]
        user_id = msg["from"]["id"]
        payment = msg["successful_payment"]
        payload = payment.get("invoice_payload", "")
        plan = PAYMENT_PLANS.get(payload)
        if not plan:
            logging.error("Unknown successful payment payload: %s", payload)
            send(user_id, "❌ Не удалось определить оплаченный тариф. Напиши в поддержку.")
            return
        telegram_charge_id = payment.get("telegram_payment_charge_id", "")
        amount = payment.get("total_amount")
        currency = payment.get("currency")
        if not telegram_charge_id or amount != plan["stars"] or currency != "XTR":
            logging.error(
                "Invalid successful payment: user_id=%s payload=%s amount=%s currency=%s has_charge=%s",
                user_id,
                payload,
                amount,
                currency,
                bool(telegram_charge_id),
            )
            send(user_id, "❌ Не удалось подтвердить параметры оплаты. Напиши в поддержку.")
            return
        granted = db.apply_stars_purchase(
            user_id=user_id,
            username=msg.get("from", {}).get("username", ""),
            first_name=msg.get("from", {}).get("first_name", ""),
            invoice_payload=payload,
            total_amount=amount,
            currency=currency,
            telegram_payment_charge_id=telegram_charge_id,
            provider_payment_charge_id=payment.get("provider_payment_charge_id", ""),
            days=plan["days"],
        )
        if not granted:
            logging.info("Duplicate successful payment ignored: %s", telegram_charge_id)
            return
        send(
            user_id,
            f"✅ <b>Оплата прошла!</b>\nПодписка на <b>{plan['days']} дней</b> активирована.",
            keyboard=main_keyboard(),
        )
        deliver_requested_locked_messages(user_id)
        return

    # ── Обычное сообщение боту ─────────────────────────────
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        command_parts = text.split(maxsplit=1)
        command = command_parts[0] if command_parts else ""
        if "@" in command and command.startswith("/"):
            name, username = command.split("@", 1)
            if username.lower() != BOT_USERNAME.lower():
                return
            command = name
            text = command + (" " + command_parts[1] if len(command_parts) > 1 else "")
        user = msg.get("from", {})
        chat_id = msg["chat"]["id"]
        user_id = user.get("id")
        if not user_id:
            logging.warning("Message without sender user_id ignored")
            return

        if command == "/start" and chat_id == user_id and user_id != ADMIN_ID:
            payload = text.split(maxsplit=1)
            if len(payload) == 2 and re.fullmatch(r"ad_[a-zA-Z0-9_-]{1,61}", payload[1]):
                db.record_ad_start(payload[1][3:], user_id, user.get("username", ""), user.get("first_name", ""))
        db.save_user(user_id, user.get("username", ""), user.get("first_name", ""))
        s = get_settings(user_id)

        if user_id != ADMIN_ID and not text.startswith("/start"):
            if not is_required_channel_member(user_id):
                send_subscription_gate(chat_id)
                return
            ensure_channel_trial(user_id, chat_id, notify=True)

        if text == "/cancel":
            if s.get("support_mode"):
                s["support_mode"] = False
                db.save_user_settings(
                    user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s.get("support_active", False)
                )
                send(chat_id, "✅ Режим обращения в поддержку выключен.", keyboard=main_keyboard())
            else:
                send(chat_id, "ℹ️ Сейчас режим поддержки не активен.", keyboard=main_keyboard())
            return

        if text.startswith("/reply ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                send(chat_id, "❌ Формат: /reply user_id|@user текст")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден.")
                return
            reply_text = parts[2].strip()
            send(target_id, f"💬 <b>Ответ поддержки</b>\n\n{escape_html(reply_text, 3500)}", keyboard=main_keyboard())
            send(chat_id, f"✅ Ответ отправлен пользователю {target_id}.")
            return

        if user_id == ADMIN_ID and msg.get("reply_to_message"):
            reply_to_message = msg["reply_to_message"]
            target_id = db.get_support_message_link(reply_to_message.get("message_id"))
            if target_id:
                media_type, media_file_id = get_support_media(msg)
                response_text = text or msg.get("caption") or ""
                if media_type and media_file_id:
                    caption = f"💬 <b>Ответ поддержки</b>\n\n{escape_html(response_text, 800)}" if response_text else "💬 <b>Ответ поддержки</b>"
                    send_file(target_id, media_file_id, media_type, caption)
                elif response_text:
                    send(target_id, f"💬 <b>Ответ поддержки</b>\n\n{escape_html(response_text, 3500)}", keyboard=main_keyboard())
                else:
                    send(chat_id, "❌ В ответе нет текста или поддерживаемого файла.")
                    return
                send(chat_id, f"✅ Ответ отправлен пользователю {target_id}.")
                return

        if (
            user_id != ADMIN_ID
            and is_reply_media_save_candidate(msg.get("reply_to_message"))
            and message_sender_id(msg.get("reply_to_message")) != user_id
        ):
            if not db.is_sub_active(user_id):
                send_expired_message(user_id, "reply_media", "ботом", locked=True, reply_to_message=msg["reply_to_message"])
                return
            result = send_reply_media(chat_id, msg["reply_to_message"], prefer_upload=True)
            if result.get("ok"):
                return

        if text.startswith("/closesupport ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send(chat_id, "❌ Формат: /closesupport user_id или /closesupport @username")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            target_settings = db.get_user_settings(target_id)
            db.save_user_settings(
                target_id,
                target_settings.get("track_deleted", True),
                target_settings.get("track_edited", True),
                False,
                False,
            )
            send(chat_id, f"✅ Диалог поддержки с пользователем {target_id} закрыт.")
            send(target_id, "✅ <b>Диалог с поддержкой закрыт.</b>\nЕсли понадобится, нажми «💬 Поддержка» снова.")
            return

        if text == "/supportlist" and user_id == ADMIN_ID:
            active_support = db.get_users_with_active_support()
            if not active_support:
                send(chat_id, "ℹ️ Сейчас нет активных диалогов поддержки.")
                return
            out = "💬 <b>Активные диалоги поддержки</b>\n\n"
            for item in active_support:
                support_user_id = item.get("user_id")
                first_name = escape_html(item.get("first_name") or "Без имени", 100)
                username = escape_html(f"@{item['username']}" if item.get("username") else "без username", 100)
                updated_at = item.get("updated_at")
                updated_text = updated_at.strftime("%d.%m.%Y %H:%M") if updated_at else "—"
                out += (
                    f"• {first_name} ({username})\n"
                    f"ID: <code>{support_user_id}</code>\n"
                    f"Последняя активность: {updated_text}\n"
                    f"Закрыть: <code>/closesupport {support_user_id}</code>\n\n"
                )
            send(chat_id, out)
            return

        if command == "/remind":
            if user_id != ADMIN_ID or chat_id != ADMIN_ID:
                send(chat_id, "Команда доступна только администратору в личном чате с ботом.")
                return
            if text.strip() == "/remind test":
                send_connection_reminder(chat_id)
            elif text.strip() == "/remind":
                prepare_reminder_broadcast(chat_id)
            else:
                send(chat_id, "Рассылка: /remind\nОтправить пример только себе: /remind test")
            return

        if command == "/ads":
            if user_id != ADMIN_ID:
                send(chat_id, "Только для администратора.")
                return
            if chat_id != ADMIN_ID:
                send(chat_id, "Открой статистику в личном чате с ботом.")
                return
            try:
                out, keyboard = ads_command_view(text)
            except ValueError as exc:
                send(chat_id, str(exc))
                return
            send(chat_id, out, keyboard=keyboard)
            return

        if command in ("/payments", "/paystats") and user_id == ADMIN_ID:
            if chat_id != ADMIN_ID:
                send(chat_id, "Открой статистику в личном чате с ботом.")
                return
            parts = text.split(maxsplit=1)
            target_id = resolve_user_identifier(parts[1]) if len(parts) > 1 else None
            if len(parts) > 1 and not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            out, keyboard = payment_report_view(target_id)
            send(chat_id, out, keyboard=keyboard)
            return

        if text.startswith("/refund ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                send(chat_id, "❌ Формат: /refund user_id|@user telegram_payment_charge_id")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден.")
                return
            charge_id = parts[2].strip()
            payment_row = db.get_payment(charge_id)
            if not payment_row:
                send(chat_id, "❌ Платёж с таким charge_id не найден в базе.")
                return
            if payment_row and payment_row.get("refunded"):
                send(chat_id, "ℹ️ Этот платёж уже отмечен как возвращённый.")
                return
            if payment_row and int(payment_row.get("user_id", 0)) != target_id:
                send(chat_id, "❌ Этот charge_id не принадлежит указанному пользователю.")
                return
            payment_plan = PAYMENT_PLANS.get(payment_row.get("invoice_payload"))
            if not payment_plan:
                send(chat_id, "❌ У платежа неизвестный тариф. Возврат остановлен, чтобы не повредить подписку.")
                return
            result = api(
                "refundStarPayment",
                user_id=target_id,
                telegram_payment_charge_id=charge_id,
            )
            if result.get("ok"):
                db.refund_stars_purchase(charge_id, payment_plan["days"])
                send(chat_id, f"✅ Возврат выполнен для user_id={target_id}.")
                send(target_id, "✅ <b>Оплата возвращена.</b>\nStars должны вернуться на твой баланс Telegram.")
            else:
                send(chat_id, "❌ Не удалось сделать возврат. Проверь charge_id и логи.")
            return

        if (text.startswith("/cancelsub ") or text.startswith("/unsub ")) and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send(chat_id, "❌ Формат: /unsub user_id или /unsub @username")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            db.set_subscription(target_id, "expired", 0)
            send(chat_id, f"✅ Подписка пользователя {target_id} отменена.")
            try:
                send(target_id, "⚠️ <b>Подписка отключена администратором.</b>", keyboard=main_keyboard())
            except Exception:
                pass
            return

        is_menu_action = command in COMMAND_ACTIONS or command == "/start" or text in MENU_ACTION_TEXTS
        if s.get("support_mode") and is_menu_action:
            s["support_mode"] = False
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], False, s.get("support_active", False))

        if s.get("support_mode") and user_id != ADMIN_ID and not text.startswith("/"):
            username = escape_html(user.get("username") or "", 100)
            first_name = escape_html(user.get("first_name") or "Без имени", 100)
            media_type, media_file_id = get_support_media(msg)
            message_body = escape_html(text or msg.get("caption") or "[не текстовое сообщение]", 3000)
            header = (
                f"💬 <b>Новое обращение в поддержку</b>\n\n"
                f"👤 Пользователь: {first_name}\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"{'🔗 @' + username if username else '🔗 username не указан'}\n\n"
                f"<b>Сообщение:</b>\n{message_body}"
            )
            header_result = send(ADMIN_ID, header)
            save_support_link_from_result(header_result, user_id)
            if media_type and media_file_id:
                media_result = send_file(ADMIN_ID, media_file_id, media_type)
                save_support_link_from_result(media_result, user_id)
            s["support_mode"] = False
            s["support_active"] = True
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s["support_active"])
            send(
                chat_id,
                "✅ Сообщение отправлено в поддержку. Ответ придёт сюда.\n\n"
                "Диалог с поддержкой открыт. Можешь продолжать писать сюда, и сообщения будут пересылаться админу.",
                keyboard=main_keyboard(),
            )
            return

        if s.get("support_active") and user_id != ADMIN_ID:
            if text.startswith("/") or text in MENU_ACTION_TEXTS:
                pass
            else:
                username = escape_html(user.get("username") or "", 100)
                first_name = escape_html(user.get("first_name") or "Без имени", 100)
                media_type, media_file_id = get_support_media(msg)
                message_body = escape_html(text or msg.get("caption") or "[не текстовое сообщение]", 3000)
                header = (
                    f"💬 <b>Сообщение в открытом диалоге поддержки</b>\n\n"
                    f"👤 Пользователь: {first_name}\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"{'🔗 @' + username if username else '🔗 username не указан'}\n\n"
                    f"<b>Сообщение:</b>\n{message_body}"
                )
                header_result = send(ADMIN_ID, header)
                save_support_link_from_result(header_result, user_id)
                if media_type and media_file_id:
                    media_result = send_file(ADMIN_ID, media_file_id, media_type)
                    save_support_link_from_result(media_result, user_id)
                return

        text = COMMAND_ACTIONS.get(command, text)

        # Реферальная ссылка
        if text.startswith("/start ref_"):
            referrer_str = text.replace("/start ref_", "").strip()
            if referrer_str.isdigit():
                referrer_id = int(referrer_str)
                if referrer_id != user_id:
                    db.add_referral(referrer_id, user_id)

        if text.startswith("/start"):
            if is_required_channel_member(user_id):
                unlock_start_after_channel(user_id, chat_id)
            else:
                send_subscription_gate(chat_id)

        elif text in ("📊 Статус", "Статус"):
            is_connected = db.get_connections_count_for_user(user_id) or db.get_connections_count_for_user(chat_id)
            status = "🟢 Подключён" if is_connected else "🔴 Не подключён"
            sub_active = db.is_sub_active(user_id)
            user_data = db.get_user(user_id)
            sub_type = user_data.get("sub_type", "trial") if user_data else "trial"
            sub_expires = format_db_date(user_data.get("sub_expires")) if user_data else "—"
            remaining_seconds = int(user_data.get("sub_remaining_seconds") or 0) if user_data else 0
            if not is_connected:
                if remaining_seconds > 0:
                    sub_reason = "подписка на паузе — подключи бота, и оставшееся время возобновится"
                else:
                    sub_reason = "нет подключённых чатов"
            elif sub_active:
                sub_reason = "активна"
            elif remaining_seconds > 0:
                sub_reason = "пауза до следующего подключения"
            else:
                sub_reason = "истекла"
            del_icon = "✅" if s["track_deleted"] else "❌"
            edit_icon = "✅" if s["track_edited"] else "❌"
            send(chat_id,
                f"📊 <b>Статус</b>\n\n"
                f"Подключение: {status}\n"
                f"Подписка: {'✅ Активна' if sub_active else '❌ Не активна'} ({sub_type})\n"
                f"Причина: {sub_reason}\n"
                f"До: {sub_expires}\n"
                f"Удалённые: {del_icon}\n"
                f"Изменённые: {edit_icon}\n\n"
                + ("" if is_connected else "⏸ Подписка не сгорает, пока бот не подключён.\nДобавь бота в <b>Автоматизация чатов</b>, чтобы она продолжила идти."),
                keyboard=main_keyboard()
            )

        elif text in ("⚙️ Настройки", "Настройки"):
            send(chat_id, "⚙️ <b>Настройки</b>\n\nВыбери что отслеживать:", keyboard=settings_keyboard(user_id))

        elif text in ("✅ Удалённые сообщения", "❌ Удалённые сообщения", "Удалённые сообщения"):
            s["track_deleted"] = not s["track_deleted"]
            db.save_user_settings(
                user_id,
                s["track_deleted"],
                s["track_edited"],
                s.get("support_mode", False),
                s.get("support_active", False),
            )
            send(chat_id, f"Удалённые сообщения: {'✅' if s['track_deleted'] else '❌'}", keyboard=settings_keyboard(user_id))

        elif text in ("✅ Изменённые сообщения", "❌ Изменённые сообщения", "Изменённые сообщения"):
            s["track_edited"] = not s["track_edited"]
            db.save_user_settings(
                user_id,
                s["track_deleted"],
                s["track_edited"],
                s.get("support_mode", False),
                s.get("support_active", False),
            )
            send(chat_id, f"Изменённые сообщения: {'✅' if s['track_edited'] else '❌'}", keyboard=settings_keyboard(user_id))

        elif text == "◀️ Назад":
            send(chat_id, "Главное меню:", keyboard=main_keyboard())

        elif text in ("💳 Подписка", "💳 Купить подписку"):
            send(chat_id,
                f"💳 <b>Купить подписку</b>\n\n"
                f"⭐ <b>Telegram Stars:</b>\n"
                f"{stars_plan_lines('Telegram Stars')}\n\n"
                "💳 СБП / QR через Platega: 30 дней — 120 ₽",
                keyboard={
                    "inline_keyboard": stars_plan_keyboard_rows() + [
                        [{"text": "💳 30 дней — 120 ₽ (СБП / QR)", "callback_data": "buy_platega_monthly", "style": "success"}],
                    ]
                }
            )

        elif text in ("📄 Документы",):
            keyboard = legal_keyboard()
            if keyboard:
                send(
                    chat_id,
                    "📄 <b>Документы и тарифы</b>\n\n"
                    "Политика, пользовательское соглашение и актуальные условия оплаты доступны по кнопкам ниже.",
                    keyboard=keyboard,
                )
            else:
                send(
                    chat_id,
                    "📄 Документы готовятся к публикации. Напиши в поддержку, если нужны ссылки.",
                    keyboard=main_keyboard(),
                )

        elif text in ("👥 Рефералка", "👥 Пригласить друга"):
            ref_link = get_ref_link(user_id)
            ref_count = db.get_referral_count(user_id)
            share_url = f"https://t.me/share/url?url={ref_link}&text=Попробуй%20этого%20бота!"
            send(chat_id,
                f"👥 <b>Пригласи друга — получи +3 дня</b>\n\n"
                f"За каждого друга, который подключит бота по твоей ссылке, — "
                f"ты получаешь <b>+3 дня</b> автоматически.\n\n"
                f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
                f"Приглашено друзей: <b>{ref_count}</b>",
                keyboard={"inline_keyboard": [[{"text": "📤 Поделиться", "url": share_url}]]}
            )

        elif text in ("💬 Поддержка",):
            s["support_mode"] = True
            s["support_active"] = False
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s["support_active"])
            send(
                chat_id,
                "💬 <b>Поддержка</b>\n\n"
                "Напиши одним сообщением, что случилось.\n"
                "Можно отправить текст, и я перешлю его админу.\n\n"
                "Для отмены отправь <code>/cancel</code>",
            )

        elif text in ("🔒 Приватность",):
            privacy_caption = (
                "🔒 <b>Приватность и безопасность</b>\n\n"
                "✅ Бот работает через <b>официальный Telegram Bot API</b>\n"
                "✅ Уведомления об удалённых и изменённых сообщениях приходят <b>только тебе</b>\n"
                "✅ Тексты сообщений и file_id хранятся только в зашифрованном виде\n"
                "✅ В базе данных нет читаемых переписок\n"
                "✅ Ключ шифрования хранится отдельно от БД\n"
                "✅ Логи не содержат содержимое сообщений\n"
                "✅ Бота можно отключить в любой момент\n\n"
            )
            privacy_image_path = next((path for path in PRIVACY_IMAGE_PATHS if os.path.exists(path)), None)
            if privacy_image_path:
                send_photo(chat_id, privacy_image_path, caption=privacy_caption, keyboard=main_keyboard())
            else:
                send(
                    chat_id,
                    privacy_caption + "\n\n<i>Положи файл <code>privacy.jpg</code> или <code>privacy.png</code> рядом с ботом, и он будет отправляться как картинка.</i>",
                    keyboard=main_keyboard()
                )

        elif text in ("📖 Инструкция",):
            send_instruction(chat_id)

        elif text == "/admin" and user_id == ADMIN_ID:
            users = db.get_all_users()
            connections = db.get_connections_count()
            trial = sum(1 for u in users if u["sub_type"] == "trial")
            paid = sum(1 for u in users if u["sub_type"] not in ("trial", "expired", "banned"))
            banned = sum(1 for u in users if u["sub_type"] == "banned")
            recent = db.get_recent_connections(5)
            recent_text = ""
            for r in recent:
                name = escape_html(r.get("first_name") or r.get("username") or str(r["owner_id"]), 100)
                uname = escape_html(f"@{r['username']}" if r.get("username") else f"id:{r['owner_id']}", 100)
                icon = "🟢" if r["is_enabled"] else "🔴"
                sub = r.get("sub_type", "?")
                raw_date = r.get("connected_at")
                date = raw_date.strftime("%d.%m.%Y %H:%M") if raw_date else ""
                recent_text += f"\n{icon} {name} ({uname}) · {sub} · {date}"
            send(chat_id,
                f"👑 <b>Админ панель</b>\n\n"
                f"👥 Пользователей: {len(users)}\n"
                f"🔗 Подключений: {connections}\n\n"
                f"🆓 Trial: {trial} | 💳 Платных: {paid} | 🚫 Бан: {banned}\n\n"
                f"🕐 <b>Последние подключения:</b>{recent_text or ' нет'}\n\n"
                f"<b>Команды:</b>\n\n"
                f"/sub @user 1|3|7|30|365 — добавить дни подписки\n"
                f"/ban user_id|@user — забанить пользователя\n"
                f"/unban user_id|@user — разбанить и дать 7 дней trial\n"
                f"/users [user_id|@user] — список пользователей и поиск\n"
                f"Кнопка в /users: 🎁 реферальная программа\n"
                f"/reply user_id текст — ответить в поддержку вручную\n"
                f"reply на сообщение пользователя — быстрый ответ в поддержку\n"
                f"/refund user_id|@user telegram_payment_charge_id — вернуть Stars\n"
                f"/unsub user_id|@user — отключить подписку без возврата\n"
                f"/cancelsub user_id|@user — то же самое\n"
                f"/closesupport user_id|@user — закрыть диалог поддержки\n"
                f"/supportlist — активные диалоги поддержки\n"
                f"/payments — статистика и история всех оплат\n"
                f"/payments user_id|@user — история оплат пользователя\n"
                f"/ads — рекламные ссылки и статистика\n"
                f"/bd [текст] — рассылка всем пользователям (alias /broadcast)\n"
                f"/bdsub [текст] — рассылка только активным подписчикам\n"
                f"/bdconn [текст] — рассылка только подключённым\n"
                f"/remind — приглашение неподключённым (с подтверждением)\n"
                f"/remind test — пример приглашения только себе\n"
                f"/admin",
                keyboard={"inline_keyboard": [
                    [{"text": "Статистика и история оплат", "callback_data": "payhist:all:1"}],
                    [{"text": "Реклама и источники", "callback_data": "ads:list:1"}],
                ]},
            )

        elif text.startswith("/sub ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 3:
                try:
                    duration = parts[2].lower()
                    duration_map = {
                        "1": ("daily", 1),
                        "3": ("manual_3d", 3),
                        "7": ("weekly", 7),
                        "30": ("monthly", 30),
                        "365": ("yearly", 365),
                        "daily": ("daily", 1),
                        "weekly": ("weekly", 7),
                        "monthly": ("monthly", 30),
                        "yearly": ("yearly", 365),
                    }
                    if duration not in duration_map:
                        send(chat_id, "❌ Формат: /sub user_id|@user 1|3|7|30|365")
                        return
                    sub_type, days = duration_map[duration]
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, f"❌ {escape_html(parts[1], 100)} не найден.")
                        return
                    db.set_subscription(target_id, sub_type, days)
                    notification = send(
                        target_id,
                        "✅ <b>Подписка активирована</b>\n\n"
                        f"Вам выдана подписка на <b>{days} дней</b>.",
                        keyboard=main_keyboard(),
                    )
                    delivery_note = ""
                    if not notification.get("ok"):
                        delivery_note = "\n⚠️ Не удалось уведомить пользователя: бот заблокирован или чат недоступен."
                    send(chat_id, f"✅ {sub_type} выдан {target_id} на {days} дней.{delivery_note}")
                except Exception as e:
                    logging.error("Admin /sub failed: %s", e)
                    send(chat_id, "❌ Не удалось выдать подписку. Подробности записаны в лог.")

        elif text.startswith("/ban ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 2:
                try:
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                        return
                    db.set_subscription(target_id, "banned", 0)
                    send(chat_id, f"🚫 {target_id} забанен.")
                    try:
                        send(target_id, "🚫 Ваш доступ заблокирован.")
                    except Exception:
                        pass
                except Exception as e:
                    logging.error("Admin /ban failed: %s", e)
                    send(chat_id, "❌ Не удалось заблокировать пользователя. Подробности записаны в лог.")

        elif text.startswith("/unban ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 2:
                try:
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                        return
                    db.set_subscription(target_id, "trial", 7)
                    send(chat_id, f"✅ {target_id} разбанен, trial 7 дней.")
                    try:
                        send(target_id, "✅ Ваш доступ восстановлен.")
                    except Exception:
                        pass
                except Exception as e:
                    logging.error("Admin /unban failed: %s", e)
                    send(chat_id, "❌ Не удалось разблокировать пользователя. Подробности записаны в лог.")

        elif user_id == ADMIN_ID and (
            text.startswith("/bd")
            or text.startswith("/broadcast")
        ):
            command = text.split(maxsplit=1)[0].lower()
            scope_map = {
                "/bd": "all",
                "/broadcast": "all",
                "/bdsub": "sub",
                "/broadcastsub": "sub",
                "/bdconn": "conn",
                "/broadcastconn": "conn",
            }
            scope = scope_map.get(command)
            if not scope:
                send(chat_id, "❌ Используй /bd, /bdsub или /bdconn.")
                return

            parts = text.split(maxsplit=1)
            broadcast_text = parts[1].strip() if len(parts) > 1 else ""
            source_message_id = None
            if not broadcast_text:
                reply = msg.get("reply_to_message")
                if reply:
                    source_message_id = reply.get("message_id")
                    source_chat_id = msg["chat"]["id"]
                else:
                    send(chat_id, f"❌ Формат: {command} текст или {command} в ответ на сообщение")
                    return
            else:
                source_chat_id = None

            targets = get_broadcast_targets(scope)
            if not targets:
                send(chat_id, "ℹ️ Нет получателей для рассылки.")
                return

            scope_label = {"all": "всем пользователям", "sub": "активным подписчикам", "conn": "подключённым"}[scope]
            send(chat_id, f"🔄 Рассылка запущена {scope_label}. Получателей: {len(targets)}")
            result = run_broadcast(
                scope=scope,
                source_chat_id=source_chat_id or msg["chat"]["id"],
                text=broadcast_text or None,
                source_message_id=source_message_id,
            )
            send(
                chat_id,
                f"✅ Рассылка завершена.\n"
                f"Получателей: {result['total']}\n"
                f"Отправлено: {result['sent']}\n"
                f"Ошибок: {result['failed']}"
            )
            return

        elif text.startswith("/users") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            query = parts[1].strip()[:32] if len(parts) > 1 else ""
            users = filter_users(query)
            text_out, keyboard = users_page_text_and_keyboard(users, page=1, query=query)
            send(chat_id, text_out, keyboard=keyboard)

    # ── Callback кнопки ────────────────────────────────────
    elif "callback_query" in update:
        cq = update["callback_query"]
        user_id = cq["from"]["id"]
        data = cq.get("data", "")
        if data.startswith(("remindsend:", "remindcancel:")):
            if user_id != ADMIN_ID or cq.get("message", {}).get("chat", {}).get("id") != ADMIN_ID:
                api("answerCallbackQuery", callback_query_id=cq["id"], text="Только для администратора в личном чате.", show_alert=True)
                return
            token = data.split(":", 1)[1]
            with _REMINDER_BROADCAST_LOCK:
                pending = _REMINDER_BROADCAST_PENDING.pop(token, None)
                if not pending or pending["expires"] < int(time.time()):
                    answer = "Подтверждение устарело. Введи /remind заново."
                elif data.startswith("remindcancel:"):
                    answer = "Рассылка отменена."
                elif _REMINDER_BROADCAST_RUNNING:
                    answer = "Рассылка уже выполняется."
                else:
                    _REMINDER_BROADCAST_RUNNING = True
                    try:
                        threading.Thread(target=run_manual_reminder_broadcast, args=(pending["targets"], ADMIN_ID),
                                         name="manual-reminders", daemon=True).start()
                    except Exception:
                        _REMINDER_BROADCAST_RUNNING = False
                        raise
                    answer = "Рассылка запущена. Итоги придут отдельным сообщением."
            api("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
            api("editMessageText", chat_id=ADMIN_ID, message_id=cq["message"]["message_id"], text=answer,
                reply_markup={"inline_keyboard": []})
            return
        if data == "reminder:stop":
            if cq.get("message", {}).get("chat", {}).get("id") != user_id:
                api("answerCallbackQuery", callback_query_id=cq["id"], text="Открой личный чат с ботом.", show_alert=True)
                return
            db.opt_out_connection_reminders(user_id)
            api("answerCallbackQuery", callback_query_id=cq["id"], text="Напоминания отключены.")
            api("editMessageReplyMarkup", chat_id=user_id, message_id=cq["message"]["message_id"],
                reply_markup={"inline_keyboard": [[{"text": "Как подключить", "callback_data": "reminder:connect"}]]})
            return
        if data.startswith(("payhist:", "ads:")) and user_id != ADMIN_ID:
            api("answerCallbackQuery", callback_query_id=cq["id"], text="Только для администратора.", show_alert=True)
            return
        if data.startswith(("payhist:", "ads:")) and cq.get("message", {}).get("chat", {}).get("id") != ADMIN_ID:
            api("answerCallbackQuery", callback_query_id=cq["id"], text="Открой статистику в личном чате с ботом.", show_alert=True)
            return
        if data == "check_required_channel":
            if not is_required_channel_member(user_id):
                api(
                    "answerCallbackQuery",
                    callback_query_id=cq["id"],
                    text="Сначала подпишись на канал, потом нажми проверку ещё раз.",
                    show_alert=True,
                )
                return
            api("answerCallbackQuery", callback_query_id=cq["id"])
            api(
                "editMessageText",
                chat_id=cq["message"]["chat"]["id"],
                message_id=cq["message"]["message_id"],
                text="✅ Подписка найдена. Открываю бота.",
                parse_mode="HTML",
            )
            unlock_start_after_channel(user_id, cq["message"]["chat"]["id"])
            return

        if user_id != ADMIN_ID and not is_required_channel_member(user_id):
            api(
                "answerCallbackQuery",
                callback_query_id=cq["id"],
                text="Сначала подпишись на канал.",
                show_alert=True,
            )
            send_subscription_gate(cq["message"]["chat"]["id"])
            return
        if user_id != ADMIN_ID:
            ensure_channel_trial(user_id, cq["message"]["chat"]["id"], notify=True)

        api("answerCallbackQuery", callback_query_id=cq["id"])

        if data == "reminder:connect":
            if cq.get("message", {}).get("chat", {}).get("id") == user_id:
                send_instruction(user_id)
        elif data.startswith("ads:recover:") and user_id == ADMIN_ID:
            match = re.fullmatch(r"ads:recover:([0-9]{1,10}):([0-9]{10}):([0-9]{10})", data)
            if not match:
                return
            campaign_id, start_ts, end_ts = map(int, match.groups())
            try:
                out, keyboard = ad_recovery_view(campaign_id, datetime.fromtimestamp(start_ts, MSK),
                                                datetime.fromtimestamp(end_ts, MSK), apply=True)
            except ValueError as exc:
                out, keyboard = str(exc), {"inline_keyboard": [[{"text": "Все размещения", "callback_data": "ads:list:1"}]]}
            api("editMessageText", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"],
                text=out, parse_mode="HTML", reply_markup=keyboard)
        elif data.startswith("ads:") and user_id == ADMIN_ID:
            parts = data.split(":")
            if len(parts) != 3 or parts[1] not in ("list", "view", "delete", "archive") or not parts[2].isdigit() or len(parts[2]) > 10:
                return
            value = int(parts[2])
            if parts[1] == "delete":
                out, keyboard = ad_delete_view(value)
            elif parts[1] == "archive":
                archived = db.archive_ad_campaign(value)
                out, keyboard = ads_list_view()
                notice = "Размещение убрано в архив." if archived else "Размещение уже в архиве или не найдено."
                out = notice + "\n\n" + out
            else:
                out, keyboard = ads_list_view(value) if parts[1] == "list" else ad_report_view(value)
            api("editMessageText", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"],
                text=out, parse_mode="HTML", reply_markup=keyboard)
        elif data.startswith("payhist:") and user_id == ADMIN_ID:
            parts = data.split(":")
            if len(parts) != 3 or not parts[2].isdigit() or not (parts[1] == "all" or parts[1].isdigit()):
                return
            target_id = None if parts[1] == "all" else int(parts[1])
            out, keyboard = payment_report_view(target_id, page=int(parts[2]))
            api("editMessageText", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"],
                text=out, parse_mode="HTML", reply_markup=keyboard)
        elif data in ("settings:deleted:0", "settings:deleted:1", "settings:edited:0", "settings:edited:1"):
            chat_id = cq["message"]["chat"]["id"]
            if chat_id != user_id:
                return
            _, setting, value = data.split(":")
            s = get_settings(user_id)
            field = "track_deleted" if setting == "deleted" else "track_edited"
            s[field] = value == "1"
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], s.get("support_mode", False), s.get("support_active", False))
            api("editMessageReplyMarkup", chat_id=chat_id, message_id=cq["message"]["message_id"], reply_markup=settings_keyboard(user_id))
        elif data.startswith("users:ref:") and user_id == ADMIN_ID:
            _, _, page_str, encoded_query = data.split(":", 3)
            page = int(page_str)
            query = urllib.parse.unquote(encoded_query) if encoded_query else ""
            text_out, keyboard = referrals_text(page=page, query=query)
            api(
                "editMessageText",
                chat_id=cq["message"]["chat"]["id"],
                message_id=cq["message"]["message_id"],
                text=text_out,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif data.startswith(("users:all:", "users:conn:")) and user_id == ADMIN_ID:
            _, mode, page_str, encoded_query = data.split(":", 3)
            page = int(page_str)
            query = urllib.parse.unquote(encoded_query) if encoded_query else ""
            users = filter_users(query)
            text_out, keyboard = users_page_text_and_keyboard(
                users,
                page=page,
                query=query,
                connected_only=mode == "conn",
            )
            api(
                "editMessageText",
                chat_id=cq["message"]["chat"]["id"],
                message_id=cq["message"]["message_id"],
                text=text_out,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif data.startswith("show_locked:"):
            token = data.split(":", 1)[1]
            if cq["message"]["chat"]["id"] != user_id:
                return
            pending = get_pending_locked_message(token, user_id)
            if not pending:
                send(user_id, "Это сообщение уже недоступно или принадлежит другому пользователю.")
                return

            db.request_locked_message(token, user_id, cq["message"]["message_id"])
            if not db.is_sub_active(user_id):
                send(
                    user_id,
                    expired_details_text(user_id, pending.get("event_type", "deleted"))
                    + "\n\nПосле оплаты запрошенное сообщение откроется автоматически. Срок хранения: 14 дней.",
                    keyboard=expired_payment_keyboard(user_id),
                )
                return

            if not deliver_locked_message(token, user_id, cq["message"]["message_id"]):
                send(user_id, "Не удалось подтвердить показ сообщения. Для медиа Telegram мог уже закрыть доступ к файлу.")
        elif data == "buy_platega_monthly":
            payment_url, error_message = create_platega_payment(user_id)
            if not payment_url:
                send(user_id, f"❌ {error_message}", keyboard=main_keyboard())
                return
            send(
                user_id,
                "💳 <b>Оплата 30 дней — 120 ₽</b>\n\n"
                "Открой страницу оплаты, отсканируй QR-код или оплати через СБП. "
                "После подтверждения подписка активируется автоматически.",
                keyboard={"inline_keyboard": [[{"text": "Оплатить 120 ₽", "url": payment_url, "style": "success"}]]},
            )
        elif data.startswith("buy_") and data.replace("buy_", "", 1) in PAYMENT_PLANS:
            payload = data.replace("buy_", "", 1)
            plan = PAYMENT_PLANS[payload]
            send_invoice(user_id, plan["title"], f"Dialog Spy Bot — {plan['title'].lower()} доступа", payload, plan["stars"])

    # ── Pre-checkout ───────────────────────────────────────
    elif "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        payload = pcq.get("invoice_payload", "")
        plan = PAYMENT_PLANS.get(payload)
        amount = pcq.get("total_amount")
        currency = pcq.get("currency")
        if not plan:
            logging.error("Invalid pre_checkout payload: %s", payload)
            api(
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=False,
                error_message="Неизвестный тариф. Попробуй создать счёт заново.",
            )
            return
        if amount != plan["stars"] or currency != "XTR":
            logging.error("Invalid pre_checkout amount for %s: got=%s expected=%s", payload, amount, plan["stars"])
            api(
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=False,
                error_message="Сумма счёта изменилась. Попробуй создать счёт заново.",
            )
            return
        api("answerPreCheckoutQuery", pre_checkout_query_id=pcq["id"], ok=True)

    # ── Подключение бизнес-аккаунта ───────────────────────
    elif "business_connection" in update:
        bc = update["business_connection"]
        owner_id = bc["user_chat_id"]
        is_enabled = bc.get("is_enabled", False)
        owner = bc.get("user", {}) or {}
        db.save_user(owner_id, owner.get("username", ""), owner.get("first_name", ""))
        db.save_connection(bc["id"], owner_id, is_enabled)
        if is_enabled:
            db.resume_subscription(owner_id)
            referrer_id = db.reward_referral_for_connection(owner_id)
            if referrer_id:
                send(
                    referrer_id,
                    "🎉 <b>Друг подключил бота по вашей ссылке!</b>\n"
                    "+3 дня добавлено к подписке.",
                )
            send(owner_id, "✅ <b>Бот подключён!</b>\n\nБуду присылать уведомления об удалённых и изменённых сообщениях.", keyboard=main_keyboard())
        else:
            if db.get_connections_count_for_user(owner_id) == 0:
                db.pause_subscription(owner_id)
            send(owner_id, "❌ Бот отключён.\n\nПричина: нет подключённых чатов.", keyboard=main_keyboard())

    # ── Новое сообщение из бизнес-чата ────────────────────
    elif "business_message" in update:
        msg = update["business_message"]
        update_received_at = time.time()
        conn_id = msg.get("business_connection_id", "")
        sender = msg.get("from", {})
        owner_id = get_business_owner(conn_id)
        if not owner_id:
            return

        reply_to_message = msg.get("reply_to_message")
        is_owner_message = sender.get("id") == owner_id
        if (
            is_owner_message
            and is_reply_media_save_candidate(reply_to_message)
            and message_sender_id(reply_to_message) != owner_id
        ):
            logging.info(
                "Business reply media update age=%.2fs file_type=%s",
                max(0.0, update_received_at - msg["date"]),
                get_support_media(reply_to_message)[0],
            )
            replied_message_id = reply_to_message.get("message_id")
            media_type, media_file_id = get_support_media(reply_to_message)
            if not db.is_sub_active(owner_id):
                send_expired_message(
                    owner_id,
                    "reply_media",
                    get_chat_link(msg["chat"]),
                    locked=True,
                    reply_to_message=reply_to_message,
                )
                return
            key = (owner_id, conn_id, msg["chat"]["id"], replied_message_id, media_file_id)
            if (
                replied_message_id
                and media_file_id
                and mark_business_reply_media_sent(key)
            ):
                try:
                    future = submit_media_task(
                        _MEDIA_EXECUTOR, _MEDIA_JOB_SLOTS, deliver_business_reply_media,
                        owner_id, msg, key, time.monotonic(),
                    )
                except Exception:
                    forget_business_reply_media(key)
                    raise
                if future is None:
                    forget_business_reply_media(key)
                    send(owner_id, "Сейчас много запросов на сохранение. Ответьте на сообщение ещё раз чуть позже.")

        if sender.get("id") == owner_id or msg["chat"]["id"] == owner_id:
            return

        incoming_media_type, incoming_file_id = get_support_media(msg)
        if incoming_media_type in REPLY_MEDIA_SAVE_TYPES and incoming_file_id:
            logging.info(
                "Incoming media file_type=%s save_candidate=%s age=%.2fs",
                incoming_media_type, is_reply_media_save_candidate(msg),
                max(0.0, update_received_at - msg["date"]),
            )
            if db.is_sub_active(owner_id):
                prefetch_reply_media(msg)

        date_str = format_ts_msk(msg["date"])
        sender_link = get_user_link(sender or msg.get("chat", {}))
        text_content = msg.get("text")
        caption_content = msg.get("caption")
        if text_content is not None or caption_content is not None:
            db.cache_message(
                conn_id,
                msg["chat"]["id"],
                msg["message_id"],
                sender_link,
                text_content if text_content is not None else caption_content,
                date_str,
            )

        if msg.get("voice"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "voice", msg["voice"]["file_id"], date_str)
        elif msg.get("video_note"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "video_note", msg["video_note"]["file_id"], date_str)
        elif msg.get("audio"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "audio", msg["audio"]["file_id"], date_str)
        elif msg.get("photo"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "photo", msg["photo"][-1]["file_id"], date_str)
        elif msg.get("video"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "video", msg["video"]["file_id"], date_str)
        elif msg.get("animation"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "animation", msg["animation"]["file_id"], date_str)
        elif msg.get("document"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "document", msg["document"]["file_id"], date_str)
        elif msg.get("sticker"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "sticker", msg["sticker"]["file_id"], date_str)

    # ── Изменённое сообщение ───────────────────────────────
    elif "edited_business_message" in update:
        msg = update["edited_business_message"]
        conn_id = msg.get("business_connection_id", "")
        new_text = msg.get("text") if msg.get("text") is not None else msg.get("caption", "")
        owner_id = get_business_owner(conn_id)
        if not owner_id:
            return

        s = get_settings(owner_id)
        if not s["track_edited"]:
            return
        if not allow_business_event(owner_id, conn_id):
            return

        original = db.get_cached_message(conn_id, msg["chat"]["id"], msg["message_id"])
        if original and original["text"] != new_text:
            edit_text = (
                f"✏️ <b>Сообщение изменено</b>\n"
                f"👤 {get_chat_link(msg['chat'])}\n"
                f"🕐 {original['date']}\n\n"
                f"<b>Было:</b>\n{escape_html(original['text'], 1500)}\n\n"
                f"<b>Стало:</b>\n{escape_html(new_text, 1500)}"
            )
            if not db.is_sub_active(owner_id):
                result = send_expired_message(owner_id, "edited", get_chat_link(msg["chat"]), locked=True, saved_text=edit_text)
                if result.get("ok"):
                    db.update_cached_text(conn_id, msg["chat"]["id"], msg["message_id"], new_text)
                return

            result = send(owner_id, edit_text)
            if result.get("ok"):
                db.update_cached_text(conn_id, msg["chat"]["id"], msg["message_id"], new_text)

    # ── Удалённые сообщения ────────────────────────────────
    elif "deleted_business_messages" in update:
        event = update["deleted_business_messages"]
        conn_id = event.get("business_connection_id", "")
        owner_id = get_business_owner(conn_id)
        if not owner_id:
            return

        s = get_settings(owner_id)
        if not s["track_deleted"]:
            return
        if not allow_business_event(owner_id, conn_id):
            return

        chat_link = get_chat_link(event["chat"])
        chat_id = event["chat"]["id"]
        deleted_items = []
        for msg_id in event["message_ids"]:
            original = db.get_cached_message(conn_id, chat_id, msg_id)
            if original:
                deleted_items.append(("message", msg_id, original))

            media = db.get_cached_media(conn_id, chat_id, msg_id)
            if media:
                deleted_items.append(("media", msg_id, media))

        # Outgoing messages from the owner are not cached, so ignore their deletion.
        if not deleted_items:
            return

        if not db.is_sub_active(owner_id):
            for item_type, msg_id, cached_item in deleted_items:
                if item_type == "message":
                    saved_text = (
                        f"🗑️ <b>В чате с {chat_link} удалено сообщение</b>\n"
                        f"🕐 {cached_item['date']}\n\n"
                        f"<b>Текст:</b>\n{escape_html(cached_item['text'], 3400)}"
                    )
                    result = send_expired_message(owner_id, "deleted", chat_link, locked=True, saved_text=saved_text)
                    if result.get("ok"):
                        db.delete_cached_message(conn_id, chat_id, msg_id)
                else:
                    media_type = cached_item["file_type"]
                    media = {"file_id": cached_item["file_id"]}
                    reply = {media_type: [media] if media_type == "photo" else media}
                    result = send_expired_message(owner_id, "deleted", chat_link, locked=True, reply_to_message=reply)
                    if result.get("ok"):
                        db.delete_cached_media(conn_id, chat_id, msg_id)
            return

        for item_type, msg_id, cached_item in deleted_items:
            if item_type == "message":
                result = send(owner_id,
                    f"🗑️ <b>В чате с {chat_link} удалено сообщение</b>\n"
                    f"🕐 {cached_item['date']}\n\n"
                    f"<b>Текст:</b>\n{escape_html(cached_item['text'], 3400)}"
                )
                if result.get("ok"):
                    db.delete_cached_message(conn_id, chat_id, msg_id)
                continue

            caption = (
                f"🗑️ <b>В чате с {chat_link} удалено медиа</b> · "
                f"{cached_item['file_type']}\n🕐 {cached_item['date']}"
            )
            result = send_file(
                owner_id,
                cached_item["file_id"],
                cached_item["file_type"],
                caption,
                fallback_to_upload=True,
            )
            if result.get("ok"):
                db.delete_cached_media(conn_id, chat_id, msg_id)


def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")

    db.init_db()
    db.cleanup_temp_tables()
    start_health_server()
    start_cleanup_worker()

    api("deleteWebhook", drop_pending_updates=False)

    me = api("getMe")
    BOT_USERNAME = me.get("result", {}).get("username", "DialogDelBot")
    setup_command_menu()
    start_connection_reminder_worker()

    print("=" * 40)
    print(f"Бот @{BOT_USERNAME} запущен!")
    print("=" * 40)

    offset = 0
    while True:
        try:
            result = api("getUpdates", offset=offset, timeout=50, allowed_updates=ALLOWED_UPDATES)
            if not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    logging.error(f"Ошибка: {e}")

        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
