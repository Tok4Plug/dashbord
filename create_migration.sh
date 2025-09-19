#!/bin/bash
# Script para criar e aplicar migration avançada no Flask-Migrate

MSG=${1:-"update bots with last_ok and indexes"}

echo "🔄 Gerando migration automática..."
flask db migrate -m "$MSG"

# Descobre o último arquivo criado dentro de migrations/versions
LATEST_FILE=$(ls -t migrations/versions/*.py | head -n 1)

echo "✍️ Substituindo conteúdo da migration em: $LATEST_FILE"

# Sobrescreve o conteúdo com o arquivo robusto que te passei
cat > $LATEST_FILE << 'EOF'
"""Update bots table with monitoring fields and advanced indexes

Revision ID: 20250917_update_bots
Revises: 
Create Date: 2025-09-17 05:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# --- Identificadores da migration ---
revision = "20250917_update_bots"
down_revision = None  # coloque o ID da última migration se já existir
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Adiciona coluna last_ok se não existir
    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_ok", sa.DateTime(), nullable=True))

        # Criação de índices avançados
        batch_op.create_index("ix_bots_status", ["status"], unique=False)
        batch_op.create_index("ix_bots_failures", ["failures"], unique=False)
        batch_op.create_index("ix_bots_last_ok", ["last_ok"], unique=False)
        batch_op.create_index("ix_bots_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_bots_updated_at", ["updated_at"], unique=False)

        # Constraint única em redirect_url
        batch_op.create_unique_constraint("uq_bot_redirect_url", ["redirect_url"])


def downgrade() -> None:
    # Remove índices e coluna caso precise rollback
    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.drop_constraint("uq_bot_redirect_url", type_="unique")
        batch_op.drop_index("ix_bots_status")
        batch_op.drop_index("ix_bots_failures")
        batch_op.drop_index("ix_bots_last_ok")
        batch_op.drop_index("ix_bots_created_at")
        batch_op.drop_index("ix_bots_updated_at")
        batch_op.drop_column("last_ok")
EOF

echo "🚀 Aplicando migration no banco..."
flask db upgrade

echo "✅ Migration aplicada com sucesso!"