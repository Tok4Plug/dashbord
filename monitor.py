import os
import time
import requests
from utils import check_link
from twilio.rest import Client

# === Variáveis de ambiente (Railway → Variables) ===
TYPEBOT_API = os.getenv("TYPEBOT_API")   # Ex: https://typebot.io/api/v1
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID")  # ID do flow no Typebot
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")  # Número WhatsApp do Twilio
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")  # Seu número WhatsApp com prefixo +55

# === Setup Twilio ===
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

# Listas de links
ativos = []
reserva = []

def send_whatsapp(msg: str):
    """Envia mensagem para o WhatsApp via Twilio"""
    twilio_client.messages.create(
        body=msg,
        from_=f"whatsapp:{TWILIO_FROM}",
        to=f"whatsapp:{ADMIN_WHATSAPP}"
    )

def carregar_links():
    """Busca os links do flow no Typebot"""
    url = f"{TYPEBOT_API}/bots/{TYPEBOT_FLOW_ID}"
    r = requests.get(url)
    data = r.json()

    links = []
    for block in data.get("blocks", []):
        if block.get("type") == "redirect":
            links.append(block["content"]["url"])

    return links

def monitor_loop():
    global ativos, reserva

    print("🔄 Carregando links do flow...")
    links = carregar_links()

    if not ativos:
        # Primeiros links entram como ativos
        ativos = links[:2]   # Exemplo: 2 ativos
        reserva = links[2:]  # O resto fica como reserva

    print("✅ Monitoramento iniciado")
    send_whatsapp("🚀 Monitor do Typebot iniciado com sucesso!")

    while True:
        for link in list(ativos):
            if not check_link(link):
                send_whatsapp(f"⚠️ Link caiu: {link}")
                ativos.remove(link)

                if reserva:
                    novo = reserva.pop(0)
                    ativos.append(novo)
                    send_whatsapp(f"🔄 Substituído por: {novo}")
                else:
                    send_whatsapp("❌ Não há mais links na reserva!")

        time.sleep(60)  # checa a cada 60s

if __name__ == "__main__":
    monitor_loop()