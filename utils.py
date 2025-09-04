import requests
import os

def check_link(url: str, retries: int = 3) -> bool:
    """Verifica se o link de redirect está online"""
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return False

def check_token(token: str) -> bool:
    """Verifica se o token do bot Telegram é válido"""
    if not token:
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception:
        return False