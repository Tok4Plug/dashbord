# ================================
# Procfile robusto e otimizado para Railway
# ================================

# --- Release Phase ---
# Aplica automaticamente as migrations antes de subir a aplicação
release: flask db upgrade || echo "⚠️ Nenhuma migration aplicada (talvez já estejam atualizadas)."

# --- Processo Web (API Flask) ---
# Gunicorn com auto-tuning, logs completos e tolerância a falhas
web: gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers ${WEB_CONCURRENCY:-2} \
    --threads ${THREADS:-4} \
    --timeout 120 \
    --graceful-timeout 30 \
    --keep-alive 20 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --log-level info \
    --access-logfile - \
    --error-logfile -

# --- Processo Worker (Monitor de Bots) ---
# Monitor rodando em paralelo com restart automático em caso de crash
worker: while true; do python monitor.py; echo "⚠️ Worker caiu, reiniciando em 5s..."; sleep 5; done