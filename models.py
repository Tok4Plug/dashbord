# ================================
# models.py (versão avançada, inteligente e sincronizada com utils.py e app.py)
# ================================
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, func

# Inicializa o SQLAlchemy (injeção feita em app.py)
db = SQLAlchemy()


def now_utc():
    """Retorna datetime UTC com timezone-aware (corrige erro naive vs aware)."""
    return datetime.now(timezone.utc)


class Bot(db.Model):
    """
    Representa um Bot monitorado no sistema TOK4.
    Cada registro contém:
    - Identificação (id, nome, token, URL)
    - Status (ativo, reserva, inativo)
    - Histórico de falhas
    - Diagnósticos detalhados de última checagem
    - Informações de webhook
    - Timestamps de criação/atualização

    Projetado para:
    - Alta performance de consulta (índices e constraints)
    - Serialização compatível com dashboard
    - Métodos utilitários (ativo/reserva/inativo)
    - Aplicação de diagnósticos de monitoramento
    """
    __tablename__ = "bots"

    # ---------- Identificação ----------
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    token = db.Column(db.String(255), nullable=False)  # obrigatório
    redirect_url = db.Column(db.String(500), nullable=False, index=True)  # obrigatório

    # ---------- Status ----------
    status = db.Column(db.String(20), default="reserva", index=True)  # ativo | reserva | inativo

    # ---------- Monitoramento ----------
    failures = db.Column(db.Integer, default=0, index=True)
    last_ok = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    last_reason = db.Column(db.Text, nullable=True)

    # Diagnósticos técnicos
    last_token_ok = db.Column(db.Boolean, nullable=True)
    last_url_ok = db.Column(db.Boolean, nullable=True)
    last_webhook_ok = db.Column(db.Boolean, nullable=True)

    # Códigos HTTP
    last_token_http = db.Column(db.Integer, nullable=True)
    last_url_http = db.Column(db.Integer, nullable=True)

    # Webhook details
    last_webhook_url = db.Column(db.Text, nullable=True)
    last_webhook_error = db.Column(db.Text, nullable=True)
    last_webhook_error_at = db.Column(db.DateTime(timezone=True), nullable=True)
    pending_update_count = db.Column(db.Integer, nullable=True)

    # ---------- Timestamps ----------
    created_at = db.Column(db.DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False, index=True)

    # ---------- Constraints & Indexes ----------
    __table_args__ = (
        UniqueConstraint("redirect_url", name="uq_bot_redirect_url"),
        Index("idx_status_failures", "status", "failures"),
        Index("idx_name_status", "name", "status"),
        Index("idx_failures_updated", "failures", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    # ====================================================
    # Métodos de Estado
    # ====================================================
    def mark_active(self):
        """Marca como ativo, reseta falhas e atualiza último sucesso."""
        self.status = "ativo"
        self.failures = 0
        self.last_ok = now_utc()
        self.touch()

    def mark_reserve(self):
        """Coloca em reserva e zera falhas."""
        self.status = "reserva"
        self.failures = 0
        self.touch()

    def mark_inactive(self, reason: str = None):
        """Marca como inativo e opcionalmente registra motivo."""
        self.status = "inativo"
        if reason:
            self.last_reason = reason
        self.touch()

    def increment_failure(self):
        """Incrementa contador de falhas consecutivas."""
        self.failures = (self.failures or 0) + 1
        self.touch()

    def reset_failures(self):
        """Reseta contador de falhas e atualiza último sucesso."""
        self.failures = 0
        self.last_ok = now_utc()
        self.touch()

    def touch(self):
        """Atualiza timestamp de atualização."""
        self.updated_at = now_utc()

    # ====================================================
    # Métodos de Diagnóstico
    # ====================================================
    def apply_diag(self, diag: dict):
        """Aplica resultados de diagnóstico ao registro."""
        self.last_token_ok = diag.get("token_ok")
        self.last_url_ok = diag.get("url_ok")
        self.last_webhook_ok = diag.get("webhook_ok")

        self.last_token_http = diag.get("last_token_http")
        self.last_url_http = diag.get("last_url_http")

        self.last_webhook_url = diag.get("last_webhook_url")
        self.last_webhook_error = diag.get("last_webhook_error")

        if diag.get("last_webhook_error_at"):
            self.last_webhook_error_at = diag["last_webhook_error_at"]

        self.pending_update_count = diag.get("pending_update_count")
        self.last_reason = diag.get("reason")

    def is_healthy(self) -> bool:
        """Avalia se o bot está saudável."""
        return (
            (self.last_token_ok is True)
            and (self.last_url_ok is True)
            and (self.last_webhook_ok in (True, None))
        )

    def failure_ratio(self) -> float:
        """Calcula taxa de falhas relativa ao tempo de vida do bot."""
        if not self.created_at:
            return float(self.failures or 0)
        total_seconds = (now_utc() - self.created_at).total_seconds()
        if total_seconds <= 0:
            return float(self.failures or 0)
        return round((self.failures or 0) / total_seconds, 6)

    # ====================================================
    # Consultas Utilitárias
    # ====================================================
    @classmethod
    def get_active(cls):
        return cls.query.filter_by(status="ativo").all()

    @classmethod
    def get_reserve(cls):
        return cls.query.filter_by(status="reserva").all()

    @classmethod
    def get_inactive(cls):
        return cls.query.filter_by(status="inativo").all()

    @classmethod
    def get_oldest_updated(cls):
        return cls.query.order_by(cls.updated_at.asc()).first()

    @classmethod
    def stats(cls):
        return db.session.query(
            func.count(cls.id).label("total"),
            func.sum(cls.failures).label("total_failures"),
            func.max(cls.updated_at).label("last_update"),
        ).first()

    # ====================================================
    # Serialização
    # ====================================================
    def to_dict(self, with_meta: bool = True, include_diag: bool = True) -> dict:
        base = {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "redirect_url": self.redirect_url,
            "status": self.status,
            "failures": self.failures,
            "last_reason": self.last_reason,
        }

        if include_diag:
            base.update({
                "last_token_ok": self.last_token_ok,
                "last_url_ok": self.last_url_ok,
                "last_webhook_ok": self.last_webhook_ok,
                "last_token_http": self.last_token_http,
                "last_url_http": self.last_url_http,
                "last_webhook_url": self.last_webhook_url,
                "last_webhook_error": self.last_webhook_error,
                "pending_update_count": self.pending_update_count,
            })

        if with_meta:
            base.update({
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "last_webhook_error_at": self.last_webhook_error_at.isoformat() if self.last_webhook_error_at else None,
                "failure_ratio": self.failure_ratio(),
            })

        return base

    # ====================================================
    # Representação
    # ====================================================
    def __repr__(self):
        return (
            f"<Bot id={self.id} name='{self.name}' "
            f"status='{self.status}' failures={self.failures} "
            f"last_ok={self.last_ok} updated_at={self.updated_at}>"
        )