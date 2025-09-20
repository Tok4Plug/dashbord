"""update bots table with monitoring fields (failsafe)

Revision ID: 003_update_bots_failsafe
Revises: 
Create Date: 2025-09-19 03:50:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# IDs de controle da migration
revision = "003_update_bots_failsafe"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    # Obter colunas existentes
    existing_columns = [col["name"] for col in inspector.get_columns("bots")]
    existing_indexes = [idx["name"] for idx in inspector.get_indexes("bots")]
    existing_constraints = [uc["name"] for uc in inspector.get_unique_constraints("bots")]

    with op.batch_alter_table("bots", schema=None) as batch_op:
        # Adiciona colunas se não existirem
        if "last_ok" not in existing_columns:
            batch_op.add_column(sa.Column("last_ok", sa.DateTime(), nullable=True))
        if "created_at" not in existing_columns:
            batch_op.add_column(sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
        if "updated_at" not in existing_columns:
            batch_op.add_column(sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))

        # Índices failsafe
        if "ix_bots_last_ok" not in existing_indexes:
            batch_op.create_index("ix_bots_last_ok", ["last_ok"], unique=False)
        if "ix_bots_created_at" not in existing_indexes:
            batch_op.create_index("ix_bots_created_at", ["created_at"], unique=False)
        if "ix_bots_updated_at" not in existing_indexes:
            batch_op.create_index("ix_bots_updated_at", ["updated_at"], unique=False)
        if "idx_status_failures" not in existing_indexes:
            batch_op.create_index("idx_status_failures", ["status", "failures"], unique=False)

        # Constraint única failsafe
        if "uq_bot_redirect_url" not in existing_constraints:
            batch_op.create_unique_constraint("uq_bot_redirect_url", ["redirect_url"])


def downgrade() -> None:
    with op.batch_alter_table("bots", schema=None) as batch_op:
        # Remover constraint se existir
        try:
            batch_op.drop_constraint("uq_bot_redirect_url", type_="unique")
        except Exception:
            pass
        # Remover índices se existirem
        for idx in ["ix_bots_last_ok", "ix_bots_created_at", "ix_bots_updated_at", "idx_status_failures"]:
            try:
                batch_op.drop_index(idx)
            except Exception:
                pass
        # Remover colunas se existirem
        for col in ["last_ok", "created_at", "updated_at"]:
            try:
                batch_op.drop_column(col)
            except Exception:
                pass