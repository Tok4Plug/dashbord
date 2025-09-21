# ================================
# app.py (monitor full + PROBE ativo + webhook-info + auxiliares + alertas + métricas)
# ================================
import os, sys, threading, time, logging, urllib.parse
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, jsonify
from flask_migrate import Migrate
from sqlalchemy import text

from models import db, Bot

# ---------- Logger ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot-monitor")

# ---------- App / Config ----------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "change_me_random")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL não definido. Configure no Railway/ENV.")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
migrate = Migrate(app, db)

# ---------- Parâmetros ----------
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
FAIL_THRESHOLD   = int(os.getenv("FAIL_THRESHOLD", "3"))
CHECK_TIMEOUT    = float(os.getenv("CHECK_TIMEOUT", "7.0"))
MAX_LOGS         = int(os.getenv("MAX_LOGS", "500"))
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

# Estratégia de check (recomendado: alive_plus)
CHECK_STRATEGY = os.getenv("CHECK_STRATEGY", "alive_plus").strip().lower()

# Webhook thresholds
REQUIRE_WEBHOOK_MATCH     = os.getenv("REQUIRE_WEBHOOK_MATCH", "1") == "1"
WEBHOOK_ERROR_MAX_AGE_SEC = int(os.getenv("WEBHOOK_ERROR_MAX_AGE_SEC", "1800"))
WEBHOOK_PENDING_MAX       = int(os.getenv("WEBHOOK_PENDING_MAX", "50"))

# Endpoint health
URL_EXPECT_2XX  = os.getenv("URL_EXPECT_2XX", "1") == "1"
URL_HEALTH_PATH = os.getenv("URL_HEALTH_PATH", "").strip()

# PROBE ativo (chat de monitoramento)
ACTIVE_PROBE_ENABLED = os.getenv("ACTIVE_PROBE_ENABLED", "1") == "1"
ACTIVE_PROBE_DELETE  = os.getenv("ACTIVE_PROBE_DELETE", "1") == "1"
MONITOR_CHAT_ID      = os.getenv("MONITOR_CHAT_ID", "").strip()  # ex: -1001234567890 (grupo)

# Alertas
ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

# Unidade única (evita múltiplos monitores)
USE_DB_LOCK_FOR_MONITOR = os.getenv("USE_DB_LOCK_FOR_MONITOR", "1") == "1"
DB_MONITOR_LOCK_KEY     = int(os.getenv("DB_MONITOR_LOCK_KEY", "72491371"))

# Evita subir monitor durante migrations/CLI
if os.getenv("FLASK_RUN_FROM_CLI") == "true" or "flask" in (sys.argv[0] or "").lower():
    START_MONITOR = "0"

# ---------- Logs / Métricas ----------
monitor_logs: List[str] = []
def add_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    monitor_logs.append(line)
    if len(monitor_logs) > MAX_LOGS:
        monitor_logs.pop(0)
    logger.info(msg)

metrics = {
    "checks_total": 0, "failures_total": 0,
    "switches_total": 0, "switch_errors_total": 0,
    "last_check_ts": None, "bots_active": 0, "bots_reserve": 0,
    "monitor_running": False, "monitor_has_lock": False
}

# ---------- Sessão requests ----------
def make_requests_session():
    session = requests.Session()
    retry = Retry(
        total=2, backoff_factor=0.5,
        status_forcelist=(500,502,503,504),
        allowed_methods=False, raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
requests_session = make_requests_session()

# ---------- Notificações (WhatsApp / Twilio) ----------
def _get_admin_whatsapps() -> List[str]:
    v = os.getenv("ADMIN_WHATSAPP", "")
    return [p.strip() for p in v.split(",") if p.strip()]

def send_whatsapp_message_text(to_number: Optional[str], text_msg: str) -> bool:
    TWILIO_SID  = os.getenv("TWILIO_SID")
    TWILIO_AUTH = os.getenv("TWILIO_AUTH")
    TWILIO_FROM = os.getenv("TWILIO_FROM")
    recipients = [to_number] if to_number else _get_admin_whatsapps()
    if TWILIO_SID and TWILIO_AUTH and TWILIO_FROM and recipients:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_AUTH)
            for r in recipients:
                client.messages.create(body=text_msg, from_=f"whatsapp:{TWILIO_FROM}", to=f"whatsapp:{r}")
            add_log("Mensagem WhatsApp enviada via Twilio.")
            return True
        except Exception as e:
            add_log(f"Erro Twilio: {e}")
    return False

