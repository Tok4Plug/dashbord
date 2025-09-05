from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Bot(db.Model):
    __tablename__ = "bots"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    token = db.Column(db.String(200), nullable=True)
    redirect_url = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), default="reserva")
    failures = db.Column(db.Integer, default=0)  # <-- nova coluna para contar falhas

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "redirect_url": self.redirect_url,
            "status": self.status,
            "failures": self.failures,
        }