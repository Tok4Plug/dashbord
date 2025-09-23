# ================================
# app.py (Monitor Avançado + Dashboard + CRUD + Alerts + Verificação Confiável + WebhookInfo Logs)
# ================================
import os
import time
import json
import logging
import threading
import random
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from flask import Flask, render_template, jsonify, request, make_response
from sqlalchemy.exc import SQLAlchemyError, DBAPIError, IntegrityError
from sqlalchemy import text
from twilio.rest import Client

# Importamos funções auxiliares
from utils import check_link, check_token, check_probe, check_webhook, log_event
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
TYPEBOT_API = os.getenv("TYPEBOT_API", "").strip()
TYPEBOT_FLOW_ID = os.getenv("TYPEBOT_FLOW_ID", "").strip()

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP")

MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
MAX_LOGS = int(os.getenv("MAX_LOGS", "500"))
STARTUP_GRACE_SECONDS = int(os.getenv("STARTUP_GRACE_SECONDS", "15"))
DOUBLECHECK_DELAY_SECONDS = int(os.getenv("DOUBLECHECK_DELAY_SECONDS", "5"))
RETRY_CHECKS_PER_PASS = int(os.getenv("RETRY_CHECKS_PER_PASS", "1"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8.0"))
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")

DASHBOARD_ALLOW_ORIGIN = os.getenv("DASHBOARD_ALLOW_ORIGIN", "*")  # CORS simples

# ================================
# Helpers de tempo (UTC timezone-aware)
# ================================
def now_utc():
    return datetime.now(timezone.utc)

# ================================
# Setup Flask
# ================================
app = Flask(__name__, template_folder="templates")  # mantém templates/
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
# Estruturas globais
# ================================
monitor_logs = []
metrics = {"checks_total": 0, "failures_total": 0, "switches_total": 0, "last_check_ts": None}
diag_cache = {}
alert_state = {}
_state_lock = threading.Lock()

# ================================
# CORS básico (sem dependências)
# ================================
@app.after_request
def add_cors_headers(resp):
    try:
        resp.headers["Access-Control-Allow-Origin"] = DASHBOARD_ALLOW_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    except Exception:
        pass
    return resp

@app.route("/api/<path:_>", methods=["OPTIONS"])
def cors_preflight(_):
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = DASHBOARD_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp

# ================================
# Funções auxiliares
# ================================
def add_log(msg: str):
    ts = now_utc().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{ts}] {msg}"
    with _state_lock:
        monitor_logs.append(line)
        if len(monitor_logs) > MAX_LOGS:
            monitor_logs.pop(0)
    logger.info(msg)

def safe_commit():
    try:
        db.session.commit()
        return True
    except (IntegrityError, SQLAlchemyError, DBAPIError) as e:
        db.session.rollback()
        add_log(f"❌ Erro no commit: {e}")
        return False

def send_whatsapp(title: str, details: str):
    if not twilio_client or not (TWILIO_FROM and ADMIN_WHATSAPP):
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

def _rollback_if_failed_tx(e: Exception):
    try:
        if "current transaction is aborted" in str(e).lower():
            db.session.rollback()
    except Exception:
        pass

def get_bots_from_db():
    try:
        ativos = Bot.query.filter_by(status="ativo").order_by(Bot.id.asc()).all()
        reserva = Bot.query.filter_by(status="reserva").order_by(Bot.id.asc()).all()
        return ativos, reserva
    except (SQLAlchemyError, DBAPIError) as e:
        _rollback_if_failed_tx(e)
        add_log(f"❌ Erro ao consultar banco: {e}")
        return [], []

def _get_payload():
    """
    Lê JSON ou form-data e normaliza strings. Suporta alias 'url' -> 'redirect_url'.
    """
    data = request.get_json(silent=True) or {}
    if not data:
        data = request.form.to_dict() or {}
    for k in list(data.keys()):
        if isinstance(data[k], str):
            data[k] = data[k].strip()
    # compatibilidade com painéis antigos
    if "url" in data and "redirect_url" not in data:
        data["redirect_url"] = data.pop("url")
    return data

# ================================
# Enriquecimento de leads + envio (subscribe)
# ================================
def _guess_e164(number: str):
    if not number:
        return None
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return None
    # se já iniciar com país (ex: 55...), adiciona '+'
    if number.strip().startswith("+"):
        return f"+{digits}"
    # Heurística simples Brasil (55) quando tiver >= 10 dígitos e não tiver país explícito
    if len(digits) >= 10 and not digits.startswith("0"):
        return f"+{digits}" if digits.startswith("55") else f"+55{digits}"
    return f"+{digits}"

def _enrich_lead(payload: dict) -> dict:
    p = dict(payload or {})
    # timestamps UTC
    p.setdefault("ts_utc", now_utc().isoformat())
    # normalizações
    phone = p.get("phone") or p.get("whatsapp") or p.get("telefone")
    p["phone_e164_guess"] = _guess_e164(phone)
    if p.get("email"):
        try:
            p["email_domain"] = p["email"].split("@", 1)[1].lower()
        except Exception:
            p["email_domain"] = None
    name = (p.get("name") or p.get("nome") or "").strip()
    if name:
        parts = [x for x in name.split(" ") if x]
        p["first_name"] = parts[0] if parts else None
        p["last_name"] = parts[-1] if len(parts) > 1 else None
    # utm & ref
    for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        p.setdefault(k, request.args.get(k) or request.form.get(k))
    p.setdefault("referer", request.headers.get("Referer"))
    # links úteis
    if p.get("phone_e164_guess"):
        digits = "".join(ch for ch in p["phone_e164_guess"] if ch.isdigit())
        p["whatsapp_link"] = f"https://wa.me/{digits}"
    return p

def _send_to_typebot(enriched: dict) -> dict:
    """
    Envia para o endpoint de subscribe/lead do Typebot (ou API externa genérica).
    Trabalha somente se TYPEBOT_API estiver configurado. Retorna dict com status.
    """
    info = {"sent": False, "status": None, "error": None}
    if not TYPEBOT_API:
        info["error"] = "TYPEBOT_API não configurado"
        add_log("ℹ️ TYPEBOT_API ausente: lead registrado localmente, mas não enviado.")
        return info
    try:
        # payload base
        out = {
            "flow_id": TYPEBOT_FLOW_ID or None,
            "event": "subscribe",
            "lead": enriched
        }
        # POST genérico; a API exata pode variar conforme seu conector
        resp = requests.post(TYPEBOT_API, json=out, timeout=HTTP_TIMEOUT)
        info["status"] = resp.status_code
        if 200 <= resp.status_code < 300:
            info["sent"] = True
        else:
            info["error"] = f"HTTP {resp.status_code} - {resp.text[:300]}"
        return info
    except Exception as e:
        info["error"] = str(e)
        add_log(f"❌ Erro no envio do lead: {e}")
        return info

# ================================
# Verificação confiável (com WebhookInfo inteligente)
# ================================
def _run_checks_once(bot):
    token_ok, token_reason, username = check_token(bot.token or "")
    url_ok, url_reason = check_link(bot.redirect_url or "")
    probe_ok, probe_reason = check_probe(bot.token, MONITOR_CHAT_ID)
    webhook_ok, webhook_reason, webhook_info = check_webhook(bot.token or "")

    decision_ok = bool(token_ok and (probe_ok is True or probe_ok is None))

    if decision_ok and not webhook_ok:
        add_log(f"⚠️ {bot.name}: webhook falhou ({webhook_reason}), mas bot responde normalmente.")

    diag = {
        "token_ok": token_ok,
        "url_ok": url_ok,
        "probe_ok": probe_ok if probe_ok in (True, False) else None,
        "webhook_ok": webhook_ok,
        "decision_ok": decision_ok,
        "reasons": {
            "token": token_reason,
            "url": url_reason,
            "probe": probe_reason,
            "webhook": webhook_reason
        },
        "username": username,
        "webhook_info": webhook_info
    }
    return diag, decision_ok

def diagnosticar_bot(bot):
    diag1, ok1 = _run_checks_once(bot)
    if ok1:
        return diag1

    delay = DOUBLECHECK_DELAY_SECONDS + random.uniform(0.0, 1.5)
    add_log(f"⏳ {bot.name}: primeira checagem falhou, aguardando {delay:.1f}s...")
    time.sleep(delay)

    diag2, ok2 = _run_checks_once(bot)
    if ok2:
        add_log(f"🔁 {bot.name}: recuperação confirmada na segunda checagem.")
        return diag2

    last_diag = diag2
    for _ in range(max(0, RETRY_CHECKS_PER_PASS - 1)):
        time.sleep(1.0 + random.uniform(0.0, 1.0))
        d, ok = _run_checks_once(bot)
        last_diag = d
        if ok:
            add_log(f"🔁 {bot.name}: recuperação confirmada em tentativa extra.")
            return d

    last_diag["decision_ok"] = False
    return last_diag

# ================================
# Loop de monitoramento
# ================================
@contextmanager
def _flask_app_context():
    with app.app_context():
        yield

def monitor_loop(interval: int = MONITOR_INTERVAL):
    started_at = now_utc()
    with _flask_app_context():
        add_log("🔄 Iniciando varredura de bots...")
        ativos, reserva = get_bots_from_db()
        add_log(f"✅ Monitor ativo | Ativos: {len(ativos)} | Reserva: {len(reserva)}")
        send_whatsapp("🚀 Monitor Iniciado", f"Ativos: {len(ativos)} | Reservas: {len(reserva)}")

        while True:
            cycle_started = now_utc()
            in_grace = (cycle_started - started_at).total_seconds() < STARTUP_GRACE_SECONDS
            ativos, reserva = get_bots_from_db()

            for bot in ativos:
                add_log(f"🔎 Checando {bot.name} → {bot.redirect_url}")
                metrics["checks_total"] += 1
                metrics["last_check_ts"] = int(time.time())

                diag = diagnosticar_bot(bot)
                with _state_lock:
                    diag_cache[bot.id] = {"when": int(time.time()), "diag": diag}

                try:
                    bot.last_token_ok = diag.get("token_ok")
                    bot.last_url_ok = diag.get("url_ok")
                    bot.last_webhook_ok = diag.get("webhook_ok")
                    bot.last_reason = json.dumps(diag.get("reasons", {}), ensure_ascii=False)
                except Exception:
                    pass

                add_log(
                    f"📋 Diagnóstico {bot.name}: "
                    f"token_ok={diag['token_ok']}, url_ok={diag['url_ok']}, "
                    f"probe_ok={diag['probe_ok']}, webhook_ok={diag['webhook_ok']} "
                    f"| R: {diag['reasons']} | webhook_info={diag['webhook_info']}"
                )

                if diag["decision_ok"]:
                    bot.reset_failures()
                    bot.last_ok = now_utc()
                    safe_commit()
                    add_log(f"✅ {bot.name}: OK")
                    with _state_lock:
                        alert_state[bot.id] = {"last_fail_count": 0, "last_alert_ts": None}
                    continue

                bot.increment_failure()
                metrics["failures_total"] += 1
                fail_cnt = bot.failures or 0
                add_log(f"⚠️ {bot.name}: queda confirmada ({fail_cnt}/{FAIL_THRESHOLD})")

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
                        f"Nome: {bot.name}\nURL: {bot.redirect_url}\nFalhas: {fail_cnt}/{FAIL_THRESHOLD}\n"
                        f"🔑 Token: {diag['reasons'].get('token')}\n🌍 URL: {diag['reasons'].get('url')}\n"
                        f"📡 Probe: {diag['reasons'].get('probe')}\n🔗 Webhook: {diag['reasons'].get('webhook')}"
                    )

                if in_grace:
                    safe_commit()
                    continue

                if fail_cnt >= FAIL_THRESHOLD:
                    bot.mark_reserve()
                    safe_commit()
                    add_log(f"🔁 {bot.name} movido para 'reserva'.")
                    _, reserva_atual = get_bots_from_db()
                    if reserva_atual:
                        novo = reserva_atual[0]
                        novo.mark_active()
                        if safe_commit():
                            metrics["switches_total"] += 1
                            send_whatsapp(
                                "🔄 Substituição Automática",
                                f"❌ {bot.name} caiu\n➡️ ✅ {novo.name} ativo\nNovo URL: {novo.redirect_url}"
                            )
                            add_log(f"✅ Troca concluída: {bot.name} ➜ {novo.name}")
                    else:
                        send_whatsapp("❌ Falha Crítica", "Não há mais bots na reserva!")

            elapsed = (now_utc() - cycle_started).total_seconds()
            time.sleep(max(1.0, interval - elapsed))

