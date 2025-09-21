# ================================
# app.py (monitor avançado + logs informativos + alertas detalhados)
# ================================
import os, sys, threading, time, logging, urllib.parse
from datetime import datetime
from typing import List, Optional, Tuple
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

CHECK_STRATEGY   = os.getenv("CHECK_STRATEGY", "token_only").strip().lower()
ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

if os.getenv("FLASK_RUN_FROM_CLI") == "true" or "flask" in (sys.argv[0] if sys.argv else "").lower():
    START_MONITOR = "0"

# ---------- Logs / Métricas ----------
monitor_logs: List[str] = []
def add_log(msg: str, level: str = "INFO"):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    monitor_logs.append(line)
    if len(monitor_logs) > MAX_LOGS:
        monitor_logs.pop(0)
    if level == "ERROR":
        logger.error(msg)
    elif level == "WARN":
        logger.warning(msg)
    else:
        logger.info(msg)

metrics = {
    "checks_total": 0, "failures_total": 0,
    "switches_total": 0, "switch_errors_total": 0,
    "last_check_ts": None, "bots_active": 0, "bots_reserve": 0,
}

# ---------- Sessão requests ----------
def make_requests_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=(500,502,503,504),
                  allowed_methods=False,
                  raise_on_status=False)
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

    if not recipients:
        add_log("⚠️ Nenhum destinatário configurado para WhatsApp.", "WARN")
        return False

    # Twilio (preferencial)
    if TWILIO_SID and TWILIO_AUTH and TWILIO_FROM:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_AUTH)
            for r in recipients:
                client.messages.create(body=text_msg,
                                       from_=f"whatsapp:{TWILIO_FROM}",
                                       to=f"whatsapp:{r}")
            add_log("📲 Mensagem WhatsApp enviada via Twilio.")
            return True
        except Exception as e:
            add_log(f"❌ Erro Twilio: {e}", "ERROR")

    # CallMeBot (backup)
    if CALLMEBOT_KEY:
        try:
            for r in recipients:
                url = (f"https://api.callmebot.com/whatsapp.php?"
                       f"phone={urllib.parse.quote_plus(r)}"
                       f"&text={urllib.parse.quote_plus(text_msg)}"
                       f"&apikey={CALLMEBOT_KEY}")
                requests_session.get(url, timeout=10)
            add_log("📲 Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"❌ Erro CallMeBot: {e}", "ERROR")

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
        f"⚠️ Bot com falha detectada!\n"
        f"• Nome: {bot.name}\n"
        f"• Falhas: {failures}/{FAIL_THRESHOLD}\n"
        f"• Motivo: {reason}\n"
        f"🕒 {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )
    add_log(f"⚠️ ALERTA disparado: {msg}")
    send_whatsapp_message_text(None, msg)

# ---------- Health checks ----------
def safe_check_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception as e:
        add_log(f"Erro check token: {e}", "ERROR")
        return False

def safe_check_link(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        r = requests_session.get(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception as e:
        add_log(f"Erro check link: {e}", "ERROR")
        return False

def decide_health(ok_token: bool, ok_url: bool) -> Tuple[bool, str]:
    pol = CHECK_STRATEGY
    if pol == "token_and_url":
        ok = ok_token and ok_url
    elif pol == "token_or_url":
        ok = ok_token or ok_url
    elif pol == "url_only":
        ok = ok_url
    else:
        ok = ok_token
    reason = f"policy={pol} | token={'OK' if ok_token else 'FAIL'} | url={'OK' if ok_url else 'FAIL'}"
    return ok, reason

# ---------- Monitor ----------
def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b:
            return
        ok_token = safe_check_token(b.token)
        ok_url   = safe_check_link(b.redirect_url)
        ok, reason = decide_health(ok_token, ok_url)

        metrics["checks_total"] += 1
        status_msg = f"[{b.id}:{b.name}] {reason} ⇒ {'✅ OK' if ok else '❌ FAIL'}"

        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            if ok:
                bot.failures = 0
                bot.last_ok = datetime.utcnow()
                bot.updated_at = datetime.utcnow()
                _clear_alert_state(bot.id)
                add_log(f"{status_msg} | fails=0/{FAIL_THRESHOLD}")
            else:
                bot.failures = (bot.failures or 0) + 1
                bot.updated_at = datetime.utcnow()
                metrics["failures_total"] += 1
                add_log(f"{status_msg} | fails={bot.failures}/{FAIL_THRESHOLD}", "WARN")

                if bot.failures == 1 and _should_alert_now(bot.id):
                    notify_bot_down(b, bot.failures, reason)

                if bot.failures >= FAIL_THRESHOLD:
                    add_log(f"🚨 {bot.name} atingiu limite de falhas! Iniciando swap…", "ERROR")

def monitor_loop():
    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("🚀 Monitor iniciado.")
    while True:
        metrics["last_check_ts"] = int(time.time())
        try:
            with app.app_context():
                bots = db.session.query(Bot).order_by(Bot.id.asc()).all()
                metrics["bots_active"]  = sum(1 for x in bots if x.status=="ativo")
                metrics["bots_reserve"] = sum(1 for x in bots if x.status=="reserva")

                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    for b in bots:
                        ex.submit(check_and_maybe_swap, b.id)
        except Exception as e:
            add_log(f"❌ Erro monitor loop: {e}", "ERROR")

        time.sleep(MONITOR_INTERVAL)

def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True, name="bot-monitor")
    t.start()
    add_log("Thread monitor iniciada.")

# ---------- Rotas ----------
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    return jsonify({
        "bots": [b.to_dict() for b in Bot.query.order_by(Bot.id).all()],
        "logs": monitor_logs,
        "last_action": metrics.get("last_check_ts")
    })

# (demais rotas iguais às anteriores…)

# ---------- Bootstrap ----------
PATCH_SQL = """
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();
"""
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(text(PATCH_SQL))
        add_log("✅ Auto-patch de schema aplicado.")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}", "ERROR")

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1').")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")