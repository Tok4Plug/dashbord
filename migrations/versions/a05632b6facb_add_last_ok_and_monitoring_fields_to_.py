"""add last_ok and monitoring fields to bots

Revision ID: a05632b6facb
Revises: None   # Agora esta é a primeira migration válida
Create Date: 2025-09-20 02:58:29.385306
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a05632b6facb'
down_revision = None   # ✅ não aponta mais para 003
branch_labels = None
depends_on = None


def upgrade():
    # adiciona colunas permitindo NULL inicialmente
    op.add_column('bots', sa.Column('last_ok', sa.DateTime(), nullable=True))
    op.add_column('bots', sa.Column('created_at', sa.DateTime(), nullable=True))
    op.add_column('bots', sa.Column('updated_at', sa.DateTime(), nullable=True))

    # preenche registros antigos com NOW()
    op.execute("UPDATE bots SET created_at = NOW(), updated_at = NOW() WHERE created_at IS NULL")

    # aplica restrições NOT NULL após atualizar os registros
    op.alter_column('bots', 'created_at', nullable=False)
    op.alter_column('bots', 'updated_at', nullable=False)

    # altera tipos de colunas existentes + cria índices/constraints
    with op.batch_alter_table('bots', schema=None) as batch_op:
        batch_op.alter_column(
            'name',
            existing_type=sa.VARCHAR(length=120),
            type_=sa.String(length=100),
            existing_nullable=False
        )
        batch_op.alter_column(
            'token',
            existing_type=sa.VARCHAR(length=200),
            type_=sa.String(length=255),
            existing_nullable=True
        )
        batch_op.create_index('idx_status_failures', ['status', 'failures'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_failures'), ['failures'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_last_ok'), ['last_ok'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_bots_redirect_url'), ['redirect_url'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_updated_at'), ['updated_at'], unique=False)
        batch_op.create_unique_constraint('uq_bot_redirect_url', ['redirect_url'])


def downgrade():
    with op.batch_alter_table('bots', schema=None) as batch_op:
        batch_op.drop_constraint('uq_bot_redirect_url', type_='unique')
        batch_op.drop_index(batch_op.f('ix_bots_updated_at'))
        batch_op.drop_index(batch_op.f('ix_bots_status'))
        batch_op.drop_index(batch_op.f('ix_bots_redirect_url'))
        batch_op.drop_index(batch_op.f('ix_bots_name'))
        batch_op.drop_index(batch_op.f('ix_bots_last_ok'))
        batch_op.drop_index(batch_op.f('ix_bots_failures'))
        batch_op.drop_index(batch_op.f('ix_bots_created_at'))
        batch_op.drop_index('idx_status_failures')

        batch_op.alter_column(
            'token',
            existing_type=sa.String(length=255),
            type_=sa.VARCHAR(length=200),
            existing_nullable=True
        )
        batch_op.alter_column(
            'name',
            existing_type=sa.String(length=100),
            type_=sa.VARCHAR(length=120),
            existing_nullable=False
        )

    op.drop_column('bots', 'updated_at')
    op.drop_column('bots', 'created_at')
    op.drop_column('bots', 'last_ok')