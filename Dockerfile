# ================================
# Etapa 1: Base Python
# ================================
FROM python:3.12-slim AS base

# Evitar prompts interativos
ENV DEBIAN_FRONTEND=noninteractive

# Diretório da aplicação
WORKDIR /app

# Instalar dependências do sistema (psycopg2, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependências do projeto
COPY requirements.txt .

# Instalar dependências do Python
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ================================
# Etapa 2: App
# ================================
FROM base AS app

# Variáveis de ambiente padrão (Railway sobrescreve)
ENV FLASK_APP=app.py \
    FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PORT=5000

# Copiar o restante dos arquivos
COPY . .

# Expor porta
EXPOSE 5000

# Rodar migrations automaticamente no deploy
CMD flask db upgrade && gunicorn app:app --bind 0.0.0.0:$PORT --workers=4 --threads=2 --timeout=120