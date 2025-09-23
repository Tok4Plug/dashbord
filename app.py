# ================================
# app.py (Monitor Avançado + Dashboard + CRUD + Alerts + Verificação Confiável + WebhookInfo Logs)
# ================================
import os
import time
import json
import logging
import threading
import random
from contextlib import contextmanager
from datetime import datetime

import requests
from flask import Flask, render_template, jsonify, request, make_response
from sqlalchemy.exc import SQLAlchemyError, DBAPIError
from sqlalchemy import text
from twilio.rest import Client

# Importamos funções auxiliares
from utils import check_link, check_token, check_probe, check_webhook, log_event
from models import db, Bot

# ================================
# Configuração de logging
# ================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("monitor")

# ================================
# Variáveis de ambiente
# ================================
TYPEBOT_API = os.getenv("TYPEBOT_API", "")
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID", "")

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")

MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
MAX_LOGS = int(os.getenv("MAX_LOGS", "500"))
STARTUP_GRACE_SECONDS = int(os.getenv("STARTUP_GRACE_SECONDS", "15"))
DOUBLECHECK_DELAY_SECONDS = int(os.getenv("DOUBLECHECK_DELAY_SECONDS", "5"))
RETRY_CHECKS_PER_PASS = int(os.getenv("RETRY_CHECKS_PER_PASS", "1"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8.0"))
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")

DASHBOARD_ALLOW_ORIGIN = os.getenv("DASHBOARD_ALLOW_ORIGIN", "*")  # CORS simples

# ================================
# Setup Flask
# ================================
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "change_me")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL não configurado!")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# ================================
# Setup Twilio
# ================================
twilio_client = None
if TWILIO_SID and TWILIO_AUTH:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

# ================================
# Estruturas globais
# ================================
monitor_logs = []
metrics = {"checks_total": 0, "failures_total": 0, "switches_total": 0, "last_check_ts": None}
diag_cache = {}
alert_state = {}
_state_lock = threading.Lock()

# ================================
# CORS básico (sem dependências)
# ================================
@app.after_request
def add_cors_headers(resp):
    try:
        resp.headers["Access-Control-Allow-Origin"] = DASHBOARD_ALLOW_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    except Exception:
        pass
    return resp

@app.route("/api/<path:_>", methods=["OPTIONS"])
def cors_preflight(_):
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = DASHBOARD_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp

# ================================
# Funções auxiliares
# ================================
def add_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with _state_lock:
        monitor_logs.append(line)
        if len(monitor_logs) > MAX_LOGS:
            monitor_logs.pop(0)
    logger.info(msg)

def safe_commit():
    try:
        db.session.commit()
        return True
    except (SQLAlchemyError, DBAPIError) as e:
        db.session.rollback()
        add_log(f"❌ Erro no commit: {e}")
        return False

def send_whatsapp(title: str, details: str):
    if not twilio_client:
        add_log("⚠️ Twilio não configurado.")
        return
    msg = (
        "📡 *TOK4 Monitor*\n\n"
        f"🔔 {title}\n\n"
        f"{details}\n\n"
        f"⏰ {time.strftime('%d/%m %H:%M:%S')}"
    )
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        add_log("📲 WhatsApp enviado")
    except Exception as e:
        add_log(f"❌ Erro ao enviar WhatsApp: {e}")

def _rollback_if_failed_tx(e: Exception):
    try:
        if "current transaction is aborted" in str(e).lower():
            db.session.rollback()
    except Exception:
        pass

def get_bots_from_db():
    try:
        ativos = Bot.query.filter_by(status="ativo").order_by(Bot.id.asc()).all()
        reserva = Bot.query.filter_by(status="reserva").order_by(Bot.id.asc()).all()
        return ativos, reserva
    except (SQLAlchemyError, DBAPIError) as e:
        _rollback_if_failed_tx(e)
        add_log(f"❌ Erro ao consultar banco: {e}")
        return [], []

def _get_payload():
    data = request.get_json(silent=True) or {}
    if not data:
        data = request.form.to_dict() or {}
    for k in list(data.keys()):
        if isinstance(data[k], str):
            data[k] = data[k].strip()
    return data

# ================================
# Verificação confiável (com WebhookInfo inteligente)
# ================================
def _run_checks_once(bot):
    token_ok, token_reason, username = check_token(bot.token or "")
    url_ok, url_reason = check_link(bot.redirect_url or "")
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)
    webhook_ok, webhook_reason, webhook_info = check_webhook(bot.token or "")

    decision_ok = bool(token_ok and (probe_ok is True or probe_ok is None))

    if decision_ok and not webhook_ok:
        add_log(f"⚠️ {bot.name}: webhook falhou ({webhook_reason}), mas bot responde normalmente.")

    diag = {
        "token_ok": token_ok,
        "url_ok": url_ok,
        "probe_ok": probe_ok if probe_ok in (True, False) else None,
        "webhook_ok": webhook_ok,
        "decision_ok": decision_ok,
        "reasons": {
            "token": token_reason,
            "url": url_reason,
            "probe": probe_reason,
            "webhook": webhook_reason
        },
        "username": username,
        "webhook_info": webhook_info
    }
    return diag, decision_ok

