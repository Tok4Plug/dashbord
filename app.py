# ================================
# app.py (versão avançada, robusta)
# ================================
import os
import sys
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
from sqlalchemy import text

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

# ---------- Parâmetros do Monitor ----------
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))   # segundos entre varreduras
FAIL_THRESHOLD   = int(os.getenv("FAIL_THRESHOLD", "3"))      # falhas seguidas p/ disparar swap
CHECK_TIMEOUT    = float(os.getenv("CHECK_TIMEOUT", "7.0"))   # timeout request
MAX_LOGS         = int(os.getenv("MAX_LOGS", "300"))          # histórico de logs
MAX_WORKERS      = int(os.getenv("MONITOR_MAX_WORKERS", "8")) # threads simultâneas
START_MONITOR    = os.getenv("START_MONITOR", "1")            # controla se o monitor sobe

# Desativa monitor quando rodando via CLI do Flask (migrations/commands)
if (
    os.getenv("FLASK_RUN_FROM_CLI") == "true" or
    "flask" in (sys.argv[0] if sys.argv else "").lower()
):
    START_MONITOR = "0"

# ---------- Logs de monitor ----------
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

# ---------- Sessão requests com retry ----------
def make_requests_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=False,  # retry em qualquer método
        raise_on_status=False,
    )
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
            logger.exception("Erro Twilio")

    # CallMeBot (backup)
    if CALLMEBOT_KEY and recipients:
        try:
            for r in recipients:
                url = (
                    "https://api.callmebot.com/whatsapp.php?"
                    f"phone={urllib.parse.quote_plus(r)}&text={urllib.parse.quote_plus(text_msg)}&apikey={urllib.parse.quote_plus(CALLMEBOT_KEY)}"
                )
                requests_session.get(url, timeout=10)
            add_log("Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"Erro CallMeBot: {e}")
            logger.exception("Erro CallMeBot")

    add_log("Nenhuma integração WhatsApp configurada ou sem destinatário.")
    return False

# ---------- Health checks ----------
def safe_check_token(token: str) -> bool:
    """
    Checagem PRIORITÁRIA: o token do bot decide o estado.
    Se o token responder ok (HTTP 200 + body ok=True), consideramos o bot saudável.
    """
    if not token:
        return False
    try:
        r = requests_session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT)
        if r.status_code == 200:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return bool(j.get("ok"))
    except Exception:
        pass
    return False

