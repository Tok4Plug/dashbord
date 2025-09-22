# ================================
# app.py (Monitor Avançado + Sincronia Dashboard + Alerts + Verificação Confiável)
# ================================
import os
import time
import json
import logging
import threading
import random
from contextlib import contextmanager
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from twilio.rest import Client

from utils import check_link, check_token, check_probe, log_event  # check_* já existentes no seu utils.py
from models import db, Bot

# ================================
# Configuração de logging
# ================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("monitor")

# ================================
# Variáveis de ambiente
# ================================
TYPEBOT_API = os.getenv("TYPEBOT_API", "")
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID", "")

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")

MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")  # chat/Grupo para probe no Telegram
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))  # intervalo de varredura
MAX_LOGS = int(os.getenv("MAX_LOGS", "500"))
STARTUP_GRACE_SECONDS = int(os.getenv("STARTUP_GRACE_SECONDS", "15"))  # carência pós-boot
DOUBLECHECK_DELAY_SECONDS = int(os.getenv("DOUBLECHECK_DELAY_SECONDS", "5"))  # delay antes de confirmar queda
RETRY_CHECKS_PER_PASS = int(os.getenv("RETRY_CHECKS_PER_PASS", "1"))  # tentativas extras por passagem (além do double-check)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8.0"))  # tempo de timeout sugerido para requests nas funções utils

# Controle de inicialização do monitor (opcional)
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")

# ================================
# Setup Flask
# ================================
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "change_me")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL não configurado!")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# ================================
# Setup Twilio
# ================================
twilio_client = None
if TWILIO_SID and TWILIO_AUTH:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

# ================================
# Estruturas globais (em memória)
# ================================
monitor_logs = []
metrics = {
    "checks_total": 0,
    "failures_total": 0,
    "switches_total": 0,
    "last_check_ts": None
}

# cache de diagnóstico (mostrado na Dashboard) e estado de alertas (anti-spam)
diag_cache = {}            # { bot_id: {"when": ts, "diag": {...}} }
alert_state = {}           # { bot_id: {"last_fail_count": int, "last_alert_ts": ts} }

# trava de exclusão mútua local para o monitor (thread-safe)
_state_lock = threading.Lock()

# ================================
# Utilidades
# ================================
def add_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with _state_lock:
        monitor_logs.append(line)
        if len(monitor_logs) > MAX_LOGS:
            monitor_logs.pop(0)
    logger.info(msg)

def safe_commit():
    """Commit com tratamento e log padronizado."""
    try:
        db.session.commit()
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        add_log(f"❌ Erro no commit: {e}")
        return False

def send_whatsapp(title: str, details: str):
    """Envia mensagem formatada via WhatsApp (Twilio)."""
    if not twilio_client:
        add_log("⚠️ Twilio não configurado.")
        return
    msg = (
        "📡 *TOK4 Monitor*\n\n"
        f"🔔 {title}\n\n"
        f"{details}\n\n"
        f"⏰ {time.strftime('%d/%m %H:%M:%S')}"
    )
    try:
        twilio_client.messages.create(
            body=msg,
            from_=f"whatsapp:{TWILIO_FROM}",
            to=f"whatsapp:{ADMIN_WHATSAPP}"
        )
        add_log("📲 WhatsApp enviado")
    except Exception as e:
        add_log(f"❌ Erro ao enviar WhatsApp: {e}")

def get_bots_from_db():
    """Recupera listas atuais de ativos e reserva, sempre a partir do banco (evita staleness)."""
    try:
        ativos = Bot.query.filter_by(status="ativo").order_by(Bot.id.asc()).all()
        reserva = Bot.query.filter_by(status="reserva").order_by(Bot.id.asc()).all()
        return ativos, reserva
    except SQLAlchemyError as e:
        add_log(f"❌ Erro ao consultar banco: {e}")
        return [], []

