import os
from sqlalchemy import create_engine, text

# Pegue a URL do banco pelo Railway (configure como variável de ambiente)
DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://postgres:ltrKkCXaMNtftdGPBmkeGmRxWDTaWbvQ@postgres.railway.internal:5432/railway"

engine = create_engine(DATABASE_URL)

sqls = [
    # Colunas
    "ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL",
    "ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()",
    "ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()",

    # Backfill
    "UPDATE bots SET created_at = COALESCE(created_at, NOW()), updated_at = COALESCE(updated_at, NOW())",

    # Índices
    "CREATE INDEX IF NOT EXISTS ix_bots_last_ok ON bots(last_ok)",
    "CREATE INDEX IF NOT EXISTS ix_bots_created_at ON bots(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_bots_updated_at ON bots(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_status_failures ON bots(status, failures)",

    # Unique redirect_url
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_redirect_url_idx ON bots(redirect_url)",
]

with engine.connect() as conn:
    for s in sqls:
        print("-> Executando:", s)
        try:
            conn.execute(text(s))
        except Exception as e:
            print("Erro (ignorado se já existe):", e)
    conn.commit()

print("✅ Patch aplicado com sucesso no banco.")