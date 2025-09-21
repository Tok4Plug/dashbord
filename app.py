# ================================
# app.py (monitor full + webhook-info + auxiliares + alertas + métricas)
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
MAX_LOGS         = int(os.getenv("MAX_LOGS", "400"))
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

# Estratégia de check
CHECK_STRATEGY = os.getenv("CHECK_STRATEGY", "token_webhook_url").strip().lower()

# Webhook thresholds
REQUIRE_WEBHOOK_MATCH     = os.getenv("REQUIRE_WEBHOOK_MATCH", "1") == "1"
WEBHOOK_ERROR_MAX_AGE_SEC = int(os.getenv("WEBHOOK_ERROR_MAX_AGE_SEC", "1800"))
WEBHOOK_PENDING_MAX       = int(os.getenv("WEBHOOK_PENDING_MAX", "50"))

# Endpoint health
URL_EXPECT_2XX  = os.getenv("URL_EXPECT_2XX", "1") == "1"
URL_HEALTH_PATH = os.getenv("URL_HEALTH_PATH", "").strip()

# Alertas
ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

# Unidade única (evita múltiplos monitores)
USE_DB_LOCK_FOR_MONITOR = os.getenv("USE_DB_LOCK_FOR_MONITOR", "1") == "1"
DB_MONITOR_LOCK_KEY     = int(os.getenv("DB_MONITOR_LOCK_KEY", "72491371"))

# Evita subir monitor durante migrations/CLI
if os.getenv("FLASK_RUN_FROM_CLI") == "true" or "flask" in (sys.argv[0]).lower():
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
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=(500,502,503,504),
                  allowed_methods=False, raise_on_status=False)
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
    from twilio.rest import Client

    recipients = [to_number] if to_number else _get_admin_whatsapps()
    if TWILIO_SID and TWILIO_AUTH and TWILIO_FROM and recipients:
        try:
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

# ---------- Health helpers ----------
def _utc_ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None

def safe_check_token(token: str) -> Tuple[bool, str]:
    if not token: return False, "sem token"
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "token OK"
        return False, f"token FAIL {r.json().get('description','')}"
    except Exception as e:
        return False, f"token erro {e.__class__.__name__}"

def safe_check_webhook(token: str, expected_url: str) -> Tuple[bool, str]:
    if not token: return False, "webhook sem token"
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=CHECK_TIMEOUT)
        data = r.json() if r.ok else {}
        info = data.get("result", {}) if data.get("ok") else {}
        url = info.get("url","")
        pending = info.get("pending_update_count",0)
        err = info.get("last_error_message","")
        err_date = info.get("last_error_date")
        if REQUIRE_WEBHOOK_MATCH and expected_url and url!=expected_url:
            return False, f"webhook url difere (got={url})"
        if err_date and (datetime.utcnow()-_utc_ts_to_dt(err_date)).total_seconds() < WEBHOOK_ERROR_MAX_AGE_SEC:
            return False, f"webhook erro recente: {err}"
        if pending > WEBHOOK_PENDING_MAX:
            return False, f"webhook pendente alto ({pending})"
        return True, "webhook OK"
    except Exception as e:
        return False, f"webhook erro {e.__class__.__name__}"

def safe_check_link(url: str) -> Tuple[bool, str]:
    if not url: return False, "sem URL"
    target = url + (URL_HEALTH_PATH if URL_HEALTH_PATH else "")
    try:
        r = requests_session.get(target, timeout=CHECK_TIMEOUT)
        if URL_EXPECT_2XX and 200 <= r.status_code < 300:
            return True, "url OK"
        if not URL_EXPECT_2XX and 200 <= r.status_code < 400:
            return True, "url OK"
        return False, f"url FAIL {r.status_code}"
    except Exception as e:
        return False, f"url erro {e.__class__.__name__}"

def decide_health(token_ok, webhook_ok, url_ok) -> Tuple[bool,str]:
    pol = CHECK_STRATEGY
    if pol=="token_only": ok=token_ok
    elif pol=="url_only": ok=url_ok
    elif pol=="webhook_only": ok=webhook_ok
    elif pol=="token_and_url": ok=token_ok and url_ok
    elif pol=="token_or_url": ok=token_ok or url_ok
    else: ok=token_ok and webhook_ok and url_ok
    return ok, f"policy={pol} token={token_ok} webhook={webhook_ok} url={url_ok}"

# ---------- Swap ----------
def swap_bot(failed_bot_id: int):
    with app.app_context():
        fb = db.session.get(Bot, failed_bot_id)
        if not fb: return
        fb.status, fb.failures = "reserva", 0
        rep = db.session.query(Bot).filter_by(status="reserva").first()
        if not rep:
            add_log(f"❌ {fb.name} caiu e não há reservas!")
            return
        rep.status, rep.failures, rep.last_ok = "ativo", 0, datetime.utcnow()
        db.session.commit()
        add_log(f"🔁 Swap: {fb.name} ❌ → {rep.name} ✅")

# ---------- Monitor ----------
def _try_acquire_monitor_lock() -> bool:
    if not USE_DB_LOCK_FOR_MONITOR: return True
    with app.app_context():
        try:
            with db.engine.begin() as conn:
                res = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k":DB_MONITOR_LOCK_KEY})
                return bool(res.scalar())
        except Exception as e:
            add_log(f"⚠️ Falha lock: {e}")
            return False

def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b: return
        token_ok, treason = safe_check_token(b.token)
        webhook_ok, wreason = safe_check_webhook(b.token, b.redirect_url)
        url_ok, ureason = safe_check_link(b.redirect_url)
        ok, preason = decide_health(token_ok, webhook_ok, url_ok)
        metrics["checks_total"] += 1
        add_log(f"[{b.name}] {preason} | {treason} | {wreason} | {ureason} ⇒ {'OK' if ok else 'FAIL'}")
        b.last_reason = f"{treason} | {wreason} | {ureason}"
        if ok: b.failures, b.last_ok = 0, datetime.utcnow()
        else: b.failures = (b.failures or 0)+1
        db.session.commit()
        if not ok and b.failures>=FAIL_THRESHOLD:
            swap_bot(b.id)

def monitor_loop():
    if not _try_acquire_monitor_lock():
        add_log("⛔ Outro processo já está rodando o monitor.")
        return
    add_log("🚀 Monitor iniciado.")
    while True:
        with app.app_context():
            bots = Bot.query.all()
            for b in bots:
                check_and_maybe_swap(b.id)
        time.sleep(MONITOR_INTERVAL)

def start_monitor_thread():
    threading.Thread(target=monitor_loop, daemon=True).start()

# ---------- Rotas ----------
@app.route("/")
def index(): return render_template("dashboard.html")

@app.route("/api/bots")
def api_bots():
    with app.app_context():
        return jsonify({"bots":[b.to_dict() for b in Bot.query.all()],
                        "logs":monitor_logs,"metrics":metrics})

# ---------- Bootstrap ----------
PATCH_SQL = """
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT;
"""
with app.app_context():
    try:
        with db.engine.begin() as conn: conn.execute(text(PATCH_SQL))
        add_log("✅ Patch aplicado.")
    except Exception as e: add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR=="1": start_monitor_thread()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=True)