import os
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
TYPEBOT_API = os.getenv("TYPEBOT_API")
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID")

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")

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
        logger.info("📲 WhatsApp enviado")
    except Exception as e:
        logger.error(f"❌ Erro ao enviar WhatsApp: {e}")


def carregar_links_typebot():
    """Busca links do flow no Typebot para debug/validação externa."""
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
# Funções de checagem (Token / URL / Probe / Webhook)
# ================================
def check_token(token: str):
    """Valida o token do bot via /getMe."""
    if not token:
        return False, "Token vazio", None
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                return True, "Token válido", data["result"]["username"]
            return False, f"Token inválido: {data}", None
        return False, f"Erro HTTP {r.status_code}", None
    except Exception as e:
        return False, f"Exceção: {e}", None


def check_link(url: str):
    """Testa se a redirect_url do bot responde HTTP."""
    if not url:
        return False, "URL não definida"
    try:
        r = requests.get(url, timeout=8)
        if 200 <= r.status_code < 400:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"Exceção: {e}"


def check_probe(token: str, chat_id: str):
    """Faz probe real enviando mensagem no grupo de monitoramento."""
    if not token or not chat_id:
        return None, "Probe desativado (token/chat_id ausente)"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": "🔎 Probe check (TOK4 Monitor)"}
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "Mensagem entregue"
        return False, f"HTTP {r.status_code} / {r.text}"
    except Exception as e:
        return False, f"Exceção: {e}"


def check_webhook(token: str):
    """
    Verifica estado do webhook via /getWebhookInfo.
    Retorna (ok: bool, reason: str, details: dict).
    """
    if not token:
        return False, "Token vazio", {}
    try:
        url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return False, f"Erro HTTP {r.status_code}", {}
        data = r.json()
        if not data.get("ok"):
            return False, "Resposta inválida da API", data
        info = data.get("result", {})

        if info.get("url"):
            if info.get("last_error_date"):
                reason = f"Erro: {info.get('last_error_message')}"
                return False, reason, info
            if info.get("pending_update_count", 0) > 0:
                reason = f"{info.get('pending_update_count')} updates pendentes"
                return False, reason, info
            return True, "Webhook ativo e saudável", info
        else:
            return False, "Nenhum webhook configurado", info
    except Exception as e:
        return False, f"Exceção: {e}", {}

# ================================
# Diagnóstico centralizado
# ================================
def diagnosticar_bot(bot: Bot) -> dict:
    """
    Executa todas as checagens: token, url, probe e webhook.
    Atualiza diagnóstico no banco de dados.
    Retorna dict com status detalhado.
    """
    token_ok, token_reason, username = check_token(bot.token or "")
    url_ok, url_reason = check_link(bot.redirect_url or "")
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)
    webhook_ok, webhook_reason, webhook_details = check_webhook(bot.token)

    # Decisão final mais rigorosa:
    decision_ok = token_ok and webhook_ok and (probe_ok is True or probe_ok is None)

    diag = {
        "token_ok": token_ok,
        "token_reason": token_reason,
        "username": username,
        "url_ok": url_ok,
        "url_reason": url_reason,
        "probe_ok": probe_ok,
        "probe_reason": probe_reason,
        "webhook_ok": webhook_ok,
        "webhook_reason": webhook_reason,
        "webhook_details": webhook_details,
        "decision_ok": decision_ok
    }

    # Sincroniza com o banco
    try:
        bot.apply_diag({
            "token_ok": token_ok,
            "url_ok": url_ok,
            "webhook_ok": webhook_ok,
            "reason": f"T:{token_reason} | U:{url_reason} | P:{probe_reason} | W:{webhook_reason}"
        })
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"❌ Erro ao salvar diagnóstico do {bot.name}: {e}")

    # Log detalhado
    status = "✅ OK" if decision_ok else "❌ FALHA"
    logger.info(f"{status} {bot.name}: "
                f"token={token_ok}, url={url_ok}, probe={probe_ok}, webhook={webhook_ok} "
                f"| R: {token_reason} / {url_reason} / {probe_reason} / {webhook_reason}")

    return diag

# ================================
# Log de eventos centralizado
# ================================
def log_event(bot: Bot, event: str, level: str = "info"):
    """Centraliza logs e envia alerta via WhatsApp se necessário."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {bot.name if bot else 'SYSTEM'}: {event}"
    if level == "error":
        logger.error(line)
        send_whatsapp(f"❌ {event}")
    elif level == "warn":
        logger.warning(line)
        send_whatsapp(f"⚠️ {event}")
    else:
        logger.info(line)