# ================================
# Bootstrap (garante schema atualizado)
# ================================
def _apply_bootstrap_patches():
    with app.app_context():
        try:
            with db.engine.begin() as conn:
                # garante todas as colunas usadas no monitor e dashboard (TIMESTAMPTZ para coerência com timezone-aware)
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS redirect_url TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_ok TIMESTAMPTZ NULL"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS failures INTEGER DEFAULT 0"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_reason TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_ok BOOLEAN"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_token_http INTEGER"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_url_http INTEGER"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_url TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error TEXT"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS last_webhook_error_at TIMESTAMPTZ"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS pending_update_count INTEGER"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ"))
                conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ"))

                # índices úteis e idempotentes (PostgreSQL)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_class c
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE c.relkind = 'i' AND c.relname = 'idx_status_failures'
                        ) THEN
                            CREATE INDEX idx_status_failures ON bots (status, failures);
                        END IF;

                        IF NOT EXISTS (
                            SELECT 1 FROM pg_class c
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE c.relkind = 'i' AND c.relname = 'idx_name_status'
                        ) THEN
                            CREATE INDEX idx_name_status ON bots (name, status);
                        END IF;

                        IF NOT EXISTS (
                            SELECT 1 FROM pg_class c
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE c.relkind = 'i' AND c.relname = 'idx_failures_updated'
                        ) THEN
                            CREATE INDEX idx_failures_updated ON bots (failures, updated_at);
                        END IF;
                    END$$;
                """))
            add_log("✅ Patch no schema aplicado")
        except Exception as e:
            add_log(f"⚠️ Patch falhou (ignorado se não-Postgres): {e}")

# ================================
# Controle do Monitor
# ================================
_monitor_thread = None
_filelock = None

def _try_acquire_file_lock():
    try:
        import fcntl
        global _filelock
        lock_path = "/tmp/tok4_monitor.lock"
        _filelock = open(lock_path, "w")
        try:
            fcntl.flock(_filelock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            add_log(f"🔁 Monitor já em execução em outro worker: {e}")
            return False
        _filelock.write(f"pid={os.getpid()} ts={time.time()}\n")
        _filelock.flush()
        add_log("🔐 File lock adquirido: monitor exclusivo neste container.")
        return True
    except Exception as e:
        add_log(f"🔁 Monitor já em execução em outro worker: {e}")
        return False

def _start_monitor_background():
    global _monitor_thread
    if not MONITOR_ENABLED:
        add_log("⏸ MONITOR_DISABLED.")
        return
    if _monitor_thread and _monitor_thread.is_alive():
        return
    if not _try_acquire_file_lock():
        return
    _monitor_thread = threading.Thread(target=monitor_loop, args=(MONITOR_INTERVAL,), daemon=True, name="tok4-monitor")
    _monitor_thread.start()
    add_log("🧵 Thread de monitoramento iniciada.")

# ================================
# Rotas Dashboard/API (CRUD completo + utilitários)
# ================================
@app.route("/")
@app.route("/dashboard")
def index():
    return render_template("dashboard.html")

@app.route("/health")
@app.route("/healthz")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/api/metrics", methods=["GET"])
def api_metrics():
    return jsonify(metrics)

@app.route("/api/logs", methods=["GET"])
def api_logs():
    try:
        limit = request.args.get("limit", "200")
        try:
            limit = int(limit)
        except Exception:
            limit = 200
        with _state_lock:
            logs = monitor_logs[-max(1, min(limit, MAX_LOGS)):]
        return jsonify({"logs": logs, "count": len(logs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/diag/<int:bot_id>", methods=["GET"])
def api_diag(bot_id):
    try:
        with _state_lock:
            cached = diag_cache.get(bot_id) or {}
        return jsonify({"bot_id": bot_id, "cached": cached})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots", methods=["GET"])
def api_bots():
    try:
        bots = Bot.query.order_by(Bot.id).all()
        payload = []
        for b in bots:
            d = b.to_dict(with_meta=True)
            cached = diag_cache.get(b.id) or {}
            d["_diag"] = cached.get("diag")
            d["_diag_ts"] = cached.get("when")
            payload.append(d)
        # Inclui logs e last_action para compatibilidade com dashboards
        with _state_lock:
            logs_copy = list(monitor_logs)
        return jsonify({"bots": payload, "logs": logs_copy, "metrics": metrics, "last_action": metrics.get("last_check_ts")})
    except Exception as e:
        _rollback_if_failed_tx(e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots", methods=["POST"])
def create_bot():
    try:
        data = _get_payload()
        name = data.get("name")
        token = data.get("token")
        redirect_url = data.get("redirect_url")
        status = data.get("status", "ativo") or "ativo"

        if not name or not token or not redirect_url:
            return jsonify({"error": "name, token e redirect_url são obrigatórios"}), 400

        new_bot = Bot(name=name, token=token, redirect_url=redirect_url, status=status)
        db.session.add(new_bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao salvar. Verifique logs."}), 500

        add_log(f"➕ Bot {new_bot.name} criado.")
        send_whatsapp("➕ Novo Bot", f"Nome: {new_bot.name}\nURL: {new_bot.redirect_url}")
        return jsonify(new_bot.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["PUT"])
def update_bot(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404

        data = _get_payload()
        if "redirect_url" in data and not data.get("redirect_url"):
            return jsonify({"error": "redirect_url não pode ser vazio"}), 400

        bot.name = data.get("name", bot.name)
        bot.token = data.get("token", bot.token)
        bot.redirect_url = data.get("redirect_url", bot.redirect_url)
        bot.status = data.get("status", bot.status)

        if not safe_commit():
            return jsonify({"error": "Falha ao atualizar. Verifique logs."}), 500

        add_log(f"✏️ Bot {bot.name} atualizado.")
        send_whatsapp("✏️ Bot Atualizado", f"Nome: {bot.name}\nURL: {bot.redirect_url}")
        return jsonify(bot.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        db.session.delete(bot)
        if not safe_commit():
            return jsonify({"error": "Falha ao excluir. Verifique logs."}), 500

        add_log(f"🗑️ Bot {bot.name} excluído.")
        send_whatsapp("🗑️ Bot Excluído", f"Nome: {bot.name}")
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bots/<int:bot_id>/force_swap", methods=["POST"])
def force_swap(bot_id):
    """
    Força a troca do bot especificado (move para reserva) por um bot da fila de reserva (primeiro da lista).
    Mantém a mesma lógica de substituição usada no monitor.
    """
    try:
        atual = Bot.query.get(bot_id)
        if not atual:
            return jsonify({"error": "Bot não encontrado"}), 404

        # Move o bot atual para reserva
        atual.mark_reserve()
        safe_commit()

        # Escolhe um bot da reserva (mais antigo/primeiro)
        _, reserva = get_bots_from_db()
        if not reserva:
            send_whatsapp("❌ Forçar Troca", "Não há bots na reserva!")
            return jsonify({"error": "Não há bots na reserva"}), 409

        novo = reserva[0]
        novo.mark_active()
        if safe_commit():
            metrics["switches_total"] += 1
            send_whatsapp(
                "🔄 Substituição Forçada",
                f"❌ {atual.name} ➜ reserva\n➡️ ✅ {novo.name} ativo\nNovo URL: {novo.redirect_url}"
            )
            add_log(f"✅ Troca forçada concluída: {atual.name} ➜ {novo.name}")
            return jsonify({"ok": True, "from": atual.to_dict(), "to": novo.to_dict()})

        return jsonify({"error": "Falha ao efetivar troca"}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/api/webhookinfo/<int:bot_id>", methods=["GET"])
def api_webhookinfo(bot_id):
    try:
        bot = Bot.query.get(bot_id)
        if not bot:
            return jsonify({"error": "Bot não encontrado"}), 404
        ok, reason, details = check_webhook(bot.token or "")
        return jsonify({"ok": ok, "reason": reason, "details": details})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================
# Lead + Subscribe (enviar lead junto com subscribe + enriquecimento)
# ================================
@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """
    Recebe um lead (name, email, phone, etc), enriquece, registra evento e envia para TYPEBOT_API (se configurado).
    Retorno inclui 'enriched' e status do envio externo.
    """
    try:
        raw = _get_payload()
        enriched = _enrich_lead(raw)
        # log interno
        try:
            log_event("subscribe", enriched)  # se utils.log_event existir
        except Exception:
            pass
        send_info = _send_to_typebot(enriched)
        add_log(f"📝 Subscribe recebido | sent={send_info['sent']} | status={send_info['status']} | err={send_info['error']}")
        return jsonify({"ok": True, "enriched": enriched, "send_result": send_info})
    except Exception as e:
        add_log(f"❌ /api/subscribe erro: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/lead", methods=["POST"])
def api_lead():
    """
    Alias de subscribe, compatível com integrações antigas.
    """
    return api_subscribe()

# ================================
# Inicialização em import (para Gunicorn) + Main local
# ================================
_apply_bootstrap_patches()
_start_monitor_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)