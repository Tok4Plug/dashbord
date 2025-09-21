# ================================
# models.py (versão final robusta)
# ================================
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, func

# Inicializa o SQLAlchemy
db = SQLAlchemy()


class Bot(db.Model):
    __tablename__ = "bots"

    # ---------- Colunas principais ----------
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Nome único + index para evitar duplicação
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)

    # Token opcional (alguns bots podem não precisar)
    token = db.Column(db.String(255), nullable=True)

    # URL obrigatória (não pode ser NULL) + índice para busca rápida
    redirect_url = db.Column(db.String(500), nullable=False, index=True)

    # Status padrão "reserva" (opções: ativo, reserva, inativo, etc.)
    status = db.Column(db.String(20), default="reserva", index=True)

    # Contador de falhas (para o monitor saber quando substituir)
    failures = db.Column(db.Integer, default=0, index=True)

    # ---------- Monitoramento ----------
    # Última resposta OK
    last_ok = db.Column(db.DateTime, nullable=True, index=True)

    # Datas automáticas
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True
    )

    # ---------- Constraints extras ----------
    __table_args__ = (
        UniqueConstraint("redirect_url", name="uq_bot_redirect_url"),  # único por URL
        Index("idx_status_failures", "status", "failures"),             # índice composto
        {"sqlite_autoincrement": True},                                # IDs consistentes em SQLite
    )

    # ---------- Métodos utilitários ----------
    def mark_active(self) -> None:
        """Marca o bot como ativo e reseta falhas"""
        self.status = "ativo"
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def mark_reserve(self) -> None:
        """Coloca o bot em estado de reserva"""
        self.status = "reserva"
        self.failures = 0
        self.touch()

    def increment_failure(self) -> None:
        """Incrementa contador de falhas"""
        self.failures = (self.failures or 0) + 1
        self.touch()

    def reset_failures(self) -> None:
        """Reseta falhas e atualiza último OK"""
        self.failures = 0
        self.last_ok = datetime.utcnow()
        self.touch()

    def touch(self) -> None:
        """Atualiza timestamp do updated_at"""
        self.updated_at = datetime.utcnow()

    # ---------- Queries utilitárias ----------
    @classmethod
    def get_active(cls):
        """Retorna todos os bots ativos"""
        return cls.query.filter_by(status="ativo").all()

    @classmethod
    def get_reserve(cls):
        """Retorna todos os bots em reserva"""
        return cls.query.filter_by(status="reserva").all()

    @classmethod
    def get_oldest_updated(cls):
        """Retorna o bot menos atualizado (ótimo para balanceamento)"""
        return cls.query.order_by(cls.updated_at.asc()).first()

    @classmethod
    def stats(cls):
        """Retorna estatísticas agregadas dos bots"""
        return db.session.query(
            func.count(cls.id).label("total"),
            func.sum(cls.failures).label("total_failures"),
            func.max(cls.updated_at).label("last_update"),
        ).first()

    # ---------- Serialização ----------
    def to_dict(self, with_meta: bool = True) -> dict:
        """Serializa em dicionário"""
        base = {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "redirect_url": self.redirect_url,
            "status": self.status,
            "failures": self.failures,
        }
        if with_meta:
            base.update({
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            })
        return base

    # ---------- Representação ----------
    def __repr__(self) -> str:
        return (
            f"<Bot id={self.id} name='{self.name}' "
            f"status='{self.status}' failures={self.failures}>"
        )