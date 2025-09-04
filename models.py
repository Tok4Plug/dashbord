from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Bot(db.Model):
    __tablename__ = "bots"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    token = db.Column(db.String(255), nullable=True)
    redirect_url = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), default="reserva")  # "ativo" ou "reserva"
    failures = db.Column(db.Integer, default=0)  # contador de falhas consecutivas

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "redirect_url": self.redirect_url,
            "status": self.status,
            "failures": self.failures,
        }