import os
import time
import requests

# ============================
# Configurações globais
# ============================
MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")  # ID do grupo de monitoramento (pode ser negativo)
TELEGRAM_API = "https://api.telegram.org"

# ============================
# Log unificado
# ============================
def log_event(message: str, level: str = "INFO"):
    """
    Log unificado para console (futuro: pode integrar Prometheus ou outro observability stack).
    """
    print(f"[{level}] {time.strftime('%Y-%m-%d %H:%M:%S')} | {message}")


# ============================
# Checagem de Link Redirect
# ============================
def check_link(url: str, retries: int = 3, backoff: int = 2):
    """
    Verifica se o link de redirect está online.
    Retorna (ok: bool, motivo: str)
    """
    if not url:
        return False, "URL vazia"

    for attempt in range(1, retries + 1):
        try:
            log_event(f"Checando link ({attempt}/{retries}): {url}")
            r = requests.get(url, timeout=10)

            if r.status_code == 200:
                return True, "Link OK"
            else:
                log_event(f"⚠️ Link respondeu {r.status_code}: {url}", level="WARNING")
                reason = f"HTTP {r.status_code}"

        except requests.Timeout:
            reason = f"Timeout na tentativa {attempt}"
            log_event(reason, level="WARNING")
        except Exception as e:
            reason = f"Erro {e}"
            log_event(f"❌ Erro ao checar {url}: {e}", level="ERROR")

        time.sleep(backoff * attempt)  # backoff exponencial

    return False, f"Link OFFLINE ({reason})"


# ============================
# Checagem de Token
# ============================
def check_token(token: str, retries: int = 2):
    """
    Verifica se o token do bot Telegram é válido.
    Retorna (ok: bool, motivo: str, username: str|None)
    """
    if not token:
        return False, "Token vazio", None

    url = f"{TELEGRAM_API}/bot{token}/getMe"

    for attempt in range(1, retries + 1):
        try:
            log_event(f"Checando token do Telegram ({attempt}/{retries})...")
            r = requests.get(url, timeout=10)

            if r.status_code == 200:
                data = r.json()
                if data.get("ok") and "id" in data.get("result", {}):
                    username = data["result"].get("username", "desconhecido")
                    return True, f"Token válido (@{username})", username
                else:
                    return False, "Resposta inesperada do Telegram", None
            else:
                reason = f"HTTP {r.status_code}"
                log_event(f"⚠️ Telegram respondeu {reason} ao validar token", level="WARNING")

        except requests.Timeout:
            reason = f"Timeout na validação do token (tentativa {attempt})"
            log_event(reason, level="WARNING")
        except Exception as e:
            reason = f"Erro na validação do token: {e}"
            log_event(reason, level="ERROR")

        time.sleep(1.5 * attempt)

    return False, "Token inválido após múltiplas tentativas", None


# ============================
# Probe ativo (mensagem de teste)
# ============================
def check_probe(token: str, chat_id: str = None):
    """
    Envia uma mensagem de teste para o grupo de monitoramento.
    Retorna (ok: bool, motivo: str)
    """
    if not token:
        return False, "Token vazio"
    if not chat_id:
        return None, "Probe desabilitado (MONITOR_CHAT_ID não definido)"

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "🔍 Probe automático: teste de vida"
    }

    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "Probe OK"
        return False, f"Falha no probe ({r.status_code})"
    except requests.Timeout:
        return False, "Timeout no probe"
    except Exception as e:
        return False, f"Erro probe: {e}"