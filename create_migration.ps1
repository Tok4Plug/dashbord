param(
    [string]$Message = "update bots with last_ok and indexes"
)

Write-Host "🔄 Gerando migration automática..." -ForegroundColor Cyan
flask db migrate -m "$Message"

# Encontra o arquivo mais recente em migrations/versions
$latestFile = Get-ChildItem "migrations\versions\*.py" | Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $latestFile) {
    Write-Host "❌ Nenhuma migration encontrada." -ForegroundColor Red
    exit 1
}

Write-Host "✍️ Substituindo conteúdo da migration em: $($latestFile.FullName)" -ForegroundColor Yellow

$migrationContent = @"
"""Update bots table with monitoring fields and advanced indexes

Revision ID: 20250917_update_bots
Revises: 
Create Date: 2025-09-17 05:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


# --- Identificadores da migration ---
revision = "20250917_update_bots"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_ok", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_bots_status", ["status"], unique=False)
        batch_op.create_index("ix_bots_failures", ["failures"], unique=False)
        batch_op.create_index("ix_bots_last_ok", ["last_ok"], unique=False)
        batch_op.create_index("ix_bots_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_bots_updated_at", ["updated_at"], unique=False)
        batch_op.create_unique_constraint("uq_bot_redirect_url", ["redirect_url"])


def downgrade() -> None:
    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.drop_constraint("uq_bot_redirect_url", type_="unique")
        batch_op.drop_index("ix_bots_status")
        batch_op.drop_index("ix_bots_failures")
        batch_op.drop_index("ix_bots_last_ok")
        batch_op.drop_index("ix_bots_created_at")
        batch_op.drop_index("ix_bots_updated_at")
        batch_op.drop_column("last_ok")
"@

Set-Content -Path $latestFile.FullName -Value $migrationContent -Encoding UTF8

Write-Host "🚀 Aplicando migration no banco..." -ForegroundColor Cyan
flask db upgrade

Write-Host "✅ Migration aplicada com sucesso!" -ForegroundColor Green