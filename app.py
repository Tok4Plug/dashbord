# ================================
# app.py (versão avançada + REST sync + alertas)
# ================================
import os, sys, threading, time, logging, urllib.parse
from datetime import datetime
from typing import List, Optional
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, request, jsonify
from flask_migrate import Migrate
from sqlalchemy import text

# imports locais
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
MAX_LOGS         = int(os.getenv("MAX_LOGS", "300"))
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

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
}

# ---------- Sessão requests ----------
def make_requests_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(500,502,503,504),
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
    CALLMEBOT_KEY = os.getenv("CALLMEBOT_KEY")
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

    if CALLMEBOT_KEY and recipients:
        try:
            for r in recipients:
                url = (f"https://api.callmebot.com/whatsapp.php?"
                       f"phone={urllib.parse.quote_plus(r)}&text={urllib.parse.quote_plus(text_msg)}&apikey={CALLMEBOT_KEY}")
                requests_session.get(url, timeout=10)
            add_log("Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"Erro CallMeBot: {e}")

    return False

# ---------- Alertas com cooldown ----------
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

def notify_bot_down(bot: Bot, failures: int):
    msg = (
        f"⚠️ Bot possivelmente fora do ar\n"
        f"• Nome: {bot.name}\n"
        f"• Falhas: {failures}\n"
        f"🕒 {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )
    send_whatsapp_message_text(None, msg)

def notify_swap_summary(failed: Bot, replacement: Bot):
    actives = db.session.query(Bot).filter_by(status="ativo").all()
    reserves = db.session.query(Bot).filter_by(status="reserva").all()
    msg = (
        "🔁 Substituição executada\n"
        f"❌ Caiu: {failed.name}\n"
        f"✅ Entrou: {replacement.name}\n\n"
        f"📊 Ativos: {len(actives)} | Reserva: {len(reserves)}"
    )
    if ALERT_SUMMARY_ON_SWAP:
        send_whatsapp_message_text(None, msg)

# ---------- Health check ----------
def safe_check_token(token: str) -> bool:
    if not token: return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception: return False

# ---------- Swap ----------
bot_locks = {}
def swap_bot(failed_bot_id: int):
    lock = bot_locks.setdefault(failed_bot_id, threading.Lock())
    if not lock.acquire(blocking=False): return
    try:
        with app.app_context():
            session = db.session
            with session.begin():
                fb = session.get(Bot, failed_bot_id)
                if not fb: return
                fb.status, fb.failures = "reserva", 0
                replacement = session.query(Bot).filter(Bot.status=="reserva", Bot.id!=fb.id).order_by(Bot.updated_at.asc()).first()
                if not replacement:
                    send_whatsapp_message_text(None, f"❌ {fb.name} caiu e não há reservas!")
                    metrics["switch_errors_total"] += 1
                    return
                replacement.status, replacement.failures, replacement.last_ok = "ativo", 0, datetime.utcnow()
            metrics["switches_total"] += 1
            add_log(f"🔁 Swap: {fb.name} ❌ → {replacement.name} ✅")
            _clear_alert_state(fb.id)
            notify_swap_summary(fb, replacement)
    finally: lock.release()

# ---------- Monitor ----------
def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b: return
        ok = safe_check_token(b.token)
        metrics["checks_total"] += 1
        add_log(f"Check {b.name}: {'✅' if ok else '❌'}")
        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            if ok:
                bot.failures, bot.last_ok, bot.updated_at = 0, datetime.utcnow(), datetime.utcnow()
                _clear_alert_state(bot.id)
            else:
                bot.failures = (bot.failures or 0) + 1
                bot.updated_at = datetime.utcnow()
                metrics["failures_total"] += 1
                if bot.failures == 1 and _should_alert_now(bot.id):
                    notify_bot_down(b, bot.failures)
                if bot.failures >= FAIL_THRESHOLD:
                    threading.Thread(target=swap_bot, args=(bot.id,), daemon=True).start()

def monitor_loop():
    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("🚀 Monitor iniciado.")
    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)
        try:
            with app.app_context():
                bots = db.session.query(Bot).all()
                metrics["bots_active"] = sum(1 for b in bots if b.status=="ativo")
                metrics["bots_reserve"] = sum(1 for b in bots if b.status=="reserva")
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    for b in bots: ex.submit(check_and_maybe_swap, b.id)
        except Exception as e:
            add_log(f"Erro monitor: {e}")
        time.sleep(max(0, MONITOR_INTERVAL-(time.time()-start_ts)))

def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

# ---------- Rotas ----------
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    return jsonify({
        "bots": [b.to_dict() for b in Bot.query.order_by(Bot.id).all()],
        "logs": monitor_logs, "last_action": metrics.get("last_check_ts")
    })

@app.route("/api/bots", methods=["POST"])
def api_create_bot():
    data = request.get_json(force=True) or {}
    name, token, redirect_url = data.get("name","").strip(), data.get("token","").strip(), data.get("redirect_url","").strip()
    if not name or not redirect_url:
        return jsonify({"error":"name e redirect_url obrigatórios"}),400
    with db.session.begin():
        bot = Bot(name=name, token=token or None, redirect_url=redirect_url, status=data.get("status","reserva"))
        db.session.add(bot)
    add_log(f"Bot criado: {name}")
    return jsonify({"bot": bot.to_dict()}),201

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def api_update_bot(bot_id):
    data = request.get_json(force=True) or {}
    with db.session.begin():
        bot = db.session.get(Bot, bot_id)
        if not bot: return jsonify({"error":"not found"}),404
        bot.name, bot.token, bot.redirect_url, bot.status = data.get("name",bot.name), data.get("token",bot.token), data.get("redirect_url",bot.redirect_url), data.get("status",bot.status)
        bot.updated_at = datetime.utcnow()
    add_log(f"Bot atualizado: {bot.name}")
    return jsonify({"bot": bot.to_dict()})

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def api_delete_bot(bot_id):
    with db.session.begin():
        bot = db.session.get(Bot, bot_id)
        if not bot: return jsonify({"error":"not found"}),404
        db.session.delete(bot)
    add_log(f"Bot removido: {bot_id}")
    return jsonify({"success":True})

@app.route("/api/bots/<int:bot_id>/force_swap", methods=["POST"])
def api_force_swap(bot_id):
    threading.Thread(target=swap_bot, args=(bot_id,), daemon=True).start()
    add_log(f"Swap forçado para bot_id={bot_id}")
    return jsonify({"success":True})

@app.route("/health")
def health(): return jsonify({"status":"ok",**metrics})

@app.route("/metrics")
def metrics_endpoint(): return jsonify(metrics)

# ---------- Bootstrap ----------
PATCH_SQL = """
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();
"""
with app.app_context():
    try:
        with db.engine.begin() as conn: conn.execute(text(PATCH_SQL))
        add_log("✅ Auto-patch de schema aplicado.")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR=="1": start_monitor_thread()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")