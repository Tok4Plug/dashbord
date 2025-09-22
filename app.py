# ================================
# app.py (monitor full + PROBE ativo + métricas + alertas avançados)
# ================================
import os, sys, threading, time, logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, jsonify
from flask_migrate import Migrate
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from models import db, Bot
from utils import log_event

# ---------- Logger ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bot-monitor")

# ---------- Flask / Config ----------
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
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "6"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

ACTIVE_PROBE_ENABLED = os.getenv("ACTIVE_PROBE_ENABLED", "1") == "1"
ACTIVE_PROBE_DELETE  = os.getenv("ACTIVE_PROBE_DELETE", "1") == "1"
MONITOR_CHAT_ID      = os.getenv("MONITOR_CHAT_ID")

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

# ---------- Funções auxiliares ----------
def _utc_ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None

def _compose_url_for_health(url: Optional[str]) -> Optional[str]:
    """Aplica path customizado de health-check, se existir."""
    URL_HEALTH_PATH = os.getenv("URL_HEALTH_PATH", "").strip()
    if not url:
        return None
    if URL_HEALTH_PATH:
        from urllib.parse import urljoin
        return urljoin(url.rstrip("/") + "/", URL_HEALTH_PATH.lstrip("/"))
    return url

# ---------- Notificações (WhatsApp) ----------
def send_whatsapp_message(text_msg: str):
    TWILIO_SID  = os.getenv("TWILIO_SID")
    TWILIO_AUTH = os.getenv("TWILIO_AUTH")
    TWILIO_FROM = os.getenv("TWILIO_FROM")
    ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")
    if not (TWILIO_SID and TWILIO_AUTH and TWILIO_FROM and ADMIN_WHATSAPP):
        add_log("⚠️ Twilio não configurado corretamente")
        return False
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_AUTH)
        client.messages.create(
            body=text_msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        add_log("📲 WhatsApp enviado")
        return True
    except Exception as e:
        add_log(f"❌ Erro Twilio: {e}")
        return False

# ---------- Checagens ----------
def check_token(token: Optional[str]) -> bool:
    if not token: return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception: return False

def check_url(url: Optional[str]) -> bool:
    if not url: return False
    try:
        target = _compose_url_for_health(url)
        r = requests_session.get(target, timeout=CHECK_TIMEOUT)
        return 200 <= r.status_code < 400
    except Exception: return False

def check_probe(token: str, chat_id: str) -> bool:
    if not ACTIVE_PROBE_ENABLED: return True
    if not (token and chat_id): return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": f"probe:{int(time.time())}", "disable_notification": True}
        r = requests_session.post(url, data=payload, timeout=CHECK_TIMEOUT)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if ok and ACTIVE_PROBE_DELETE:
            msg_id = r.json()["result"]["message_id"]
            requests_session.post(
                f"https://api.telegram.org/bot{token}/deleteMessage",
                data={"chat_id": chat_id, "message_id": msg_id},
                timeout=CHECK_TIMEOUT
            )
        return ok
    except Exception: return False

# ---------- Diagnóstico ----------
def diagnosticar_bot(bot: Bot) -> Dict[str, Any]:
    token_ok = check_token(bot.token or "")
    url_ok   = check_url(bot.redirect_url or "")
    probe_ok = check_probe(bot.token, MONITOR_CHAT_ID)

    decision_ok = token_ok and url_ok and probe_ok
    reason = f"token={'OK' if token_ok else 'FAIL'} | url={'OK' if url_ok else 'FAIL'} | probe={'OK' if probe_ok else 'FAIL'}"
    return {"decision_ok": decision_ok, "reason": reason}

# ---------- Monitor ----------
def check_and_update(bot: Bot):
    diag = diagnosticar_bot(bot)
    metrics["checks_total"] += 1

    if diag["decision_ok"]:
        bot.reset_failures()
        bot.last_ok = datetime.utcnow()
        bot.last_reason = "OK"
        db.session.commit()
        add_log(f"✅ {bot.name} OK")
    else:
        bot.increment_failure()
        bot.last_reason = diag["reason"]
        db.session.commit()
        add_log(f"❌ {bot.name} FAIL ({bot.failures}/{FAIL_THRESHOLD}) - {diag['reason']}")

        if bot.failures >= FAIL_THRESHOLD:
            bot.mark_reserve()
            db.session.commit()
            add_log(f"🔁 {bot.name} movido para reserva")

            # Substitui por reserva
            reserva = Bot.query.filter_by(status="reserva").first()
            if reserva:
                reserva.mark_active()
                db.session.commit()
                add_log(f"✅ Substituído por {reserva.name}")
                send_whatsapp_message(f"🔁 Swap automático: {bot.name} ❌ → {reserva.name} ✅")
            else:
                send_whatsapp_message(f"❌ {bot.name} caiu e não há reservas disponíveis")

def monitor_loop():
    add_log("🚀 Monitor iniciado")
    metrics["monitor_running"] = True
    while True:
        try:
            with app.app_context():
                bots = Bot.query.order_by(Bot.id).all()
                metrics["bots_active"]  = sum(1 for b in bots if b.status == "ativo")
                metrics["bots_reserve"] = sum(1 for b in bots if b.status == "reserva")

                for bot in bots:
                    check_and_update(bot)
        except Exception as e:
            add_log(f"Erro monitor: {e}")
        time.sleep(MONITOR_INTERVAL)

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
        "last_action": metrics.get("last_check_ts")
    })

# ---------- Bootstrap ----------
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT NULL;"))
        add_log("✅ Schema patch aplicado")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1')")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")