import os
import threading
import time
import requests
import urllib.parse
from flask import Flask, render_template, request, jsonify
from sqlalchemy import text
from models import db, Bot
from utils import check_link, check_token

# ---------- Config ----------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "change_me_random")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))  # segundos

db.init_app(app)

# logs em memória (mostrados no painel)
monitor_logs = []


def add_log(s: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    monitor_logs.append(f"[{ts}] {s}")
    if len(monitor_logs) > 200:
        monitor_logs.pop(0)


# ---------- WhatsApp helpers (Twilio ou CallMeBot) ----------
def send_whatsapp_message_text(to_number: str, text: str):
    TWILIO_SID = os.getenv("TWILIO_SID")
    TWILIO_AUTH = os.getenv("TWILIO_AUTH")
    TWILIO_FROM = os.getenv("TWILIO_FROM")
    CALLMEBOT_KEY = os.getenv("CALLMEBOT_KEY")

    if TWILIO_SID and TWILIO_AUTH and TWILIO_FROM:
        try:
            from twilio.rest import Client

            client = Client(TWILIO_SID, TWILIO_AUTH)
            client.messages.create(
                body=text,
                from_=f"whatsapp:{TWILIO_FROM}",
                to=f"whatsapp:{os.getenv('ADMIN_WHATSAPP')}"
            )
            add_log("Mensagem WhatsApp enviada via Twilio.")
            return True
        except Exception as e:
            add_log(f"Erro Twilio: {e}")
            return False

    if CALLMEBOT_KEY:
        try:
            admin = os.getenv("ADMIN_WHATSAPP")
            url = (
                "https://api.callmebot.com/whatsapp.php?"
                f"phone={urllib.parse.quote_plus(admin)}&text={urllib.parse.quote_plus(text)}&apikey={urllib.parse.quote_plus(CALLMEBOT_KEY)}"
            )
            requests.get(url, timeout=10)
            add_log("Mensagem WhatsApp enviada via CallMeBot.")
            return True
        except Exception as e:
            add_log(f"Erro CallMeBot: {e}")
            return False

    add_log("Nenhuma integração WhatsApp configurada (TWILIO ou CALLMEBOT).")
    return False


# ---------- Swap logic ----------
def swap_bot(failed_bot: Bot):
    """Marca failed como reserva e ativa o primeiro disponível da reserva."""
    with app.app_context():
        add_log(f"Detectado problema no bot '{failed_bot.name}' ({failed_bot.redirect_url}). Iniciando substituição.")
        failed_bot.status = "reserva"
        db.session.commit()

        replacement = Bot.query.filter_by(status="reserva").order_by(Bot.id).first()
        if replacement:
            replacement.status = "ativo"
            db.session.commit()

            ativos_count = Bot.query.filter_by(status="ativo").count()
            reserva_count = Bot.query.filter_by(status="reserva").count()
            msg = (
                f"🔁 Substituição automática:\n"
                f"❌ Caiu: {failed_bot.name}\n"
                f"✅ Substituído por: {replacement.name}\n"
                f"📦 Ativos: {ativos_count} | 🔐 Reserva: {reserva_count}"
            )
            send_whatsapp_message_text(os.getenv("ADMIN_WHATSAPP", ""), msg)
            add_log("Substituição concluída: " + msg.replace("\n", " | "))
        else:
            add_log("Sem bots em reserva para substituir.")
            send_whatsapp_message_text(
                os.getenv("ADMIN_WHATSAPP", ""),
                f"❌ O bot {failed_bot.name} caiu e não há reservas disponíveis!"
            )


# ---------- Monitor loop ----------
def monitor_loop():
    with app.app_context():
        db.create_all()
        add_log("Monitor iniciado.")

        # garante que a coluna failures existe
        try:
            db.session.execute(text("SELECT failures FROM bots LIMIT 1"))
        except Exception:
            db.session.rollback()
            try:
                db.session.execute(text("ALTER TABLE bots ADD COLUMN failures INTEGER DEFAULT 0"))
                db.session.commit()
                add_log("Coluna 'failures' adicionada com sucesso.")
            except Exception as e:
                db.session.rollback()
                add_log(f"Erro ao adicionar coluna failures: {e}")

    send_whatsapp_message_text(os.getenv("ADMIN_WHATSAPP", ""), "🚀 Monitor iniciado.")

    while True:
        try:
            with app.app_context():
                ativos = Bot.query.filter_by(status="ativo").all()
                for bot in ativos:
                    ok_url = check_link(bot.redirect_url, retries=3)
                    ok_token = check_token(bot.token)

                    ok = ok_url and ok_token  # checagem dupla

                    add_log(
                        f"Check {bot.name}: "
                        f"{'OK' if ok else 'FALHOU'} "
                        f"(URL={'✅' if ok_url else '❌'} | TOKEN={'✅' if ok_token else '❌'}) "
                        f"-> {bot.redirect_url}"
                    )

                    if not ok:
                        swap_bot(bot)

        except Exception as e:
            add_log(f"Erro no loop do monitor: {e}")
        time.sleep(MONITOR_INTERVAL)


def start_monitor_thread():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()


# inicia o monitor junto com o servidor web
start_monitor_thread()


# ---------- Rotas ----------
@app.route("/")
def index():
    bots = Bot.query.order_by(Bot.id).all()
    return render_template("dashboard.html", bots=bots, logs=monitor_logs)


@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    bots = [b.to_dict() for b in Bot.query.order_by(Bot.id).all()]
    return jsonify({"bots": bots})


@app.route("/api/bot", methods=["POST"])
def api_add_bot():
    data = request.json or request.form
    name = data.get("name")
    token = data.get("token")
    redirect_url = data.get("redirect_url")
    status = data.get("status", "reserva")
    if not name or not redirect_url:
        return jsonify({"error": "name and redirect_url required"}), 400
    bot = Bot(name=name, token=token, redirect_url=redirect_url, status=status)
    with app.app_context():
        db.session.add(bot)
        db.session.commit()
    add_log(f"Bot adicionado: {name} ({status})")
    return jsonify({"ok": True, "bot": bot.to_dict()})


@app.route("/api/bot/<int:bot_id>", methods=["POST"])
def api_update_bot(bot_id):
    data = request.json or request.form
    bot = Bot.query.get(bot_id)
    if not bot:
        return jsonify({"error": "not found"}), 404
    bot.name = data.get("name", bot.name)
    bot.token = data.get("token", bot.token)
    bot.redirect_url = data.get("redirect_url", bot.redirect_url)
    bot.status = data.get("status", bot.status)
    db.session.commit()
    add_log(f"Bot atualizado: {bot.name}")
    return jsonify({"ok": True, "bot": bot.to_dict()})


@app.route("/api/bot/<int:bot_id>/delete", methods=["POST"])
def api_delete_bot(bot_id):
    bot = Bot.query.get(bot_id)
    if not bot:
        return jsonify({"error": "not found"}), 404
    db.session.delete(bot)
    db.session.commit()
    add_log(f"Bot excluído: {bot.name}")
    return jsonify({"ok": True})


@app.route("/api/force_swap/<int:bot_id>", methods=["POST"])
def api_force_swap(bot_id):
    bot = Bot.query.get(bot_id)
    if not bot:
        return jsonify({"error": "not found"}), 404
    swap_bot(bot)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)