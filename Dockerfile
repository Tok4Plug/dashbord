# ================================
# Etapa 1: Base Python
# ================================
FROM python:3.12-slim AS base

# Evitar prompts interativos
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Diretório da aplicação
WORKDIR /app

# Instalar dependências do sistema necessárias (psycopg2, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    curl \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependências do projeto
COPY requirements.txt .

# Instalar dependências do Python
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# ================================
# Etapa 2: App
# ================================
FROM base AS app

# Variáveis de ambiente padrão (Railway sobrescreve)
ENV FLASK_APP=app.py \
    FLASK_ENV=production \
    PORT=5000 \
    WEB_CONCURRENCY=2 \
    THREADS=4

# Copiar o restante dos arquivos
COPY . .

# Expor porta
EXPOSE 5000

# Script de inicialização que garante migrations e execução
CMD flask db upgrade || echo "⚠️ Nenhuma migration aplicada (talvez já estejam atualizadas)." \
    && gunicorn app:app \
        --bind 0.0.0.0:$PORT \
        --workers ${WEB_CONCURRENCY} \
        --threads ${THREADS} \
        --timeout 120 \
        --graceful-timeout 30 \
        --keep-alive 20 \
        --max-requests 1000 \
        --max-requests-jitter 100 \
        --log-level info \
        --access-logfile - \
        --error-logfile -