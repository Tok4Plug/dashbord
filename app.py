# app.py (versão final com Flask-Migrate e robustez para alta escala)
import os
import threading
import time
import logging
from datetime import datetime
from typing import List, Optional
import urllib.parse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, request, jsonify
from flask_migrate import Migrate

# imports locais
from models import db, Bot
from utils import check_link

# ---------- Config & Logger ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot-monitor")

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "change_me_random")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Integração Flask-Migrate
db.init_app(app)
migrate = Migrate(app, db)

# ---------- Configurações ----------
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
CHECK_TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "7.0"))
MAX_LOGS = int(os.getenv("MAX_LOGS", "300"))
MAX_WORKERS = int(os.getenv("MONITOR_MAX_WORKERS", "8"))

# ---------- Logs ----------
monitor_logs: List[str] = []
def add_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    monitor_logs.append(line)
    if len(monitor_logs) > MAX_LOGS:
        monitor_logs.pop(0)
    logger.info(msg)

# ---------- Métricas ----------
metrics = {
    "checks_total": 0,
    "failures_total": 0,
    "switches_total": 0,
    "switch_errors_total": 0,
    "last_check_ts": None,
    "bots_active": 0,
    "bots_reserve": 0,
}

# Locks por bot
bot_locks = {}

# ---------- Sessão requests com retry ----------
def make_requests_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(500, 502, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

requests_session = make_requests_session()

# ---------- Notificações (WhatsApp) ----------
def _get_admin_whatsapps() -> List[str]:
    v = os.getenv("ADMIN_WHATSAPP", "")
    return [p.strip() for p in v.split(",") if p.strip()]

def send_whatsapp_message_text(to_number: Optional[str], text_msg: str) -> bool:
    TWILIO_SID = os.getenv("TWILIO_SID")
    TWILIO_AUTH = os.getenv("TWILIO_AUTH")
    TWILIO_FROM = os.getenv("TWILIO_FROM")
    CALLMEBOT_KEY = os.getenv("CALLMEBOT_KEY")
    recipients = [to_number] if to_number else _get_admin_whatsapps()

    # Twilio
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
            logger.exception("Erro Twilio")

    # CallMeBot (backup)
    if CALLMEBOT_KEY and recipients:
        try:
            for r in recipients:
                url = ("https://api.callmebot.com/whatsapp.php?"
                       f"phone={urllib.parse.quote_plus(r)}&text={urllib.parse.quote_plus(text_msg)}&apikey={urllib.parse.quote_plus(CALLMEBOT_KEY)}")
                requests_session.get(url, timeout=10)
            add_log("Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"Erro CallMeBot: {e}")
            logger.exception("Erro CallMeBot")

    add_log("Nenhuma integração WhatsApp configurada ou sem destinatário.")
    return False

# ---------- Verificações ----------
def safe_check_token(token: str) -> Optional[bool]:
    if not token:
        return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception:
        return False

def safe_check_link(url: str, retries: int = 2) -> Optional[bool]:
    if not url:
        return False
    try:
        r = requests_session.head(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        if 200 <= r.status_code < 400:
            return True
        return check_link(url, retries=retries)
    except Exception:
        return False

# ---------- Swap ----------
def swap_bot(failed_bot: Bot):
    bot_id = failed_bot.id
    lock = bot_locks.setdefault(bot_id, threading.Lock())
    if not lock.acquire(blocking=False):
        add_log(f"Swap já em andamento para {failed_bot.name}")
        return

    try:
        with app.app_context():
            session = db.session
            with session.begin():
                fb = session.get(Bot, bot_id)
                if not fb:
                    return
                fb.status = "reserva"
                fb.failures = 0
                replacement = session.query(Bot).filter(Bot.status == "reserva", Bot.id != fb.id).order_by(Bot.id).first()
                if not replacement:
                    send_whatsapp_message_text(None, f"❌ {fb.name} caiu e não há reservas!")
                    metrics["switch_errors_total"] += 1
                    return
                replacement.status = "ativo"
                replacement.failures = 0

            metrics["switches_total"] += 1
            msg = (f"🔁 Substituição automática:\n"
                   f"❌ Caiu: {fb.name}\n"
                   f"✅ Substituído por: {replacement.name}")
            send_whatsapp_message_text(None, msg)
            add_log(msg.replace("\n", " | "))
    finally:
        lock.release()

# ---------- Monitor ----------
def monitor_loop():
    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("Monitor iniciado.")

    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)

        try:
            with app.app_context():
                bots = db.session.query(Bot).all()
                metrics["bots_active"] = sum(1 for b in bots if b.status == "ativo")
                metrics["bots_reserve"] = sum(1 for b in bots if b.status == "reserva")

                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    ex.map(lambda b: check_and_maybe_swap(b.id), bots)
        except Exception as e:
            add_log(f"Erro monitor: {e}")
            logger.exception("Erro monitor_loop")

        time.sleep(max(0, MONITOR_INTERVAL - (time.time() - start_ts)))

def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        bot = db.session.get(Bot, bot_id)
        if not bot:
            return

        ok_url = safe_check_link(bot.redirect_url, retries=2) if bot.redirect_url else None
        ok_token = safe_check_token(bot.token) if bot.token else None
        ok = ok_url or ok_token

        metrics["checks_total"] += 1
        add_log(f"Check {bot.name}: "
                f"URL={'OK' if ok_url else 'FAIL'} | "
                f"TOKEN={'OK' if ok_token else 'FAIL'} | "
                f"RESULT={'✅' if ok else '❌'}")

        with db.session.begin():
            b = db.session.get(Bot, bot_id)
            if ok:
                b.failures = 0
                b.last_ok = datetime.utcnow()
            else:
                b.failures = (b.failures or 0) + 1
                metrics["failures_total"] += 1
                if b.failures >= FAIL_THRESHOLD:
                    add_log(f"{b.name} atingiu {b.failures} falhas consecutivas, agendando swap...")
                    threading.Thread(target=swap_bot, args=(b,), daemon=True).start()

# ---------- Rotas ----------
@app.route("/")
def index():
    bots = Bot.query.all()
    return render_template("dashboard.html", bots=bots, logs=monitor_logs, metrics=metrics)

@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    return jsonify({"bots": [b.to_dict() for b in Bot.query.all()]})

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "active": metrics["bots_active"],
        "reserve": metrics["bots_reserve"],
        "last_check": metrics["last_check_ts"],
    }), 200

@app.route("/metrics")
def metrics_endpoint():
    return jsonify(metrics)

# ---------- Start Monitor ----------
def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    add_log("Thread monitor iniciada.")

start_monitor_thread()

if __name__ == "__main__":
    # Não criamos tabelas automaticamente; usamos `flask db migrate/upgrade`
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=os.getenv("DEBUG", "True") == "True")