import os
import time
import requests


def log_event(message: str, level: str = "INFO"):
    """
    Log unificado para console (futuro: pode integrar Prometheus ou outro observability stack).
    """
    print(f"[{level}] {time.strftime('%Y-%m-%d %H:%M:%S')} | {message}")


def check_link(url: str, retries: int = 3, backoff: int = 2) -> bool:
    """
    Verifica se o link de redirect está online.
    - Tenta várias vezes (retries) com backoff exponencial.
    - Retorna True se resposta 200, False caso contrário.
    """
    for attempt in range(1, retries + 1):
        try:
            log_event(f"Checando link ({attempt}/{retries}): {url}")
            r = requests.get(url, timeout=10)

            if r.status_code == 200:
                log_event(f"✅ Link OK: {url}")
                return True
            else:
                log_event(f"⚠️ Link respondeu {r.status_code}: {url}", level="WARNING")

        except requests.Timeout:
            log_event(f"⏳ Timeout na tentativa {attempt} para {url}", level="WARNING")
        except Exception as e:
            log_event(f"❌ Erro ao checar {url}: {e}", level="ERROR")

        time.sleep(backoff * attempt)  # backoff exponencial

    log_event(f"❌ Link OFFLINE após {retries} tentativas: {url}", level="ERROR")
    return False


def check_token(token: str, retries: int = 2) -> bool:
    """
    Verifica se o token do bot Telegram é válido.
    - Usa endpoint oficial Telegram getMe.
    - Faz retries em caso de erro de rede.
    """
    if not token:
        log_event("⚠️ Token vazio recebido para validação", level="WARNING")
        return False

    url = f"https://api.telegram.org/bot{token}/getMe"

    for attempt in range(1, retries + 1):
        try:
            log_event(f"Checando token do Telegram ({attempt}/{retries})...")
            r = requests.get(url, timeout=10)

            if r.status_code == 200:
                data = r.json()
                if data.get("ok") and "id" in data.get("result", {}):
                    log_event(f"✅ Token válido. Bot: @{data['result'].get('username', 'desconhecido')}")
                    return True
                else:
                    log_event("⚠️ Token inválido ou resposta inesperada do Telegram", level="WARNING")
                    return False
            else:
                log_event(f"⚠️ Telegram respondeu {r.status_code} ao validar token", level="WARNING")

        except requests.Timeout:
            log_event(f"⏳ Timeout na validação do token (tentativa {attempt})", level="WARNING")
        except Exception as e:
            log_event(f"❌ Erro na validação do token: {e}", level="ERROR")

        time.sleep(1.5 * attempt)

    log_event("❌ Token inválido após múltiplas tentativas", level="ERROR")
    return False