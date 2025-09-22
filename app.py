# ================================
# app.py (Monitor Avançado + Dashboard Sync + Twilio Alerts)
# ================================
import os
import time
import logging
import threading
from datetime import datetime
import requests
from flask import Flask, render_template, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from twilio.rest import Client

from utils import check_link, check_token, check_probe, log_event
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
metrics = {
    "checks_total": 0,
    "failures_total": 0,
    "switches_total": 0,
    "last_check_ts": None
}

# ================================
# Funções auxiliares
# ================================
def add_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    monitor_logs.append(line)
    if len(monitor_logs) > MAX_LOGS:
        monitor_logs.pop(0)
    logger.info(msg)


def send_whatsapp(title: str, details: str):
    """Envia mensagem formatada via WhatsApp (Twilio)"""
    if not twilio_client:
        add_log("⚠️ Twilio não configurado.")
        return
    msg = f"📡 *TOK4 Monitor*\n\n" \
          f"🔔 {title}\n\n" \
          f"{details}\n\n" \
          f"⏰ {time.strftime('%d/%m %H:%M:%S')}"
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        add_log("📲 WhatsApp enviado")
    except Exception as e:
        add_log(f"❌ Erro ao enviar WhatsApp: {e}")


def get_bots_from_db():
    """Carrega bots ativos e reservas"""
    try:
        ativos = Bot.query.filter_by(status="ativo").all()
        reserva = Bot.query.filter_by(status="reserva").all()
        return ativos, reserva
    except SQLAlchemyError as e:
        add_log(f"❌ Erro ao consultar banco: {e}")
        return [], []


def diagnosticar_bot(bot):
    """Executa checagens do bot (Token, URL, Probe)"""
    diag = {}
    token_ok, token_reason, username = check_token(bot.token or "")
    url_ok, url_reason = check_link(bot.redirect_url or "")
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)

    diag["token_ok"] = token_ok
    diag["url_ok"] = url_ok
    diag["probe_ok"] = probe_ok
    diag["decision_ok"] = token_ok and url_ok and (probe_ok or probe_ok is None)
    diag["reasons"] = {
        "token": token_reason,
        "url": url_reason,
        "probe": probe_reason
    }
    return diag

# ================================
# Loop de monitoramento
# ================================
def monitor_loop(interval: int = MONITOR_INTERVAL):
    add_log("🔄 Carregando bots do banco...")
    ativos, reserva = get_bots_from_db()

    # Se não houver ativos → ativa 2 da reserva
    if not ativos and reserva:
        for bot in reserva[:2]:
            bot.mark_active()
        try:
            db.session.commit()
            ativos, reserva = get_bots_from_db()
        except SQLAlchemyError as e:
            db.session.rollback()
            add_log(f"❌ Erro ao ativar bots iniciais: {e}")

    add_log(f"✅ Monitor iniciado | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
    send_whatsapp("🚀 Monitor Iniciado", f"Ativos: {len(ativos)} | Reservas: {len(reserva)}")

    while True:
        for bot in list(ativos):
            add_log(f"🔎 Checando {bot.name} → {bot.redirect_url}")
            metrics["checks_total"] += 1
            metrics["last_check_ts"] = int(time.time())

            diag = diagnosticar_bot(bot)
            bot._diag = diag  # para expor no JSON

            if diag["decision_ok"]:
                bot.reset_failures()
                bot.last_ok = datetime.utcnow()
                try:
                    db.session.commit()
                except SQLAlchemyError as e:
                    db.session.rollback()
                    add_log(f"❌ Erro ao salvar status OK no banco: {e}")
                continue

            # Falha
            bot.increment_failure()
            metrics["failures_total"] += 1
            add_log(f"⚠️ Falha em {bot.name} ({bot.failures}/{FAIL_THRESHOLD})")

            send_whatsapp(
                "⚠️ Bot com problema",
                f"Nome: {bot.name}\n"
                f"URL: {bot.redirect_url}\n"
                f"Falhas: {bot.failures}\n\n"
                f"🔑 Token: {diag['reasons']['token']}\n"
                f"🌍 URL: {diag['reasons']['url']}\n"
                f"📡 Probe: {diag['reasons']['probe']}"
            )

            # Substituição se passar do threshold
            if bot.failures >= FAIL_THRESHOLD:
                bot.mark_reserve()
                try:
                    db.session.commit()
                except SQLAlchemyError:
                    db.session.rollback()
                if bot in ativos:
                    ativos.remove(bot)

                if reserva:
                    novo = reserva.pop(0)
                    novo.mark_active()
                    try:
                        db.session.commit()
                        ativos.append(novo)
                        metrics["switches_total"] += 1
                        send_whatsapp(
                            "🔄 Substituição Automática",
                            f"❌ {bot.name} caiu\n➡️ ✅ {novo.name} ativo\n"
                            f"Novo URL: {novo.redirect_url}"
                        )
                    except SQLAlchemyError as e:
                        db.session.rollback()
                        add_log(f"❌ Erro ao ativar novo bot {novo.name}: {e}")
                else:
                    send_whatsapp("❌ Falha Crítica", "Não há mais bots na reserva!")

        time.sleep(interval)

# ================================
# Rotas para Dashboard
# ================================
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/bots", methods=["GET"])
def api_bots():
    try:
        bots = Bot.query.order_by(Bot.id).all()
        return jsonify({
            "bots": [b.to_dict(include_diag=True) for b in bots],
            "logs": monitor_logs,
            "metrics": metrics,
            "last_action": metrics.get("last_check_ts")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots", methods=["POST"])
def add_bot():
    data = request.json
    if not data or not data.get("name") or not data.get("url") or not data.get("token"):
        return jsonify({"error": "Campos obrigatórios: name, url, token"}), 400
    try:
        bot = Bot(name=data["name"], redirect_url=data["url"], token=data["token"], status="reserva")
        db.session.add(bot)
        db.session.commit()
        add_log(f"✅ Novo bot adicionado: {bot.name}")
        return jsonify(bot.to_dict()), 201
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def update_bot(bot_id):
    data = request.json
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        bot.name = data.get("name", bot.name)
        bot.redirect_url = data.get("url", bot.redirect_url)
        bot.token = data.get("token", bot.token)
        db.session.commit()
        add_log(f"✏️ Bot atualizado: {bot.name}")
        return jsonify(bot.to_dict())
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        db.session.delete(bot)
        db.session.commit()
        add_log(f"🗑 Bot removido: {bot.name}")
        return jsonify({"message": "Bot deletado"})
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>/force_swap", methods=["POST"])
def force_swap(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404

        bot.status = "reserva"
        db.session.commit()

        novo = Bot.query.filter_by(status="reserva").first()
        if novo:
            novo.status = "ativo"
            db.session.commit()
            metrics["switches_total"] += 1
            add_log(f"🔄 Swap forçado: {bot.name} ➝ {novo.name}")
            return jsonify({"old": bot.to_dict(), "new": novo.to_dict()})
        else:
            return jsonify({"error": "Não há bots em reserva"}), 400
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# ================================
# Bootstrap
# ================================
with app.app_context():
    try:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL"))
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS failures INTEGER DEFAULT 0"))
        add_log("✅ Patch no schema aplicado")
    except Exception as e:
        add_log(f"⚠️ Patch falhou: {e}")

# Start monitor em thread separada
threading.Thread(target=monitor_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)