def safe_check_link(url: str, retries: int = 1) -> bool:
    """
    Checagem auxiliar (NÃO decide sozinha): só para log/bônus.
    """
    if not url:
        return False
    try:
        r = requests_session.head(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        if 200 <= r.status_code < 400:
            return True
    except Exception:
        pass
    # fallback com GET simples se der falha no HEAD
    try:
        r2 = requests_session.get(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        return 200 <= r2.status_code < 400
    except Exception:
        return False

# ---------- Swap ----------
bot_locks = {}  # evita swaps simultâneos do mesmo bot

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

                # marca o que caiu como reserva e zera falhas
                fb.status = "reserva"
                fb.failures = 0

                # escolhe o primeiro reserva diferente do que caiu
                replacement = (
                    session.query(Bot)
                    .filter(Bot.status == "reserva", Bot.id != fb.id)
                    .order_by(Bot.updated_at.asc(), Bot.id.asc())
                    .first()
                )

                if not replacement:
                    send_whatsapp_message_text(None, f"❌ {fb.name} caiu e não há reservas!")
                    metrics["switch_errors_total"] += 1
                    return

                replacement.status = "ativo"
                replacement.failures = 0
                replacement.last_ok = datetime.utcnow()

            metrics["switches_total"] += 1
            msg = (f"🔁 Substituição automática:\n"
                   f"❌ Caiu: {fb.name}\n"
                   f"✅ Substituído por: {replacement.name}")
            send_whatsapp_message_text(None, msg)
            add_log(msg.replace("\n", " | "))
    finally:
        lock.release()

# ---------- Auto-patch de Esquema (idempotente) ----------
PATCH_SQL = """
-- Colunas
ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok    TIMESTAMP NULL;
ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();

-- Backfill (no-op se já existir)
UPDATE bots
SET created_at = COALESCE(created_at, NOW()),
    updated_at = COALESCE(updated_at, NOW());

-- Índices
CREATE INDEX IF NOT EXISTS ix_bots_last_ok     ON bots(last_ok);
CREATE INDEX IF NOT EXISTS ix_bots_created_at  ON bots(created_at);
CREATE INDEX IF NOT EXISTS ix_bots_updated_at  ON bots(updated_at);
CREATE INDEX IF NOT EXISTS idx_status_failures ON bots(status, failures);

-- Unique por redirect_url (via índice único, não precisa de constraint nomeada)
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_redirect_url_idx ON bots(redirect_url);
"""

def bootstrap_schema():
    """Garante que as colunas/índices existam mesmo se migrations falharam."""
    try:
        with app.app_context():
            with db.engine.begin() as conn:
                conn.execute(text(PATCH_SQL))
        add_log("✅ Auto-patch de esquema aplicado (idempotente).")
    except Exception as e:
        # Não derruba a app por causa disso; apenas registra
        add_log(f"⚠️ Falha ao aplicar auto-patch: {e}")
        logger.exception("bootstrap_schema error")

# ---------- Monitor ----------
def check_and_maybe_swap(bot_id: int):
    with app.app_context():
        b = db.session.get(Bot, bot_id)
        if not b:
            return

        # Checagem principal: TOKEN decide
        ok_token = safe_check_token(b.token) if b.token else False
        # Checagem auxiliar só para log
        ok_url = safe_check_link(b.redirect_url) if b.redirect_url else False

        ok = ok_token  # <- decisão final baseada 100% no token

        metrics["checks_total"] += 1
        add_log(
            f"Check {b.name}: TOKEN={'OK' if ok_token else 'FAIL'} | "
            f"URL={'OK' if ok_url else 'FAIL'} | RESULT={'✅' if ok else '❌'}"
        )

        with db.session.begin():
            bot = db.session.get(Bot, bot_id)
            if ok:
                bot.failures = 0
                bot.last_ok = datetime.utcnow()
                bot.updated_at = datetime.utcnow()
            else:
                bot.failures = (bot.failures or 0) + 1
                bot.updated_at = datetime.utcnow()
                metrics["failures_total"] += 1
                if bot.failures >= FAIL_THRESHOLD:
                    add_log(f"{bot.name} atingiu {bot.failures} falhas consecutivas; disparando swap…")
                    threading.Thread(target=swap_bot, args=(bot.id,), daemon=True).start()

def monitor_loop():
    send_whatsapp_message_text(None, "🚀 Monitor iniciado.")
    add_log("Monitor iniciado.")

    while True:
        start_ts = time.time()
        metrics["last_check_ts"] = int(start_ts)

        try:
            with app.app_context():
                bots = db.session.query(Bot).order_by(Bot.updated_at.asc(), Bot.id.asc()).all()
                metrics["bots_active"]  = sum(1 for b in bots if b.status == "ativo")
                metrics["bots_reserve"] = sum(1 for b in bots if b.status == "reserva")

                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    for b in bots:
                        ex.submit(check_and_maybe_swap, b.id)

        except Exception as e:
            add_log(f"Erro monitor: {e}")
            logger.exception("Erro monitor_loop")

        # espera até completar o intervalo
        delta = time.time() - start_ts
        time.sleep(max(0, MONITOR_INTERVAL - delta))

def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True, name="bot-monitor")
    t.start()
    add_log("Thread monitor iniciada.")

# ---------- Rotas ----------
@app.route("/")
def index():
    bots = Bot.query.order_by(Bot.status.desc(), Bot.updated_at.desc()).all()
    return render_template("dashboard.html", bots=bots, logs=monitor_logs, metrics=metrics)

@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    return jsonify({"bots": [b.to_dict() for b in Bot.query.order_by(Bot.id).all()]})

@app.route("/api/bots", methods=["POST"])
def api_create_bot():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip() or None
    redirect_url = (data.get("redirect_url") or "").strip()

    if not name or not redirect_url:
        return jsonify({"error": "name e redirect_url são obrigatórios"}), 400

    # valida token se informado
    if token and not safe_check_token(token):
        return jsonify({"error": "token inválido (Telegram getMe falhou)"}), 400

    with db.session.begin():
        bot = Bot(
            name=name,
            token=token,
            redirect_url=redirect_url,
            status=data.get("status") or "reserva",
            failures=0,
            last_ok=datetime.utcnow() if token else None,
        )
        db.session.add(bot)

    add_log(f"Bot criado: {name} (status={bot.status})")
    return jsonify({"bot": bot.to_dict()}), 201

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

# ---------- Inicialização controlada ----------
with app.app_context():
    bootstrap_schema()

if START_MONITOR == "1":
    start_monitor_thread()
else:
    add_log("Monitor desativado (START_MONITOR != '1' ou execução via CLI).")

# Execução local (dev)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=os.getenv("DEBUG", "True") == "True")