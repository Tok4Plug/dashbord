from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Bot(db.Model):
    __tablename__ = "bots"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    token = db.Column(db.String(150), nullable=False, unique=True)
    redirect_url = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="ativo")  # ativo ou reserva