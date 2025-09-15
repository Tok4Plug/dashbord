# --- Processo Web (API Flask) ---
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120

# --- Processo Worker (Monitor de Bots) ---
worker: python monitor.py