# ---------------- Verificação confiável ----------------
def _run_checks_once(bot):
    """
    Executa uma rodada de checagens para um BOT:
      - check_token → chama API do Telegram (getMe ou equivalente) para validar token.
      - check_link  → valida redirect_url (apenas sanidade de URL/HTTP; não define 'vida' do bot sozinho).
      - check_probe → tenta enviar mensagem/echo no chat monitor (se configurado).
    Retorna (diag_dict, decision_ok_parcial)
    """
    # Garantir que funções utils sejam chamadas com timeouts adequados (se aceitarem parâmetro).
    # Aqui assumimos que as funções já possuem timeout interno. Caso seu utils permita, adicione:
    #   check_token(bot.token, timeout=HTTP_TIMEOUT) etc. Para manter compatibilidade, chamaremos conforme assinatura base.
    token_ok, token_reason, username = check_token(bot.token or "")
    url_ok, url_reason = check_link(bot.redirect_url or "")
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)

    # Regras de decisão:
    # 1) Token_ok é a evidência principal de que o bot "existe" e a API está respondendo.
    # 2) Probe_ok é um reforço (quando possível). Se disponível e falhar, reduz confiança.
    # 3) URL_ok é apenas sanidade da URL armazenada; NÃO define vida do bot sozinho.
    #
    # Decisão parcial "ok" quando:
    #   - token_ok é True
    #   - e (probe_ok é True OU probe_ok é None (não testável))
    decision_ok = bool(token_ok and (probe_ok is True or probe_ok is None))

    diag = {
        "token_ok": bool(token_ok),
        "url_ok": bool(url_ok),
        "probe_ok": probe_ok if probe_ok in (True, False) else None,
        "decision_ok": decision_ok,
        "reasons": {
            "token": token_reason,
            "url": url_reason,
            "probe": probe_reason
        },
        "username": username
    }
    return diag, decision_ok

def diagnosticar_bot(bot):
    """
    Verificação robusta com double-check e tentativas por passagem.
    1) Executa checagem.
    2) Se falhar, aguarda DOUBLECHECK_DELAY_SECONDS + jitter e repete para confirmar.
    3) Pode fazer RETRY_CHECKS_PER_PASS tentativas extras (configurável).
    A decisão final só será 'falha' se pelo menos duas rodadas consecutivas
    retornarem 'decision_ok=False' (evita falsos positivos).
    """
    # Rodada 1
    diag1, ok1 = _run_checks_once(bot)
    if ok1:
        return diag1

    # Aguardar para confirmar (double-check)
    delay = DOUBLECHECK_DELAY_SECONDS + random.uniform(0.0, 1.5)
    add_log(f"⏳ {bot.name}: primeira checagem falhou, aguardando {delay:.1f}s para confirmar...")
    time.sleep(delay)

    diag2, ok2 = _run_checks_once(bot)
    # Se na segunda rodada ficar ok, consideramos recuperado e retornamos diag2
    if ok2:
        add_log(f"🔁 {bot.name}: recuperação confirmada na segunda checagem.")
        return diag2

    # Rodadas extras opcionais (robustez adicional)
    last_diag = diag2
    for n in range(max(0, RETRY_CHECKS_PER_PASS - 1)):
        step_delay = 1.0 + random.uniform(0.0, 1.0)
        add_log(f"⏳ {bot.name}: tentativa extra {n+1}/{RETRY_CHECKS_PER_PASS-1}, aguardando {step_delay:.1f}s...")
        time.sleep(step_delay)
        d, ok = _run_checks_once(bot)
        last_diag = d
        if ok:
            add_log(f"🔁 {bot.name}: recuperação confirmada em tentativa extra.")
            return d

    # Se chegou aqui, duas seguidas (e possivelmente extras) falharam → queda confirmada
    last_diag["decision_ok"] = False
    return last_diag

# ================================
# Loop de monitoramento
# ================================
@contextmanager
def _flask_app_context():
    """Context manager para garantir app_context em funções de thread."""
    with app.app_context():
        yield

