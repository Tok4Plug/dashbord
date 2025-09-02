import threading
import time
import os
from flask import Flask, jsonify, request, render_template, redirect, url_for, flash
from models import db, Bot
import requests

# --- Config Flask ---
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# Banco de dados
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Inicializa DB
db.init_app(app)

# --- Logs em memória ---
monitor_logs = []


def add_log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    monitor_logs.append(f"[{timestamp}] {message}")
    if len(monitor_logs) > 100:
        monitor_logs.pop(0)


# --- Função monitor de bots ---
def monitor_bots_iteration():
    bots = Bot.query.all()
    ativos = [b for b in bots if b.status == "ativo"]
    reservas = [b for b in bots if b.status == "reserva"]

    add_log(f"Monitor: {len(ativos)} ativos / {len(reservas)} em reserva")

    for bot in bots:
        status_msg = f"Bot {bot.name} - Status: {bot.status}"
        add_log(status_msg)

        # Envio Typebot
        TYPEBOT_TOKEN = os.getenv("TYPEBOT_TOKEN")
        BOT_ID = bot.token
        if TYPEBOT_TOKEN:
            url = f"https://api.typebot.io/v1/bots/{BOT_ID}/messages"
            headers = {"Authorization": f"Bearer {TYPEBOT_TOKEN}", "Content-Type": "application/json"}
            data = {"message": status_msg}
            try:
                requests.post(url, json=data, headers=headers)
            except Exception as e:
                add_log(f"Erro Typebot: {e}")

    # WhatsApp report
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER")
    if BOT_TOKEN and WHATSAPP_NUMBER:
        try:
            msg = f"Relatório Monitor:\nAtivos: {len(ativos)}\nReserva: {len(reservas)}"
            whatsapp_send_message(WHATSAPP_NUMBER, msg)
        except Exception as e:
            add_log(f"Erro WhatsApp: {e}")


def whatsapp_send_message(number, message):
    """Envio via API CallMeBot (ajuste se usar outro serviço)"""
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    url = f"https://api.callmebot.com/whatsapp.php?phone={number}&text={message}&apikey={BOT_TOKEN}"
    requests.get(url)


# --- Thread monitor ---
def start_monitor_thread():
    def run():
        with app.app_context():
            db.create_all()
            while True:
                monitor_bots_iteration()
                time.sleep(60)
    t = threading.Thread(target=run, daemon=True)
    t.start()


start_monitor_thread()

# --- Rotas Flask ---
@app.route("/")
def dashboard():
    bots = Bot.query.all()
    return render_template("dashboard.html", bots=bots, logs=monitor_logs)


@app.route("/add_bot", methods=["POST"])
def add_bot():
    data = request.form
    bot = Bot(
        name=data["name"],
        token=data["token"],
        redirect_url=data["redirect_url"],
        status=data.get("status", "ativo")
    )
    db.session.add(bot)
    db.session.commit()
    flash(f"Bot {bot.name} adicionado!", "success")
    return redirect(url_for("dashboard"))


@app.route("/update_bot/<int:bot_id>", methods=["POST"])
def update_bot(bot_id):
    bot = Bot.query.get(bot_id)
    if bot:
        bot.name = request.form.get("name", bot.name)
        bot.token = request.form.get("token", bot.token)
        bot.status = request.form.get("status", bot.status)
        db.session.commit()
        flash(f"Bot {bot.name} atualizado!", "success")
    return redirect(url_for("dashboard"))


@app.route("/delete_bot/<int:bot_id>", methods=["POST"])
def delete_bot(bot_id):
    bot = Bot.query.get(bot_id)
    if bot:
        db.session.delete(bot)
        db.session.commit()
        flash(f"Bot {bot.name} removido!", "success")
    return redirect(url_for("dashboard"))