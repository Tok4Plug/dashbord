import os
import requests

# Função para checar se um link está ativo
def check_link(url: str) -> bool:
    try:
        r = requests.get(url, timeout=10)
        return r.status_code == 200
    except:
        return False