# ---------- Alertas ----------
alerts_lock = threading.Lock()
down_alert_last_at = defaultdict(lambda: None)

def _should_alert_now(bot_id: int) -> bool:
    if not ALERT_ON_FIRST_FAIL:
        return False
    now = datetime.utcnow()
    with alerts_lock:
        last = down_alert_last_at.get(bot_id)
        if last is None or (now - last).total_seconds() >= ALERT_COOLDOWN_MIN*60:
            down_alert_last_at[bot_id] = now
            return True
    return False

def _clear_alert_state(bot_id: int):
    with alerts_lock:
        down_alert_last_at.pop(bot_id, None)

def notify_bot_down(bot: Bot, failures: int, reason: str):
    msg = (
        f"⚠️ Bot em falha\n"
        f"• Nome: {bot.name}\n"
        f"• Falhas consecutivas: {failures}/{FAIL_THRESHOLD}\n"
        f"• Motivo: {reason}\n"
        f"🕒 {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )
    send_whatsapp_message_text(None, msg)

def notify_swap_summary(failed: Bot, replacement: Bot):
    actives = db.session.query(Bot).filter_by(status="ativo").count()
    reserves = db.session.query(Bot).filter_by(status="reserva").count()
    msg = (
        "🔁 Substituição executada\n"
        f"❌ Caiu: {failed.name}\n"
        f"✅ Entrou: {replacement.name}\n\n"
        f"📊 Ativos: {actives} | Reserva: {reserves}"
    )
    if ALERT_SUMMARY_ON_SWAP:
        send_whatsapp_message_text(None, msg)

# ---------- Helpers ----------
def _utc_ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None

def _compose_url_for_health(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if URL_HEALTH_PATH:
        from urllib.parse import urljoin
        return urljoin(url.rstrip("/") + "/", URL_HEALTH_PATH.lstrip("/"))
    return url

# ---------- Checks: token / webhook / url ----------
def safe_check_token(token: Optional[str]) -> Tuple[bool, str, Optional[int]]:
    if not token:
        return False, "sem token", None
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = requests_session.get(url, timeout=CHECK_TIMEOUT)
        st = r.status_code
        ok = False
        desc = ""
        try:
            js = r.json()
            ok = bool(js.get("ok"))
            desc = js.get("description", "")
        except Exception:
            pass
        if st == 200 and ok:
            return True, "token OK", st
        return False, f"token FAIL ({desc or 'inválido'})", st
    except Exception as e:
        return False, f"token erro {e.__class__.__name__}", None

def safe_check_webhook(token: Optional[str], expected_url: Optional[str]) -> Tuple[bool, str, Dict[str, Any]]:
    diag: Dict[str, Any] = {}
    if not token:
        return False, "webhook FAIL (sem token)", diag
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=CHECK_TIMEOUT)
        js = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        info = js.get("result", {}) if js.get("ok") else {}
        webhook_url = info.get("url") or ""
        last_err_msg = info.get("last_error_message") or ""
        last_err_date = info.get("last_error_date")
        pending = int(info.get("pending_update_count") or 0)
        diag.update({
            "webhook_url": webhook_url,
            "last_error_message": last_err_msg,
            "last_error_date": last_err_date,
            "pending_update_count": pending
        })
        if REQUIRE_WEBHOOK_MATCH and expected_url and webhook_url != expected_url:
            return False, f"webhook FAIL (url difere: got={webhook_url})", diag
        if last_err_date:
            last_err_dt = _utc_ts_to_dt(int(last_err_date))
            if last_err_dt and (datetime.utcnow() - last_err_dt).total_seconds() <= WEBHOOK_ERROR_MAX_AGE_SEC:
                return False, f"webhook FAIL (erro recente: {last_err_msg})", diag
        if pending > WEBHOOK_PENDING_MAX:
            return False, f"webhook FAIL (pending={pending})", diag
        return True, "webhook OK", diag
    except Exception as e:
        return False, f"webhook erro {e.__class__.__name__}", diag

def safe_check_link(url: Optional[str]) -> Tuple[bool, str, Optional[int]]:
    if not url:
        return False, "sem URL", None
    target = _compose_url_for_health(url)
    try:
        r = requests_session.get(target, timeout=CHECK_TIMEOUT, allow_redirects=True)
        st = r.status_code
        if URL_EXPECT_2XX:
            return (200 <= st < 300), ("url OK" if 200 <= st < 300 else f"url HTTP {st}"), st
        else:
            return (200 <= st < 400), ("url OK" if 200 <= st < 400 else f"url HTTP {st}"), st
    except Exception as e:
        return False, f"url erro {e.__class__.__name__}", None

# ---------- PROBE ativo (teste real no chat) ----------
def safe_check_alive(token: Optional[str], chat_id: Optional[str]) -> Tuple[bool, str, Optional[int], Optional[int]]:
    """
    Retorna: (ok, reason, status_code, message_id)
    - ok True apenas se o bot conseguiu postar no chat monitorado.
    """
    if not token:
        return False, "alive FAIL (sem token)", None, None
    if not ACTIVE_PROBE_ENABLED:
        return True, "alive SKIP (desativado)", None, None
    if not chat_id:
        return False, "alive FAIL (sem MONITOR_CHAT_ID)", None, None

    try:
        # 1) enviar ping
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"probe:{int(time.time())}",
            "disable_notification": True,
            "allow_sending_without_reply": True
        }
        r = requests_session.post(url, data=payload, timeout=CHECK_TIMEOUT)
        st = r.status_code
        js = {}
        try:
            js = r.json()
        except Exception:
            pass

        if st == 200 and js.get("ok") and js.get("result", {}).get("message_id"):
            msg_id = js["result"]["message_id"]

            # 2) limpa a mensagem se configurado
            if ACTIVE_PROBE_DELETE:
                try:
                    del_url = f"https://api.telegram.org/bot{token}/deleteMessage"
                    requests_session.post(del_url, data={"chat_id": chat_id, "message_id": msg_id}, timeout=CHECK_TIMEOUT)
                except Exception:
                    pass

            return True, "alive OK", st, msg_id

        # mensagens de erro úteis do Telegram
        err_desc = js.get("description", "")
        return False, f"alive FAIL ({err_desc or st})", st, None

    except Exception as e:
        return False, f"alive erro {e.__class__.__name__}", None, None

