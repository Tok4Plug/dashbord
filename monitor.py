import os
import time
import requests
from twilio.rest import Client
from utils import check_link
from models import db, Bot

# === Variáveis de ambiente (Railway → Variables) ===
TYPEBOT_API = os.getenv("TYPEBOT_API")   # Ex: https://typebot.io/api/v1
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID")  # ID do flow no Typebot
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")  # Número WhatsApp do Twilio
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")  # Seu número WhatsApp com prefixo +55

# === Setup Twilio ===
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)


def send_whatsapp(msg: str):
    """Envia mensagem para o WhatsApp via Twilio"""
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp: {e}")


def carregar_links_typebot():
    """Busca os links do flow no Typebot"""
    try:
        url = f"{TYPEBOT_API}/bots/{TYPEBOT_FLOW_ID}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        links = []
        for block in data.get("blocks", []):
            if block.get("type") == "redirect":
                links.append(block["content"]["url"])
        return links

    except Exception as e:
        print(f"❌ Erro ao carregar links do Typebot: {e}")
        send_whatsapp(f"⚠️ Erro ao carregar links do Typebot: {e}")
        return []


def get_bots_from_db():
    """Carrega os bots do banco"""
    ativos = Bot.query.filter_by(status="ativo").all()
    reserva = Bot.query.filter_by(status="reserva").all()
    return ativos, reserva


def monitor_loop():
    print("🔄 Carregando bots do banco...")
    ativos, reserva = get_bots_from_db()

    if not ativos and reserva:
        # Ativa os dois primeiros, se não houver ativos
        for bot in reserva[:2]:
            bot.status = "ativo"
        db.session.commit()
        ativos, reserva = get_bots_from_db()

    print(f"✅ Monitoramento iniciado | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
    send_whatsapp("🚀 Monitor do Typebot iniciado com sucesso!")

    while True:
        for bot in list(ativos):
            print(f"🔎 Checando bot {bot.name} → {bot.redirect_url}")
            if not check_link(bot.redirect_url):
                bot.failures += 1
                db.session.commit()

                send_whatsapp(f"⚠️ Bot caiu!\n\n"
                              f"Nome: {bot.name}\n"
                              f"URL: {bot.redirect_url}\n"
                              f"Falhas: {bot.failures}")

                # Desativa o bot
                bot.status = "inativo"
                db.session.commit()
                ativos.remove(bot)

                if reserva:
                    novo = reserva.pop(0)
                    novo.status = "ativo"
                    db.session.commit()
                    ativos.append(novo)
                    send_whatsapp(f"🔄 Substituído automaticamente!\n\n"
                                  f"Novo Ativo: {novo.name}\nURL: {novo.redirect_url}")
                else:
                    send_whatsapp("❌ Não há mais bots na reserva!")

        time.sleep(60)  # checa a cada 60s


if __name__ == "__main__":
    monitor_loop()