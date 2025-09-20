# ================================
# Procfile robusto para Railway
# ================================

# --- Release Phase ---
# Executado ANTES de iniciar o app, garante que as migrations sejam aplicadas
release: flask db upgrade

# --- Processo Web (API Flask) ---
# Gunicorn com auto-tuning de workers e threads
web: gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers ${WEB_CONCURRENCY:-2} \
    --threads ${THREADS:-4} \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -

# --- Processo Worker (Monitor de Bots) ---
# Mantém o monitor ativo em paralelo
worker: python monitor.py