# ---------- Decisão ----------
def decide_health(token_ok: bool, webhook_ok: bool, url_ok: bool, alive_ok: bool) -> Tuple[bool, str]:
    pol = CHECK_STRATEGY
    if   pol == "token_only":        ok = token_ok
    elif pol == "url_only":          ok = url_ok
    elif pol == "webhook_only":      ok = webhook_ok
    elif pol == "token_and_url":     ok = token_ok and url_ok
    elif pol == "token_or_url":      ok = token_ok or url_ok
    elif pol == "token_webhook_url": ok = token_ok and webhook_ok and url_ok
    elif pol == "alive_only":        ok = alive_ok
    else:  # alive_plus (default): alive && webhook && url
        ok = alive_ok and webhook_ok and url_ok

    reason = (f"policy={pol} | token={'OK' if token_ok else 'FAIL'} "
              f"| webhook={'OK' if webhook_ok else 'FAIL'} "
              f"| url={'OK' if url_ok else 'FAIL'} "
              f"| alive={'OK' if alive_ok else 'FAIL'}")
    return ok, reason

# ---------- Swap ----------
bot_locks: Dict[int, threading.Lock] = {}
def swap_bot(failed_bot_id: int):
    lock = bot_locks.setdefault(failed_bot_id, threading.Lock())
    if not lock.acquire(blocking=False):
        add_log(f"Swap já em andamento para bot_id={failed_bot_id}")
        return
    try:
        with app.app_context():
            session = db.session
            with session.begin():
                fb = session.get(Bot, failed_bot_id)
                if not fb:
                    return
                fb.status, fb.failures = "reserva", 0
                fb.last_reason = "movido para reserva por falhas"
                replacement = (
                    session.query(Bot)
                    .filter(Bot.status=="reserva", Bot.id!=fb.id)
                    .order_by(Bot.updated_at.asc(), Bot.id.asc())
                    .first()
                )
                if not replacement:
                    add_log(f"❌ {fb.name} caiu e não há reservas!")
                    send_whatsapp_message_text(None, f"❌ {fb.name} caiu e não há reservas!")
                    metrics["switch_errors_total"] += 1
                    return
                replacement.status, replacement.failures, replacement.last_ok = "ativo", 0, datetime.utcnow()
                replacement.last_reason = "ativado como substituto"

            metrics["switches_total"] += 1
            add_log(f"🔁 Swap: {fb.name} ❌ → {replacement.name} ✅")
            _clear_alert_state(fb.id)
            notify_swap_summary(fb, replacement)
    finally:
        lock.release()

