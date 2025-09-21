# ================================
# app.py (monitor full + webhook-info + auxiliares + alertas + métricas)
# ================================
import os, sys, threading, time, logging, urllib.parse, json
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, request, jsonify
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
MAX_LOGS         = int(os.getenv("MAX_LOGS", "400"))
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

# Política: exige tudo OK por padrão
CHECK_STRATEGY = os.getenv("CHECK_STRATEGY", "token_webhook_url").strip().lower()
# opções aceitas:
#   token_only | url_only | webhook_only
#   token_and_url | token_or_url
#   token_webhook_url  (default)  -> token && webhook && url

# Webhook-health thresholds
REQUIRE_WEBHOOK_MATCH      = os.getenv("REQUIRE_WEBHOOK_MATCH", "1") == "1"
WEBHOOK_ERROR_MAX_AGE_SEC  = int(os.getenv("WEBHOOK_ERROR_MAX_AGE_SEC", "1800"))  # 30min
WEBHOOK_PENDING_MAX        = int(os.getenv("WEBHOOK_PENDING_MAX", "50"))

# Endpoint health
URL_EXPECT_2XX             = os.getenv("URL_EXPECT_2XX", "1") == "1"
URL_HEALTH_PATH            = os.getenv("URL_HEALTH_PATH", "").strip()  # ex: /health

# Alertas
ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

# Unidade única do monitor (evita rodar em todos os workers)
USE_DB_LOCK_FOR_MONITOR = os.getenv("USE_DB_LOCK_FOR_MONITOR", "1") == "1"
DB_MONITOR_LOCK_KEY     = int(os.getenv("DB_MONITOR_LOCK_KEY", "72491371"))

# Evita subir monitor durante migrations / CLI
if os.getenv("FLASK_RUN_FROM_CLI") == "true" or "flask" in (sys.argv[0] if sys.argv else "").lower():
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
        total=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=False,
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
requests_session = make_requests_session()

# ---------- Notificações ----------
def _get_admin_whatsapps() -> List[str]:
    v = os.getenv("ADMIN_WHATSAPP", "")
    return [p.strip() for p in v.split(",") if p.strip()]

