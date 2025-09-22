import os
import time
import logging
import requests
from datetime import datetime
from twilio.rest import Client
from sqlalchemy.exc import SQLAlchemyError

from models import db, Bot

# ================================
# Configuração de logging
# ================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("utils")

# ================================
# Variáveis de ambiente
# ================================
TYPEBOT_API = os.getenv("TYPEBOT_API")       # Ex: https://typebot.io/api/v1
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID")

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")      # Número WhatsApp Twilio
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")  # Número admin no formato +55...

MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")

# ================================
# Setup Twilio
# ================================
twilio_client = None
if TWILIO_SID and TWILIO_AUTH:
    try:
        twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
    except Exception as e:
        logger.error(f"❌ Erro ao configurar Twilio: {e}")

# ================================
# Funções auxiliares
# ================================
def send_whatsapp(msg: str):
    """Envia mensagem formatada via WhatsApp (Twilio)."""
    if not twilio_client:
        logger.warning("⚠️ Twilio não configurado.")
        return
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        logger.info("📲 Mensagem enviada ao WhatsApp")
    except Exception as e:
        logger.error(f"❌ Erro ao enviar WhatsApp: {e}")


def carregar_links_typebot():
    """Busca links do flow do Typebot para debug/validação externa."""
    if not TYPEBOT_API or not TYPEBOT_FLOW_ID:
        logger.warning("⚠️ TYPEBOT_API ou TYPEBOT_FLOW_ID não configurados.")
        return []

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
        logger.error(f"❌ Erro ao carregar links do Typebot: {e}")
        send_whatsapp(f"⚠️ Erro ao carregar links do Typebot: {e}")
        return []


# ================================
# Funções de checagem (Token / URL / Probe)
# ================================
def check_token(token: str):
    """
    Verifica se o token do bot é válido usando a API Telegram.
    Retorna (ok: bool, reason: str, username: str|None).
    """
    if not token:
        return False, "Token vazio", None
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                return True, "Token válido", data["result"]["username"]
            return False, "Token inválido (API retornou erro)", None
        return False, f"Erro HTTP {r.status_code}", None
    except Exception as e:
        return False, f"Exceção: {e}", None


def check_link(url: str):
    """
    Testa se o redirect_url do bot está online.
    Retorna (ok: bool, reason: str).
    """
    if not url:
        return False, "URL não definida"
    try:
        r = requests.get(url, timeout=10)
        if 200 <= r.status_code < 400:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"Exceção: {e}"


def check_probe(token: str, chat_id: str):
    """
    Envia uma mensagem de teste ao grupo de monitoramento.
    Retorna (ok: bool, reason: str).
    """
    if not token or not chat_id:
        return None, "Probe desativado (token ou chat_id ausente)"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": "🔎 Probe check"}, timeout=10)
        if r.status_code == 200:
            return True, "Mensagem enviada"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"Exceção: {e}"


# ================================
# Diagnóstico e Banco
# ================================
def diagnosticar_bot(bot: Bot) -> dict:
    """
    Executa todas as checagens de um bot (token, url, probe).
    Atualiza os campos de diagnóstico no banco.
    Retorna dict com diagnóstico detalhado.
    """
    diag = {}

    # Token
    token_ok, token_reason, username = check_token(bot.token or "")
    diag["token_ok"] = token_ok
    diag["token_reason"] = token_reason
    diag["username"] = username

    # URL
    url_ok, url_reason = check_link(bot.redirect_url or "")
    diag["url_ok"] = url_ok
    diag["url_reason"] = url_reason

    # Probe
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)
    diag["probe_ok"] = probe_ok
    diag["probe_reason"] = probe_reason

    # Decisão final
    diag["decision_ok"] = token_ok and url_ok and (probe_ok or probe_ok is None)

    # Sincroniza no banco
    try:
        bot.apply_diag({
            "token_ok": token_ok,
            "url_ok": url_ok,
            "webhook_ok": probe_ok,   # proxy do probe
            "reason": f"T:{token_reason} | U:{url_reason} | P:{probe_reason}"
        })
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"❌ Erro ao salvar diagnóstico de {bot.name}: {e}")

    return diag


# ================================
# Log de eventos centralizado
# ================================
def log_event(bot: Bot, event: str, level: str = "info"):
    """
    Loga eventos do bot com timestamp, envia para WhatsApp se crítico.
    """
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {event}"

    if level == "error":
        logger.error(line)
        send_whatsapp(f"❌ {event}")
    elif level == "warn":
        logger.warning(line)
        send_whatsapp(f"⚠️ {event}")
    else:
        logger.info(line)