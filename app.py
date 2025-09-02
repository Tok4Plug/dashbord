import os
import requests
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, flash
from models import db, Bot
from sqlalchemy.exc import IntegrityError

# Config Flask
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")

# Railway fornece DATABASE_URL já no painel
db_url = os.getenv("DATABASE_URL", "sqlite:///bots.db")
# Correção para compatibilidade com SQLAlchemy + Railway
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# === Função de monitoramento ===
def check_bot(token: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = requests.get(url, timeout=5)
        return r.status_code == 200
    except:
        return False

def monitor_bots():
    with app.app_context():
        while True:
            ativos = Bot.query.filter_by(status="ativo").all()
            for bot in ativos:
                if not check_bot(bot.token):
                    reserva = Bot.query.filter_by(status="reserva").first()
                    if reserva:
                        reserva.status = "ativo"
                        db.session.delete(bot)
                        db.session.commit()
                        print(f"[!] Bot {bot.name} caiu. Substituído por {reserva.name}")
                        # 🔔 Aqui você pode chamar Twilio (WhatsApp) para notificar
            time.sleep(60)  # verifica a cada 1 min

# === Rotas Dashboard ===
@app.route("/")
def dashboard():
    ativos = Bot.query.filter_by(status="ativo").all()
    reserva = Bot.query.filter_by(status="reserva").all()
    return render_template("dashboard.html", ativos=ativos, reserva=reserva)

@app.route("/add", methods=["POST"])
def add_bot():
    name = request.form["name"]
    token = request.form["token"]
    redirect_url = request.form["redirect_url"]
    status = request.form["status"]

    try:
        new_bot = Bot(name=name, token=token, redirect_url=redirect_url, status=status)
        db.session.add(new_bot)
        db.session.commit()
        flash(f"Bot {name} adicionado com sucesso!", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Token já existe!", "danger")

    return redirect(url_for("dashboard"))

@app.route("/delete/<int:bot_id>")
def delete_bot(bot_id):
    bot = Bot.query.get(bot_id)
    if bot:
        db.session.delete(bot)
        db.session.commit()
        flash("Bot removido!", "danger")
    return redirect(url_for("dashboard"))

# === Inicialização ===
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    # Thread para monitorar os bots
    t = threading.Thread(target=monitor_bots, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))