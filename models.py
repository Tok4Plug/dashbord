# ================================
# models.py (versão final robusta + sincronizado com app.py)
# ================================
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, func

# Inicializa o SQLAlchemy
db = SQLAlchemy()


class Bot(db.Model):
    __tablename__ = "bots"

    # ---------- Identificação ----------
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Nome único (ex: Bot A, Bot B, etc.)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)

    # Token de API do Telegram
    token = db.Column(db.String(255), nullable=True)

    # URL de redirecionamento / webhook (não pode ser NULL)
    redirect_url = db.Column(db.String(500), nullable=False, index=True)

    # Status do bot: ativo | reserva | inativo
    status = db.Column(db.String(20), default="reserva", index=True)

    # Contador de falhas consecutivas
    failures = db.Column(db.Integer, default=0, index=True)

    # ---------- Monitoramento ----------
    last_ok = db.Column(db.DateTime, nullable=True, index=True)
    last_reason = db.Column(db.Text, nullable=True)

    # Resultados das últimas checagens
    last_token_ok = db.Column(db.Boolean, nullable=True)
    last_url_ok = db.Column(db.Boolean, nullable=True)
    last_webhook_ok = db.Column(db.Boolean, nullable=True)

    # Últimos códigos HTTP
    last_token_http = db.Column(db.Integer, nullable=True)
    last_url_http = db.Column(db.Integer, nullable=True)

    # Informações de webhook
    last_webhook_url = db.Column(db.Text, nullable=True)
    last_webhook_error = db.Column(db.Text, nullable=True)
    last_webhook_error_at = db.Column(db.DateTime, nullable=True)
    pending_update_count = db.Column(db.Integer, nullable=True)

    # ---------- Timestamps ----------
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)

    # ---------- Constraints ----------
    __table_args__ = (
        UniqueConstraint("redirect_url", name="uq_bot_redirect_url"),
        Index("idx_status_failures", "status", "failures"),
        {"sqlite_autoincrement": True},
    )

    # ---------- Métodos utilitários ----------
    def mark_active(self):
        """Marca como ativo e reseta falhas"""
        self.status = "ativo"
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def mark_reserve(self):
        """Coloca como reserva"""
        self.status = "reserva"
        self.failures = 0
        self.touch()

    def increment_failure(self):
        """Incrementa falhas"""
        self.failures = (self.failures or 0) + 1
        self.touch()

    def reset_failures(self):
        """Reseta falhas"""
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def touch(self):
        """Atualiza o updated_at"""
        self.updated_at = datetime.utcnow()

    # ---------- Queries utilitárias ----------
    @classmethod
    def get_active(cls):
        return cls.query.filter_by(status="ativo").all()

    @classmethod
    def get_reserve(cls):
        return cls.query.filter_by(status="reserva").all()

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

    # ---------- Serialização ----------
    def to_dict(self, with_meta: bool = True) -> dict:
        base = {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "redirect_url": self.redirect_url,
            "status": self.status,
            "failures": self.failures,
            "last_reason": self.last_reason,
            "last_token_ok": self.last_token_ok,
            "last_url_ok": self.last_url_ok,
            "last_webhook_ok": self.last_webhook_ok,
            "last_token_http": self.last_token_http,
            "last_url_http": self.last_url_http,
            "last_webhook_url": self.last_webhook_url,
            "last_webhook_error": self.last_webhook_error,
            "pending_update_count": self.pending_update_count,
        }
        if with_meta:
            base.update({
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "last_webhook_error_at": self.last_webhook_error_at.isoformat() if self.last_webhook_error_at else None,
            })
        return base

    # ---------- Representação ----------
    def __repr__(self):
        return (
            f"<Bot id={self.id} name='{self.name}' "
            f"status='{self.status}' failures={self.failures}>"
        )