# ---------- Monitor ----------
def _try_acquire_monitor_lock() -> bool:
    if not USE_DB_LOCK_FOR_MONITOR:
        metrics["monitor_has_lock"] = True
        return True
    with app.app_context():
        try:
            with db.engine.begin() as conn:
                row = conn.execute(text("SELECT pg_try_advisory_lock(:k) AS locked"), {"k": DB_MONITOR_LOCK_KEY}).fetchone()
                locked = bool(row and row[0])
                metrics["monitor_has_lock"] = locked
                return locked
        except Exception as e:
            add_log(f"⚠️ Falha ao adquirir DB advisory lock: {e}")
            metrics["monitor_has_lock"] = False
            return False

def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b:
            return

        # 1) token
        token_ok, token_reason, token_http = safe_check_token(b.token)
        # 2) webhook
        webhook_ok, webhook_reason, webhook_diag = safe_check_webhook(b.token, b.redirect_url)
        # 3) url
        url_ok, url_reason, url_http = safe_check_link(b.redirect_url)
        # 4) alive (probe ativo no chat)
        alive_ok, alive_reason, alive_http, alive_msg_id = safe_check_alive(b.token, MONITOR_CHAT_ID)

        ok, policy_reason = decide_health(token_ok, webhook_ok, url_ok, alive_ok)
        full_reason = f"{policy_reason} | {token_reason} | {webhook_reason} | {url_reason} | {alive_reason}"

        metrics["checks_total"] += 1
        add_log(
            f"[{b.id}:{b.name}] "
            f"TOKEN:{'✅' if token_ok else '❌'}({token_http}) | "
            f"WEBHOOK:{'✅' if webhook_ok else '❌'}(url={webhook_diag.get('webhook_url','')}, pend={webhook_diag.get('pending_update_count')}, err={webhook_diag.get('last_error_message')}) | "
            f"URL:{'✅' if url_ok else '❌'}({url_http}) | "
            f"ALIVE:{'✅' if alive_ok else '❌'}({alive_http}) ⇒ "
            f"{'✅ OK' if ok else f'❌ FAIL ({(b.failures or 0)+1}/{FAIL_THRESHOLD})'}"
        )

        # Persistência + colunas auxiliares
        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            bot.last_reason = full_reason
            # auxiliares
            if hasattr(bot, "last_token_ok"):   bot.last_token_ok = token_ok
            if hasattr(bot, "last_webhook_ok"): bot.last_webhook_ok = webhook_ok
            if hasattr(bot, "last_url_ok"):     bot.last_url_ok = url_ok
            if hasattr(bot, "last_alive_ok"):   bot.last_alive_ok = alive_ok

            if hasattr(bot, "last_token_http"): bot.last_token_http = token_http
            if hasattr(bot, "last_url_http"):   bot.last_url_http = url_http
            if hasattr(bot, "last_alive_http"): bot.last_alive_http = alive_http

            if hasattr(bot, "last_webhook_url"):   bot.last_webhook_url = webhook_diag.get("webhook_url")
            if hasattr(bot, "last_webhook_error"): bot.last_webhook_error = webhook_diag.get("last_error_message")
            if hasattr(bot, "last_webhook_error_at"):
                dt = _utc_ts_to_dt(webhook_diag.get("last_error_date"))
                bot.last_webhook_error_at = dt
            if hasattr(bot, "pending_update_count"): bot.pending_update_count = webhook_diag.get("pending_update_count")
            if hasattr(bot, "last_probe_msg_id"):    bot.last_probe_msg_id = alive_msg_id
            if hasattr(bot, "last_probe_at"):        bot.last_probe_at = datetime.utcnow()

            if ok:
                bot.failures = 0
                bot.last_ok = datetime.utcnow()
                _clear_alert_state(bot.id)
            else:
                bot.failures = (bot.failures or 0) + 1
                metrics["failures_total"] += 1
                if bot.failures == 1 and _should_alert_now(bot.id):
                    notify_bot_down(b, bot.failures, full_reason)
                if bot.failures >= FAIL_THRESHOLD:
                    add_log(f"{bot.name} atingiu {bot.failures} falhas; agendando swap…")
                    threading.Thread(target=swap_bot, args=(bot.id,), daemon=True).start()

