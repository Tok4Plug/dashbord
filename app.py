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
    """
    Lê o payload aceitando JSON e/ou form-data.
    """
    data = request.get_json(silent=True) or {}
    if not data:
        data = request.form.to_dict() or {}
    for k in list(data.keys()):
        if isinstance(data[k], str):
            data[k] = data[k].strip()
    return data

# ================================
# Rotas Dashboard/API (CRUD completo)
# ================================
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/api/bots", methods=["GET"])
def api_bots():
    try:
        bots = Bot.query.order_by(Bot.id).all()
        payload = []
        for b in bots:
            d = b.to_dict(with_meta=True)
            cached = diag_cache.get(b.id) or {}
            d["_diag"] = cached.get("diag")
            d["_diag_ts"] = cached.get("when")
            payload.append(d)
        return jsonify({"bots": payload, "logs": monitor_logs, "metrics": metrics})
    except Exception as e:
        _rollback_if_failed_tx(e)
        add_log(f"❌ /api/bots erro: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots", methods=["POST"])
def create_bot():
    try:
        data = _get_payload()
        add_log(f"📥 Recebido no POST /api/bots: {data}")

        name = data.get("name")
        token = data.get("token")
        redirect_url = data.get("redirect_url") or f"https://t.me/{name}"  # fallback
        status = data.get("status", "ativo") or "ativo"

        if not name or not token:
            return jsonify({"error": "name e token são obrigatórios"}), 400

        new_bot = Bot(
            name=name,
            token=token,
            redirect_url=redirect_url,
            status=status
        )
        db.session.add(new_bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao salvar. Verifique os logs."}), 500

        add_log(f"➕ Bot {new_bot.name} criado.")
        send_whatsapp("➕ Novo Bot", f"Nome: {new_bot.name}\nURL: {new_bot.redirect_url}")
        return jsonify(new_bot.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def update_bot(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404

        data = _get_payload()
        add_log(f"📥 Recebido no PUT /api/bots/{bot_id}: {data}")

        if "redirect_url" in data and not data.get("redirect_url"):
            return jsonify({"error": "redirect_url não pode ser vazio"}), 400

        bot.name = data.get("name", bot.name)
        bot.token = data.get("token", bot.token)
        bot.redirect_url = data.get("redirect_url", bot.redirect_url)
        bot.status = data.get("status", bot.status)
        if not safe_commit():
            return jsonify({"error": "Falha ao salvar. Verifique os logs."}), 500

        add_log(f"✏️ Bot {bot.name} atualizado.")
        send_whatsapp("✏️ Bot Atualizado", f"Nome: {bot.name}\nURL: {bot.redirect_url}")
        return jsonify(bot.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        db.session.delete(bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao excluir. Verifique os logs."}), 500

        add_log(f"🗑️ Bot {bot.name} excluído.")
        send_whatsapp("🗑️ Bot Excluído", f"Nome: {bot.name}")
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/webhookinfo/<int:bot_id>", methods=["GET"])
def api_webhookinfo(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        ok, reason, details = check_webhook(bot.token or "")
        return jsonify({"ok": ok, "reason": reason, "details": details})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================
# Bootstrap
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

_apply_bootstrap_patches()

# ================================
# Monitor
# ================================
_monitor_thread = None
_filelock = None

def _try_acquire_file_lock():
    try:
        import fcntl
        global _filelock
        lock_path = "/tmp/tok4_monitor.lock"
        _filelock = open(lock_path, "w")
        fcntl.flock(_filelock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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

_start_monitor_background()

# ================================
# Main
# ================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)