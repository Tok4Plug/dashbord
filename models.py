from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint

db = SQLAlchemy()

class Bot(db.Model):
    __tablename__ = "bots"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)  
    token = db.Column(db.String(200), nullable=True)               
    redirect_url = db.Column(db.String(500), nullable=False)       

    status = db.Column(db.String(20), default="reserva", index=True)   # "ativo" ou "reserva"
    failures = db.Column(db.Integer, default=0, index=True)            # contador de falhas

    # Campos extras para monitoramento
    last_ok = db.Column(db.DateTime, nullable=True)                   # última vez que respondeu OK
    created_at = db.Column(db.DateTime, default=datetime.utcnow)       # quando o bot foi adicionado
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Constraints extras
    __table_args__ = (
        UniqueConstraint("redirect_url", name="uq_bot_redirect_url"),
        Index("idx_status_failures", "status", "failures"),
    )

    # ---------- Métodos utilitários ----------
    def mark_active(self):
        self.status = "ativo"
        self.failures = 0
        self.last_ok = datetime.utcnow()

    def mark_reserve(self):
        self.status = "reserva"
        self.failures = 0

    def reset_failures(self):
        self.failures = 0
        self.last_ok = datetime.utcnow()

    # ---------- Serialização ----------
    def to_dict(self, with_meta: bool = True):
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