def monitor_loop(interval: int = MONITOR_INTERVAL):
    """Loop principal que monitora continuamente os bots e gerencia swaps/alerts."""
    started_at = datetime.utcnow()
    with _flask_app_context():
        add_log("🔄 Iniciando varredura de bots...")
        # Mensagem de início
        try:
            ativos, reserva = get_bots_from_db()
            add_log(f"✅ Monitor ativo | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
            send_whatsapp("🚀 Monitor Iniciado", f"Ativos: {len(ativos)} | Reservas: {len(reserva)}")
        except Exception as e:
            add_log(f"❌ Falha ao iniciar monitor: {e}")

        # Loop contínuo
        while True:
            cycle_started = datetime.utcnow()

            # SE houver período de carência após start, evita trocas/quedas imediatas causadas por rede fria
            in_grace = (cycle_started - started_at).total_seconds() < STARTUP_GRACE_SECONDS

            # Recarrega listas frescas do DB a cada ciclo
            ativos, reserva = get_bots_from_db()

            for bot in ativos:
                add_log(f"🔎 Checando {bot.name} → {bot.redirect_url}")
                metrics["checks_total"] += 1
                metrics["last_check_ts"] = int(time.time())

                # Executa diagnóstico robusto
                diag = diagnosticar_bot(bot)

                # cache p/ API
                with _state_lock:
                    diag_cache[bot.id] = {"when": int(time.time()), "diag": diag}

                # Persistir últimos resultados importantes no modelo (para histórico e KPIs)
                try:
                    bot.last_token_ok = diag.get("token_ok")
                    bot.last_url_ok = diag.get("url_ok")
                    # "probe_ok" não está no modelo original; registramos como last_webhook_ok (True/False/None)
                    probe_ok_val = diag.get("probe_ok")
                    bot.last_webhook_ok = probe_ok_val if isinstance(probe_ok_val, bool) else None
                    bot.last_reason = json.dumps(diag.get("reasons", {}), ensure_ascii=False)
                except Exception:
                    pass  # proteção, caso algum campo não exista no modelo atual

                if diag["decision_ok"]:
                    # Recuperação / OK
                    bot.reset_failures()
                    bot.last_ok = datetime.utcnow()
                    if not safe_commit():
                        # Se falhar commit, apenas loga; próxima passada tentará de novo
                        pass
                    else:
                        add_log(f"✅ {bot.name}: OK | token_ok={diag['token_ok']} probe_ok={diag['probe_ok']} url_ok={diag['url_ok']}")
                    # Zera estado de alerta anti-spam
                    with _state_lock:
                        alert_state[bot.id] = {"last_fail_count": 0, "last_alert_ts": None}
                    continue

                # Queda confirmada
                bot.increment_failure()
                metrics["failures_total"] += 1
                fail_cnt = bot.failures or 0
                add_log(f"⚠️ {bot.name}: queda confirmada ({fail_cnt}/{FAIL_THRESHOLD}) "
                        f"[token_ok={diag['token_ok']}, probe_ok={diag['probe_ok']}, url_ok={diag['url_ok']}]")

                # Anti-spam: só alerta quando o contador muda ou no cruzamento do threshold
                should_alert = False
                with _state_lock:
                    st = alert_state.get(bot.id) or {}
                    last_fail_seen = st.get("last_fail_count", 0)
                    if fail_cnt != last_fail_seen or fail_cnt == FAIL_THRESHOLD:
                        should_alert = True
                    alert_state[bot.id] = {"last_fail_count": fail_cnt, "last_alert_ts": int(time.time())}

                if should_alert:
                    send_whatsapp(
                        "⚠️ Bot com problema",
                        f"Nome: {bot.name}\n"
                        f"URL: {bot.redirect_url}\n"
                        f"Falhas: {fail_cnt}/{FAIL_THRESHOLD}\n\n"
                        f"🔑 Token: {diag['reasons'].get('token')}\n"
                        f"🌍 URL: {diag['reasons'].get('url')}\n"
                        f"📡 Probe: {diag['reasons'].get('probe')}"
                    )

                # Em carência inicial: não trocar ainda; só acumular falhas
                if in_grace:
                    add_log(f"⛳ Período de carência ativo ({STARTUP_GRACE_SECONDS}s). Sem trocas por enquanto.")
                    safe_commit()  # salva o contador
                    continue

                # Substituição automática quando atingir o threshold
                if fail_cnt >= FAIL_THRESHOLD:
                    bot.mark_reserve()
                    if not safe_commit():
                        # rollback já foi chamado; seguimos sem trocar
                        pass
                    else:
                        # Tira dos ativos (apenas efeito local de log)
                        add_log(f"🔁 {bot.name} movido para 'reserva' após {fail_cnt} falhas.")

                        # Escolhe o primeiro da reserva como novo ativo
                        # (carrega novamente a lista de reservas para evitar staleness)
                        _, reserva_atual = get_bots_from_db()
                        if reserva_atual:
                            novo = reserva_atual[0]
                            novo.mark_active()
                            if safe_commit():
                                metrics["switches_total"] += 1
                                send_whatsapp(
                                    "🔄 Substituição Automática",
                                    f"❌ {bot.name} caiu\n➡️ ✅ {novo.name} ativo\n"
                                    f"Novo URL: {novo.redirect_url}"
                                )
                                add_log(f"✅ Troca concluída: {bot.name} ➜ {novo.name}")
                            else:
                                add_log(f"❌ Erro ao ativar novo bot {novo.name}.")
                        else:
                            send_whatsapp("❌ Falha Crítica", "Não há mais bots na reserva!")
                            add_log("❌ Falha Crítica: sem bots de reserva disponíveis.")

            # Ajuste do intervalo (se a rodada demorou, honra o intervalo restante)
            elapsed = (datetime.utcnow() - cycle_started).total_seconds()
            sleep_for = max(1.0, interval - elapsed)
            time.sleep(sleep_for)

# ================================
# Rotas para Dashboard / API
# ================================
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/health")
def health():
    """Rota simples para liveness/readiness checks."""
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/api/bots", methods=["GET"])
def api_bots():
    """Lista todos os bots com métricas, logs e último diagnóstico (cache)."""
    try:
        bots = Bot.query.order_by(Bot.id).all()
        payload = []
        for b in bots:
            d = b.to_dict(with_meta=True)
            # anexa diagnóstico em cache (não persistido)
            cached = diag_cache.get(b.id) or {}
            d["_diag"] = cached.get("diag")
            d["_diag_ts"] = cached.get("when")
            payload.append(d)
        return jsonify({
            "bots": payload,
            "logs": monitor_logs,
            "metrics": metrics,
            "last_action": metrics.get("last_check_ts")
        })
    except Exception as e:
        add_log(f"❌ /api/bots erro: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots", methods=["POST"])
def add_bot():
    """Adiciona um novo bot à base."""
    data = request.json or {}
    if not data.get("name") or not data.get("url") or not data.get("token"):
        return jsonify({"error": "Campos obrigatórios: name, url, token"}), 400
    try:
        bot = Bot(
            name=data["name"].strip(),
            redirect_url=data["url"].strip(),
            token=data["token"].strip(),
            status="reserva"
        )
        db.session.add(bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao salvar bot"}), 500
        add_log(f"✅ Novo bot adicionado: {bot.name}")
        return jsonify(bot.to_dict(with_meta=True)), 201
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def update_bot(bot_id):
    """Atualiza dados de um bot existente."""
    data = request.json or {}
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404

        name = data.get("name")
        url = data.get("url")
        token = data.get("token")

        if name is not None:
            bot.name = name.strip()
        if url is not None:
            bot.redirect_url = url.strip()
        if token is not None:
            bot.token = token.strip()

        if not safe_commit():
            return jsonify({"error": "Falha ao atualizar bot"}), 500

        add_log(f"✏️ Bot atualizado: {bot.name}")
        return jsonify(bot.to_dict(with_meta=True))
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    """Remove um bot da base."""
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        db.session.delete(bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao deletar bot"}), 500
        # limpa caches
        with _state_lock:
            diag_cache.pop(bot.id, None)
            alert_state.pop(bot.id, None)
        add_log(f"🗑 Bot removido: {bot.name}")
        return jsonify({"message": "Bot deletado"})
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>/force_swap", methods=["POST"])
def force_swap(bot_id):
    """Força a troca manual de um bot ativo por um da reserva."""
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404

        # se já está em reserva, apenas escolhe um para ativar
        if bot.status != "reserva":
            bot.status = "reserva"
            if not safe_commit():
                return jsonify({"error": "Falha ao colocar bot em reserva"}), 500

        # escolhe próximo da reserva
        candidato = Bot.query.filter_by(status="reserva").order_by(Bot.updated_at.asc()).first()
        if not candidato:
            return jsonify({"error": "Não há bots em reserva"}), 400

        candidato.status = "ativo"
        candidato.reset_failures()
        if not safe_commit():
            return jsonify({"error": "Falha ao ativar novo bot"}), 500

        with _state_lock:
            metrics["switches_total"] += 1

        add_log(f"🔄 Swap forçado: {bot.name} ➝ {candidato.name}")
        send_whatsapp("🔄 Swap Forçado", f"❌ {bot.name} ➝ ✅ {candidato.name}\nURL: {candidato.redirect_url}")
        return jsonify({"old": bot.to_dict(with_meta=True), "new": candidato.to_dict(with_meta=True)})
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# ================================
# Bootstrap (migrations rápidas + single-run monitor guard)
# ================================
def _apply_bootstrap_patches():
    with app.app_context():
        try:
            with db.engine.begin() as conn:
                # Campos mínimos usados pelo monitor e painel
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMP NULL"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS failures INTEGER DEFAULT 0"))
                # Campos de diagnóstico (idempotentes, se já existirem serão ignorados por alguns bancos;
                # se usar Postgres puro, prefira migrações adequadas)
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_ok BOOLEAN"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_ok BOOLEAN"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_ok BOOLEAN"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))
                except Exception:
                    pass
            add_log("✅ Patch no schema aplicado")
        except Exception as e:
            add_log(f"⚠️ Patch falhou: {e}")

# ---------- Evita múltiplos monitores quando rodando com vários workers ----------
# Em plataformas como Railway com gunicorn (vários workers), cada worker poderia iniciar sua própria thread de monitor.
# Para evitar duplicidade, usamos um lock de arquivo baseado em fcntl (Unix).
_monitor_thread = None
_filelock = None

def _try_acquire_file_lock():
    """Tenta adquirir um lock de arquivo exclusivo. Retorna True/False."""
    try:
        import fcntl
        global _filelock
        lock_path = "/tmp/tok4_monitor.lock"
        _filelock = open(lock_path, "w")
        fcntl.flock(_filelock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _filelock.write(f"pid={os.getpid()} ts={time.time()}\n")
        _filelock.flush()
        add_log("🔐 File lock adquirido: monitor exclusivo neste container.")
        return True
    except Exception as e:
        add_log(f"🔁 Monitor já em execução em outro worker (lock indisponível): {e}")
        return False

def _start_monitor_background():
    global _monitor_thread
    if not MONITOR_ENABLED:
        add_log("⏸ MONITOR_DISABLED por variável de ambiente.")
        return
    if _monitor_thread and _monitor_thread.is_alive():
        add_log("ℹ️ Monitor já está ativo (thread viva).")
        return
    if not _try_acquire_file_lock():
        # outro worker possui o lock → não inicia aqui
        return
    _monitor_thread = threading.Thread(target=monitor_loop, args=(MONITOR_INTERVAL,), daemon=True, name="tok4-monitor")
    _monitor_thread.start()
    add_log("🧵 Thread de monitoramento iniciada.")

# Inicialização
_apply_bootstrap_patches()
_start_monitor_background()

# ================================
# Main (desenvolvimento) / Gunicorn (produção)
# ================================
if __name__ == "__main__":
    # Quando executado diretamente (sem gunicorn), a thread é iniciada acima.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)