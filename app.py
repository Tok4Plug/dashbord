# app.py (trecho relevante - use este padrão)
import threading
from flask import Flask
from models import db, Bot
# ... suas imports, configs, funções check_bot, send_whatsapp_message, etc.

app = Flask(__name__)
# configs do SQLAlchemy, db.init_app(app) etc.

def start_monitor_thread():
    def run():
        with app.app_context():
            # criar tabelas (se necessário)
            db.create_all()
            # loop principal do monitor (substitua com sua lógica)
            import time
            while True:
                # sua função monitor que verifica bots e faz substituições
                monitor_bots_iteration()  # colocar sua lógica aqui
                time.sleep(60)
    t = threading.Thread(target=run, daemon=True)
    t.start()

# chama a função ao importar o módulo (garante que inicie com gunicorn)
start_monitor_thread()

# rotas do Flask...
@app.route("/")
def dashboard():
    # ...
    pass

# NÃO coloque thread start dentro do if __name__ == "__main__" quando usar gunicorn