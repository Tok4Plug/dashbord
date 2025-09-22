# ================================
# models.py (versão avançada, inteligente e sincronizada)
# ================================
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, func

# Inicializa o SQLAlchemy (injeção via app.py)
db = SQLAlchemy()


class Bot(db.Model):
    """
    Representa um Bot monitorado no sistema TOK4.
    Cada registro contém informações de identificação,
    status, monitoramento de saúde, métricas de falhas,
    diagnósticos detalhados e timestamps de criação/atualização.

    Essa classe foi projetada para:
    - Alta performance em consultas.
    - Sincronia direta com o monitor (app.py).
    - Serialização compatível com o Dashboard.
    - Métodos utilitários para manipulação de estado e diagnósticos.
    """
    __tablename__ = "bots"

    # ---------- Identificação ----------
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    token = db.Column(db.String(255), nullable=True)
    redirect_url = db.Column(db.String(500), nullable=False, index=True)

    # Status do bot: ativo | reserva | inativo
    status = db.Column(db.String(20), default="reserva", index=True)

    # ---------- Monitoramento ----------
    failures = db.Column(db.Integer, default=0, index=True)
    last_ok = db.Column(db.DateTime, nullable=True, index=True)
    last_reason = db.Column(db.Text, nullable=True)

    # Diagnósticos técnicos
    last_token_ok = db.Column(db.Boolean, nullable=True)
    last_url_ok = db.Column(db.Boolean, nullable=True)
    last_webhook_ok = db.Column(db.Boolean, nullable=True)

    last_token_http = db.Column(db.Integer, nullable=True)
    last_url_http = db.Column(db.Integer, nullable=True)

    last_webhook_url = db.Column(db.Text, nullable=True)
    last_webhook_error = db.Column(db.Text, nullable=True)
    last_webhook_error_at = db.Column(db.DateTime, nullable=True)
    pending_update_count = db.Column(db.Integer, nullable=True)

    # ---------- Timestamps ----------
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)

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
        """Marca como ativo, reseta falhas e registra último sucesso."""
        self.status = "ativo"
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def mark_reserve(self):
        """Coloca o bot em reserva e zera falhas."""
        self.status = "reserva"
        self.failures = 0
        self.touch()

    def mark_inactive(self, reason: str = None):
        """Desativa o bot, armazenando razão opcional."""
        self.status = "inativo"
        if reason:
            self.last_reason = reason
        self.touch()

    def increment_failure(self):
        """Incrementa falhas consecutivas."""
        self.failures = (self.failures or 0) + 1
        self.touch()

    def reset_failures(self):
        """Reseta falhas e define o último sucesso."""
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def touch(self):
        """Atualiza campo updated_at."""
        self.updated_at = datetime.utcnow()

    # ====================================================
    # Métodos de Diagnóstico
    # ====================================================
    def apply_diag(self, diag: dict):
        """
        Aplica os resultados de diagnóstico ao bot.
        Exemplo de dict esperado:
        {
            "token_ok": True,
            "url_ok": True,
            "webhook_ok": None,
            "last_token_http": 200,
            "last_url_http": 200,
            "last_webhook_url": "...",
            "last_webhook_error": None,
            "last_webhook_error_at": datetime.utcnow(),
            "pending_update_count": 0,
            "reason": "Token válido, URL ok"
        }
        """
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
        """
        Avalia se o bot está saudável.
        Critérios:
        - Token válido.
        - URL válida.
        - Webhook não crítico (True ou None).
        """
        return (
            (self.last_token_ok is True)
            and (self.last_url_ok is True)
            and (self.last_webhook_ok in (True, None))
        )

    def failure_ratio(self) -> float:
        """
        Calcula a taxa de falhas relativa ao tempo de vida do bot.
        Pode ser usado para estatísticas comparativas no dashboard.
        """
        if not self.created_at:
            return float(self.failures or 0)
        total_seconds = (datetime.utcnow() - self.created_at).total_seconds()
        if total_seconds <= 0:
            return float(self.failures or 0)
        return round((self.failures or 0) / total_seconds, 6)

    # ====================================================
    # Consultas Utilitárias
    # ====================================================
    @classmethod
    def get_active(cls):
        """Retorna todos os bots ativos."""
        return cls.query.filter_by(status="ativo").all()

    @classmethod
    def get_reserve(cls):
        """Retorna todos os bots em reserva."""
        return cls.query.filter_by(status="reserva").all()

    @classmethod
    def get_inactive(cls):
        """Retorna todos os bots inativos."""
        return cls.query.filter_by(status="inativo").all()

    @classmethod
    def get_oldest_updated(cls):
        """Retorna o bot mais antigo em termos de atualização."""
        return cls.query.order_by(cls.updated_at.asc()).first()

    @classmethod
    def stats(cls):
        """
        Retorna estatísticas globais dos bots.
        Inclui:
        - total de bots
        - total de falhas acumuladas
        - último update global
        """
        return db.session.query(
            func.count(cls.id).label("total"),
            func.sum(cls.failures).label("total_failures"),
            func.max(cls.updated_at).label("last_update"),
        ).first()

    # ====================================================
    # Serialização
    # ====================================================
    def to_dict(self, with_meta: bool = True, include_diag: bool = True) -> dict:
        """
        Serializa o objeto Bot para dicionário,
        compatível com o consumo via API do Dashboard.
        """
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