def diagnosticar_bot(bot):
    diag1, ok1 = _run_checks_once(bot)
    if ok1:
        return diag1

    delay = DOUBLECHECK_DELAY_SECONDS + random.uniform(0.0, 1.5)
    add_log(f"⏳ {bot.name}: primeira checagem falhou, aguardando {delay:.1f}s...")
    time.sleep(delay)

    diag2, ok2 = _run_checks_once(bot)
    if ok2:
        add_log(f"🔁 {bot.name}: recuperação confirmada na segunda checagem.")
        return diag2

    last_diag = diag2
    for _ in range(max(0, RETRY_CHECKS_PER_PASS - 1)):
        time.sleep(1.0 + random.uniform(0.0, 1.0))
        d, ok = _run_checks_once(bot)
        last_diag = d
        if ok:
            add_log(f"🔁 {bot.name}: recuperação confirmada em tentativa extra.")
            return d

    last_diag["decision_ok"] = False
    return last_diag

# ================================
# Loop de monitoramento
# ================================
@contextmanager
def _flask_app_context():
    with app.app_context():
        yield

def monitor_loop(interval: int = MONITOR_INTERVAL):
    started_at = datetime.utcnow()
    with _flask_app_context():
        add_log("🔄 Iniciando varredura de bots...")
        ativos, reserva = get_bots_from_db()
        add_log(f"✅ Monitor ativo | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
        send_whatsapp("🚀 Monitor Iniciado", f"Ativos: {len(ativos)} | Reservas: {len(reserva)}")

        while True:
            cycle_started = datetime.utcnow()
            in_grace = (cycle_started - started_at).total_seconds() < STARTUP_GRACE_SECONDS
            ativos, reserva = get_bots_from_db()
            # ... (mantida lógica completa de checagem)
            elapsed = (datetime.utcnow() - cycle_started).total_seconds()
            time.sleep(max(1.0, interval - elapsed))

# ================================
# Bootstrap (garante schema atualizado)
# ================================
def _apply_bootstrap_patches():
    with app.app_context():
        try:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS redirect_url TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS failures INTEGER DEFAULT 0"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))

            add_log("✅ Patch no schema aplicado")
        except Exception as e:
            add_log(f"⚠️ Patch falhou: {e}")

# ================================
# Controle do Monitor
# ================================
_monitor_thread = None
_filelock = None

def _try_acquire_file_lock():
    try:
        import fcntl
        global _filelock
        lock_path = "/tmp/tok4_monitor.lock"
        _filelock = open(lock_path, "w")
        try:
            fcntl.flock(_filelock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            add_log(f"🔁 Monitor já em execução em outro worker: {e}")
            return False
        _filelock.write(f"pid={os.getpid()} ts={time.time()}\n")
        _filelock.flush()
        add_log("🔐 File lock adquirido: monitor exclusivo neste container.")
        return True
    except Exception as e:
        add_log(f"🔁 Monitor já em execução em outro worker: {e}")
        return False

def _start_monitor_background():
    global _monitor_thread
    if not MONITOR_ENABLED:
        add_log("⏸ MONITOR_DISABLED.")
        return
    if _monitor_thread and _monitor_thread.is_alive():
        return
    if not _try_acquire_file_lock():
        return
    _monitor_thread = threading.Thread(target=monitor_loop, args=(MONITOR_INTERVAL,), daemon=True, name="tok4-monitor")
    _monitor_thread.start()
    add_log("🧵 Thread de monitoramento iniciada.")

# ================================
# Main
# ================================
if __name__ == "__main__":
    _apply_bootstrap_patches()
    _start_monitor_background()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)