def monitor_loop():
    # Não roda probe sem MONITOR_CHAT_ID quando alive está habilitado
    if ACTIVE_PROBE_ENABLED and not MONITOR_CHAT_ID:
        add_log("⛔ ACTIVE_PROBE_ENABLED=1 mas MONITOR_CHAT_ID não definido. Desabilite o probe ou defina o chat.")
        return

    if not _try_acquire_monitor_lock():
        add_log("⛔ Outro processo já está rodando o monitor (lock não adquirido).")
        return

    add_log("🚀 Monitor iniciado.")
    metrics["monitor_running"] = True

    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)
        try:
            with app.app_context():
                bots = db.session.query(Bot).order_by(Bot.updated_at.asc(), Bot.id.asc()).all()
                metrics["bots_active"]  = sum(1 for x in bots if x.status=="ativo")
                metrics["bots_reserve"] = sum(1 for x in bots if x.status=="reserva")

                from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    futs = [ex.submit(check_and_maybe_swap, b.id) for b in bots]
                    wait(futs, return_when=ALL_COMPLETED)
        except Exception as e:
            add_log(f"Erro monitor: {e}")

        time.sleep(max(0, MONITOR_INTERVAL - (time.time()-start_ts)))

def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True, name="bot-monitor")
    t.start()
    add_log("Thread monitor disparada.")

# ---------- Rotas ----------
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    bots = Bot.query.order_by(Bot.id).all()
    return jsonify({
        "bots": [b.to_dict() for b in bots],
        "logs": monitor_logs,
        "metrics": metrics,
        "last_action": metrics.get("last_check_ts"),
        "last_action_human": datetime.utcfromtimestamp(metrics["last_check_ts"]).strftime("%Y-%m-%d %H:%M:%S") if metrics["last_check_ts"] else None
    })

# ---------- Bootstrap (patch de colunas auxiliares) ----------
PATCH_SQL = """
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT NULL;

-- auxiliares de checks
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_ok BOOLEAN;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_ok BOOLEAN;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_ok BOOLEAN;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_alive_ok BOOLEAN;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_http INTEGER;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_http INTEGER;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_alive_http INTEGER;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_url TEXT;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error TEXT;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error_at TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS pending_update_count INTEGER;

-- probe info
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_probe_msg_id BIGINT;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_probe_at TIMESTAMP NULL;

-- metadados
ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();
"""
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(text(PATCH_SQL))
        add_log("✅ Auto-patch de schema aplicado (auxiliares + probe).")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1' ou execução via CLI).")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")