from flask import Flask, jsonify
from monitor import ativos, reserva

app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "ativos": ativos,
        "reserva": reserva,
        "status": "Monitor rodando no Railway"
    })