def send_whatsapp_message_text(to_number: Optional[str], text_msg: str) -> bool:
    TWILIO_SID  = os.getenv("TWILIO_SID")
    TWILIO_AUTH = os.getenv("TWILIO_AUTH")
    TWILIO_FROM = os.getenv("TWILIO_FROM")
    CALLMEBOT_KEY = os.getenv("CALLMEBOT_KEY")
    recipients = [to_number] if to_number else _get_admin_whatsapps()

    if TWILIO_SID and TWILIO_AUTH and TWILIO_FROM and recipients:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_AUTH)
            for r in recipients:
                client.messages.create(body=text_msg,
                                       from_=f"whatsapp:{TWILIO_FROM}",
                                       to=f"whatsapp:{r}")
            add_log("Mensagem WhatsApp enviada via Twilio.")
            return True
        except Exception as e:
            add_log(f"Erro Twilio: {e}")

    if CALLMEBOT_KEY and recipients:
        try:
            for r in recipients:
                url = (f"https://api.callmebot.com/whatsapp.php?"
                       f"phone={urllib.parse.quote_plus(r)}"
                       f"&text={urllib.parse.quote_plus(text_msg)}"
                       f"&apikey={CALLMEBOT_KEY}")
                requests_session.get(url, timeout=10)
            add_log("Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"Erro CallMeBot: {e}")

    return False

# ---------- Alertas ----------
alerts_lock = threading.Lock()
down_alert_last_at = defaultdict(lambda: None)

def _should_alert_now(bot_id: int) -> bool:
    if not ALERT_ON_FIRST_FAIL: return False
    now = datetime.utcnow()
    with alerts_lock:
        last = down_alert_last_at.get(bot_id)
        if last is None or (now - last).total_seconds() >= ALERT_COOLDOWN_MIN * 60:
            down_alert_last_at[bot_id] = now
            return True
    return False

def _clear_alert_state(bot_id: int):
    with alerts_lock:
        if bot_id in down_alert_last_at:
            del down_alert_last_at[bot_id]

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

# ---------- Health helpers ----------
Diag = Dict[str, Any]
diag_state: Dict[int, Diag] = {}  # cache in-memory para dashboard

def _utc_ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    if not ts: return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return None

def safe_check_token(token: Optional[str]) -> Tuple[bool, str, Optional[int]]:
    if not token:
        return False, "sem token", None
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = requests_session.get(url, timeout=CHECK_TIMEOUT)
        status = r.status_code
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if status == 200 and data.get("ok") is True:
            return True, "token OK", status
        desc = data.get("description", "inválido")
        return False, f"token FAIL ({desc})", status
    except Exception as e:
        return False, f"token erro {e.__class__.__name__}", None

def safe_check_webhook(token: Optional[str], expected_url: Optional[str]) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Avalia /getWebhookInfo:
      - URL configurada deve bater com expected_url (se REQUIRE_WEBHOOK_MATCH=1)
      - last_error_date recente => FAIL
      - pending_update_count > limite => FAIL
    """
    diag: Dict[str, Any] = {}
    if not token:
        return False, "webhook FAIL (sem token)", diag
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    try:
        r = requests_session.get(url, timeout=CHECK_TIMEOUT)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        info = data.get("result", {}) if data.get("ok") else {}
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

        # match da URL
        if REQUIRE_WEBHOOK_MATCH and expected_url and webhook_url != expected_url:
            return False, f"webhook FAIL (url difere: exp={expected_url} got={webhook_url})", diag

        # erro recente
        if last_err_date:
            last_err_dt = _utc_ts_to_dt(int(last_err_date))
            diag["last_error_dt"] = last_err_dt.isoformat() if last_err_dt else None
            if last_err_dt:
                age = (datetime.utcnow() - last_err_dt).total_seconds()
                if age <= WEBHOOK_ERROR_MAX_AGE_SEC:
                    return False, f"webhook FAIL (erro recente: {last_err_msg})", diag

        # fila acumulada
        if pending > WEBHOOK_PENDING_MAX:
            return False, f"webhook FAIL (pending={pending})", diag

        return True, "webhook OK", diag

    except Exception as e:
        return False, f"webhook erro {e.__class__.__name__}", diag

def _compose_url_for_health(url: Optional[str]) -> Optional[str]:
    if not url: return None
    if URL_HEALTH_PATH:
        try:
            # anexa /health de forma segura
            from urllib.parse import urljoin
            return urljoin(url.rstrip("/") + "/", URL_HEALTH_PATH.lstrip("/"))
        except Exception:
            return url
    return url

def safe_check_link(url: Optional[str]) -> Tuple[bool, str, Optional[int]]:
    if not url:
        return False, "sem URL", None
    target = _compose_url_for_health(url)
    try:
        r = requests_session.get(target, timeout=CHECK_TIMEOUT, allow_redirects=True)
        st = r.status_code
        if URL_EXPECT_2XX:
            if 200 <= st < 300:
                return True, "url OK", st
            return False, f"url HTTP {st}", st
        else:
            if 200 <= st < 400:
                return True, "url OK", st
            return False, f"url HTTP {st}", st
    except Exception as e:
        return False, f"url erro {e.__class__.__name__}", None

def decide_health(token_ok: bool, webhook_ok: bool, url_ok: bool) -> Tuple[bool, str]:
    pol = CHECK_STRATEGY
    if pol == "token_only":
        ok = token_ok
    elif pol == "url_only":
        ok = url_ok
    elif pol == "webhook_only":
        ok = webhook_ok
    elif pol == "token_and_url":
        ok = token_ok and url_ok
    elif pol == "token_or_url":
        ok = token_ok or url_ok
    else:
        # token_webhook_url (default): exige os três
        ok = token_ok and webhook_ok and url_ok
    reason = f"policy={pol} | token={'OK' if token_ok else 'FAIL'} | webhook={'OK' if webhook_ok else 'FAIL'} | url={'OK' if url_ok else 'FAIL'}"
    return ok, reason

# ---------- Swap ----------
bot_locks = {}
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
                if not fb: return
                fb.status, fb.failures, fb.last_reason = "reserva", 0, "trocado para reserva"
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
                replacement.status, replacement.failures, replacement.last_ok, replacement.last_reason = "ativo", 0, datetime.utcnow(), "substituto ativado"
            metrics["switches_total"] += 1
            add_log(f"🔁 Swap: {fb.name} ❌ → {replacement.name} ✅")
            _clear_alert_state(fb.id)
            notify_swap_summary(fb, replacement)
    finally:
        lock.release()

# ---------- Monitor ----------
def _record_diag(bot_id:int, diag:Diag):
    diag_state[bot_id] = diag

def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b: return

        # 1) token
        token_ok, token_reason, token_http = safe_check_token(b.token)
        # 2) webhook
        webhook_ok, webhook_reason, webhook_diag = safe_check_webhook(b.token, b.redirect_url)
        # 3) url
        url_ok, url_reason, url_http = safe_check_link(b.redirect_url)

        ok, policy_reason = decide_health(token_ok, webhook_ok, url_ok)
        full_reason = f"{policy_reason} | {token_reason} | {webhook_reason} | {url_reason}"

        # métricas + log
        metrics["checks_total"] += 1
        add_log(
            f"[{b.id}:{b.name}] "
            f"TOKEN:{'✅' if token_ok else '❌'}({token_http}) | "
            f"WEBHOOK:{'✅' if webhook_ok else '❌'}(url={webhook_diag.get('webhook_url','')}, pend={webhook_diag.get('pending_update_count')}, err={webhook_diag.get('last_error_message')}) | "
            f"URL:{'✅' if url_ok else '❌'}({url_http}) ⇒ "
            f"{'✅ OK' if ok else f'❌ FAIL ({(b.failures or 0)+1}/{FAIL_THRESHOLD})'}"
        )

        # guarda diagnóstico in-memory para a dashboard
        _record_diag(b.id, {
            "token_ok": token_ok, "token_http": token_http, "token_reason": token_reason,
            "webhook_ok": webhook_ok, "webhook_reason": webhook_reason, **webhook_diag,
            "url_ok": url_ok, "url_http": url_http, "url_reason": url_reason,
            "decision_ok": ok, "policy_reason": policy_reason,
            "ts": int(time.time())
        })

        # persistência em banco (colunas auxiliares)
        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            bot.last_reason = full_reason
            if hasattr(bot, "last_token_ok"):   bot.last_token_ok   = token_ok
            if hasattr(bot, "last_url_ok"):     bot.last_url_ok     = url_ok
            if hasattr(bot, "last_webhook_ok"): bot.last_webhook_ok = webhook_ok
            if hasattr(bot, "last_token_http"): bot.last_token_http = token_http
            if hasattr(bot, "last_url_http"):   bot.last_url_http   = url_http
            if hasattr(bot, "last_webhook_url"):   bot.last_webhook_url   = webhook_diag.get("webhook_url")
            if hasattr(bot, "last_webhook_error"): bot.last_webhook_error = webhook_diag.get("last_error_message")
            if hasattr(bot, "last_webhook_error_at"):
                dt = _utc_ts_to_dt(webhook_diag.get("last_error_date"))
                bot.last_webhook_error_at = dt
            if hasattr(bot, "pending_update_count"): bot.pending_update_count = webhook_diag.get("pending_update_count")

            if ok:
                bot.failures = 0
                bot.last_ok = datetime.utcnow()
                bot.updated_at = datetime.utcnow()
                _clear_alert_state(bot.id)
            else:
                bot.failures = (bot.failures or 0) + 1
                bot.updated_at = datetime.utcnow()
                metrics["failures_total"] += 1
                if bot.failures == 1 and _should_alert_now(bot.id):
                    notify_bot_down(b, bot.failures, full_reason)
                if bot.failures >= FAIL_THRESHOLD:
                    add_log(f"{bot.name} atingiu {bot.failures} falhas; agendando swap…")
                    threading.Thread(target=swap_bot, args=(bot.id,), daemon=True).start()

def _try_acquire_monitor_lock() -> bool:
    """Tenta adquirir um lock global no Postgres; retorna True se lock adquirido."""
    if not USE_DB_LOCK_FOR_MONITOR:
        return True
    try:
        with db.engine.begin() as conn:
            res = conn.execute(text("SELECT pg_try_advisory_lock(:k) AS locked"), {"k": DB_MONITOR_LOCK_KEY})
            row = res.fetchone()
            locked = bool(row and row[0])
            metrics["monitor_has_lock"] = locked
            return locked
    except Exception as e:
        add_log(f"⚠️ Falha ao adquirir DB advisory lock: {e}")
        return False

def monitor_loop():
    if not _try_acquire_monitor_lock():
        add_log("⛔ Outro processo já está rodando o monitor (lock não adquirido).")
        return

    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("🚀 Monitor iniciado.")
    metrics["monitor_running"] = True

    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)
        try:
            with app.app_context():
                bots = db.session.query(Bot).order_by(Bot.updated_at.asc(), Bot.id.asc()).all()
                metrics["bots_active"]  = sum(1 for b in bots if b.status=="ativo")
                metrics["bots_reserve"] = sum(1 for b in bots if b.status=="reserva")

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
    # junta o to_dict() com diagnóstico in-memory
    bots = Bot.query.order_by(Bot.id).all()
    items = []
    for b in bots:
        base = b.to_dict()
        diag = diag_state.get(b.id, {})
        base["_diag"] = diag
        items.append(base)
    return jsonify({
        "bots": items,
        "logs": monitor_logs,
        "metrics": metrics,
        "last_action": metrics.get("last_check_ts"),
        "last_action_human": datetime.utcfromtimestamp(metrics["last_check_ts"]).strftime("%Y-%m-%d %H:%M:%S") if metrics["last_check_ts"] else None
    })

# ---------- Bootstrap (patch de colunas auxiliares) ----------
PATCH_SQL = """
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT NULL;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_ok BOOLEAN;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_ok BOOLEAN;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_ok BOOLEAN;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_http INTEGER;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_http INTEGER;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_url TEXT;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error TEXT;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error_at TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS pending_update_count INTEGER;

ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();
"""
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(text(PATCH_SQL))
        add_log("✅ Auto-patch de schema aplicado (auxiliares).")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1' ou execução via CLI).")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")