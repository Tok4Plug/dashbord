# ================================
# app.py (monitor avançado + política de check + alertas)
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
MAX_LOGS         = int(os.getenv("MAX_LOGS", "300"))
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8"))
START_MONITOR    = os.getenv("START_MONITOR", "1")

# Política de decisão do health:
#  - token_only     : OK se token OK (padrão anterior)
#  - token_and_url  : OK somente se token e URL OK
#  - token_or_url   : OK se token OU URL OK
#  - url_only       : OK somente se URL OK
CHECK_STRATEGY = os.getenv("CHECK_STRATEGY", "token_only").strip().lower()

# Alertas
ALERT_ON_FIRST_FAIL   = os.getenv("ALERT_ON_FIRST_FAIL", "1") == "1"
ALERT_COOLDOWN_MIN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
ALERT_SUMMARY_ON_SWAP = os.getenv("ALERT_SUMMARY_ON_SWAP", "1") == "1"

# Evita subir monitor durante comandos do Flask (migrate, etc.)
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

    # Twilio (preferencial)
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

    # CallMeBot (backup)
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
        f"⚠️ Bot com falha\n"
        f"• Nome: {bot.name}\n"
        f"• Falhas consecutivas: {failures}/{FAIL_THRESHOLD}\n"
        f"• {reason}\n"
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

# ---------- Health checks ----------
def safe_check_token(token: Optional[str]) -> bool:
    if not token: 
        return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        return r.status_code == 200 and r.headers.get("content-type","").startswith("application/json") and r.json().get("ok", False)
    except Exception:
        return False

def safe_check_link(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        r = requests_session.head(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        if 200 <= r.status_code < 400:
            return True
    except Exception:
        pass
    # fallback GET
    try:
        r2 = requests_session.get(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        return 200 <= r2.status_code < 400
    except Exception:
        return False

def decide_health(ok_token: bool, ok_url: bool) -> Tuple[bool, str]:
    pol = CHECK_STRATEGY
    if pol == "token_and_url":
        ok = bool(ok_token) and bool(ok_url)
    elif pol == "token_or_url":
        ok = bool(ok_token) or bool(ok_url)
    elif pol == "url_only":
        ok = bool(ok_url)
    else:  # token_only (default)
        ok = bool(ok_token)
    reason = f"policy={pol} | token={'OK' if ok_token else 'FAIL'} | url={'OK' if ok_url else 'FAIL'}"
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
                if not fb:
                    return
                fb.status, fb.failures = "reserva", 0
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
            metrics["switches_total"] += 1
            add_log(f"🔁 Swap: {fb.name} ❌ → {replacement.name} ✅")
            _clear_alert_state(fb.id)
            notify_swap_summary(fb, replacement)
    finally:
        lock.release()

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

        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            if ok:
                bot.failures = 0
                bot.last_ok = datetime.utcnow()
                bot.updated_at = datetime.utcnow()
                _clear_alert_state(bot.id)
                add_log(f"[{bot.id}:{bot.name}] {reason} ⇒ ✅ (fails=0/{FAIL_THRESHOLD})")
            else:
                bot.failures = (bot.failures or 0) + 1
                bot.updated_at = datetime.utcnow()
                metrics["failures_total"] += 1
                add_log(f"[{bot.id}:{bot.name}] {reason} ⇒ ❌ (fails={bot.failures}/{FAIL_THRESHOLD})")

                # alerta imediato no 1º erro (com cooldown)
                if bot.failures == 1 and _should_alert_now(bot.id):
                    notify_bot_down(b, bot.failures, reason)

                # disparar swap ao atingir o threshold
                if bot.failures >= FAIL_THRESHOLD:
                    add_log(f"{bot.name} atingiu {bot.failures} falhas consecutivas; agendando swap…")
                    threading.Thread(target=swap_bot, args=(bot.id,), daemon=True).start()

def monitor_loop():
    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("🚀 Monitor iniciado.")
    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)
        try:
            with app.app_context():
                bots = db.session.query(Bot).order_by(Bot.updated_at.asc(), Bot.id.asc()).all()
                metrics["bots_active"]  = sum(1 for x in bots if x.status=="ativo")
                metrics["bots_reserve"] = sum(1 for x in bots if x.status=="reserva")

                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    for b in bots:
                        ex.submit(check_and_maybe_swap, b.id)
        except Exception as e:
            add_log(f"Erro monitor: {e}")

        time.sleep(max(0, MONITOR_INTERVAL - (time.time()-start_ts)))

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

@app.route("/api/bots", methods=["POST"])
def api_create_bot():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip() or None
    redirect_url = (data.get("redirect_url") or "").strip()
    status = (data.get("status") or "reserva").strip()

    if not name or not redirect_url:
        return jsonify({"error":"name e redirect_url obrigatórios"}), 400

    with db.session.begin():
        bot = Bot(name=name, token=token, redirect_url=redirect_url, status=status)
        db.session.add(bot)

    add_log(f"Bot criado: {name} (status={status})")
    return jsonify({"bot": bot.to_dict()}), 201

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def api_update_bot(bot_id):
    data = request.get_json(force=True) or {}
    with db.session.begin():
        bot = db.session.get(Bot, bot_id)
        if not bot:
            return jsonify({"error":"not found"}), 404
        bot.name = (data.get("name", bot.name) or "").strip()
        bot.token = (data.get("token", bot.token) or "") or None
        bot.redirect_url = (data.get("redirect_url", bot.redirect_url) or "").strip()
        bot.status = (data.get("status", bot.status) or bot.status).strip()
        bot.updated_at = datetime.utcnow()

    add_log(f"Bot atualizado: {bot.name}")
    return jsonify({"bot": bot.to_dict()})

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def api_delete_bot(bot_id):
    with db.session.begin():
        bot = db.session.get(Bot, bot_id)
        if not bot:
            return jsonify({"error":"not found"}), 404
        db.session.delete(bot)
    add_log(f"Bot removido: {bot_id}")
    return jsonify({"success": True})

@app.route("/api/bots/<int:bot_id>/force_swap", methods=["POST"])
def api_force_swap(bot_id):
    threading.Thread(target=swap_bot, args=(bot_id,), daemon=True).start()
    add_log(f"Swap forçado para bot_id={bot_id}")
    return jsonify({"success": True})

@app.route("/health")
def health():
    return jsonify({"status":"ok", **metrics})

@app.route("/metrics")
def metrics_endpoint():
    return jsonify(metrics)

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
        add_log(f"⚠️ Patch falhou: {e}")

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1' ou execução via CLI).")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("DEBUG","True")=="True")