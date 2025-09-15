import os
import time
import requests
import logging
from twilio.rest import Client
from sqlalchemy.exc import SQLAlchemyError

from utils import check_link
from models import db, Bot

# === Configuração de logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# === Variáveis de ambiente (Railway → Variables) ===
TYPEBOT_API = os.getenv("TYPEBOT_API")   # Ex: https://typebot.io/api/v1
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID")  # ID do flow no Typebot
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")  # Número WhatsApp do Twilio
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")  # Seu número WhatsApp com prefixo +55

# === Setup Twilio ===
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)


# ---------------- Funções auxiliares ----------------
def send_whatsapp(msg: str):
    """Envia mensagem para o WhatsApp via Twilio"""
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        logging.info("📲 Mensagem enviada ao WhatsApp")
    except Exception as e:
        logging.error(f"❌ Erro ao enviar WhatsApp: {e}")


def carregar_links_typebot():
    """Busca os links do flow no Typebot (para debug/validação externa)"""
    try:
        url = f"{TYPEBOT_API}/bots/{TYPEBOT_FLOW_ID}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        links = [
            block["content"]["url"]
            for block in data.get("blocks", [])
            if block.get("type") == "redirect"
        ]
        return links

    except Exception as e:
        logging.error(f"❌ Erro ao carregar links do Typebot: {e}")
        send_whatsapp(f"⚠️ Erro ao carregar links do Typebot: {e}")
        return []


def get_bots_from_db():
    """Carrega os bots do banco e separa por status"""
    try:
        ativos = Bot.query.filter_by(status="ativo").all()
        reserva = Bot.query.filter_by(status="reserva").all()
        return ativos, reserva
    except SQLAlchemyError as e:
        logging.error(f"❌ Erro ao consultar banco: {e}")
        return [], []


# ---------------- Loop principal ----------------
def monitor_loop(interval: int = 60):
    """Loop de monitoramento dos bots"""
    logging.info("🔄 Carregando bots do banco...")
    ativos, reserva = get_bots_from_db()

    if not ativos and reserva:
        # Ativa os dois primeiros da reserva
        for bot in reserva[:2]:
            bot.mark_active()
        try:
            db.session.commit()
            ativos, reserva = get_bots_from_db()
        except SQLAlchemyError as e:
            db.session.rollback()
            logging.error(f"❌ Erro ao ativar bots iniciais: {e}")

    logging.info(f"✅ Monitoramento iniciado | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
    send_whatsapp("🚀 Monitor do Typebot iniciado com sucesso!")

    while True:
        for bot in list(ativos):
            logging.info(f"🔎 Checando bot {bot.name} → {bot.redirect_url}")

            if check_link(bot.redirect_url):
                bot.reset_failures()
                try:
                    db.session.commit()
                except SQLAlchemyError as e:
                    db.session.rollback()
                    logging.error(f"❌ Erro ao salvar status OK no banco: {e}")
                continue

            # Se não passou no check
            bot.increment_failure()
            logging.warning(f"⚠️ Falha detectada no bot {bot.name} ({bot.failures}x)")
            send_whatsapp(f"⚠️ Bot com problema!\n\n"
                          f"Nome: {bot.name}\n"
                          f"URL: {bot.redirect_url}\n"
                          f"Falhas: {bot.failures}")

            if bot.failures >= 3:  # Exemplo: troca só após 3 falhas
                bot.mark_reserve()
                try:
                    db.session.commit()
                except SQLAlchemyError as e:
                    db.session.rollback()
                    logging.error(f"❌ Erro ao atualizar bot {bot.name} como reserva: {e}")
                if bot in ativos:
                    ativos.remove(bot)

                if reserva:
                    novo = reserva.pop(0)
                    novo.mark_active()
                    try:
                        db.session.commit()
                        ativos.append(novo)
                        send_whatsapp(f"🔄 Substituído automaticamente!\n\n"
                                      f"Novo Ativo: {novo.name}\nURL: {novo.redirect_url}")
                    except SQLAlchemyError as e:
                        db.session.rollback()
                        logging.error(f"❌ Erro ao ativar novo bot {novo.name}: {e}")
                else:
                    send_whatsapp("❌ Não há mais bots na reserva!")

        time.sleep(interval)


# ---------------- EntryPoint ----------------
if __name__ == "__main__":
    monitor_loop()