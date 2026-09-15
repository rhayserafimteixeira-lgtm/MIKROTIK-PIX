import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================================================
# CONFIGURACAO
# =========================================================

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
MP_PAYER_EMAIL = os.getenv("MP_PAYER_EMAIL", "rhayr8@gmail.com")

MP_API_BASE = "https://api.mercadopago.com"
REQUEST_TIMEOUT = 20

# SQLite evita perder a liberacao quando o servidor usa mais de um worker.
# Em hospedagens como Render, /tmp e gravavel.
DB_PATH = os.getenv(
    "DB_PATH",
    "/var/data/mikrotik_pix.db" if os.path.isdir("/var/data") else "/tmp/mikrotik_pix.db",
)

PLANOS = {
    "30min": {"nome": "30 minutos", "valor": "5.00", "minutos": 30, "horas": 0.5},
    "2h": {"nome": "2 horas", "valor": "10.00", "minutos": 120, "horas": 2},
    "3h": {"nome": "3 horas", "valor": "15.00", "minutos": 180, "horas": 3},
    "5h": {"nome": "5 horas", "valor": "20.00", "minutos": 300, "horas": 5},
}

# Codigos exclusivos da equipe: cada codigo fica preso ao primeiro MAC.
EQUIPE_CODIGOS = {
    "WPX-HFWUVCSN": "EQUIPE01",
    "WPX-LDRCAAGX": "EQUIPE02",
    "WPX-SF65JL8W": "EQUIPE03",
    "WPX-AG37V52K": "EQUIPE04",
    "WPX-VUPNQYTP": "EQUIPE05",
    "WPX-6J69SE45": "EQUIPE06",
    "WPX-FTRQBUD4": "EQUIPE07",
    "WPX-9FQ3HY4J": "EQUIPE08",
}


# =========================================================
# BANCO LOCAL DE LIBERACOES
# =========================================================

def db_conectar():
    conexao = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )
    conexao.row_factory = sqlite3.Row
    return conexao


def db_inicializar():
    with db_conectar() as conexao:
        conexao.execute("""
            CREATE TABLE IF NOT EXISTS liberacoes (
                order_id TEXT PRIMARY KEY,
                mac TEXT NOT NULL,
                ip TEXT NOT NULL,
                plano TEXT NOT NULL,
                horas REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
                criado_em TEXT NOT NULL,
                confirmado_em TEXT
            )
        """)

        conexao.execute("""
            CREATE INDEX IF NOT EXISTS idx_liberacoes_status
            ON liberacoes(status)
        """)

        conexao.execute("""
            CREATE INDEX IF NOT EXISTS idx_liberacoes_mac
            ON liberacoes(mac)
        """)

        # Fila de acessos temporarios. Ela e criada somente depois
        # que o Mercado Pago devolve uma order PIX valida.
        conexao.execute("""
            CREATE TABLE IF NOT EXISTS acessos_temporarios (
                order_id TEXT PRIMARY KEY,
                mac TEXT NOT NULL,
                ip TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
                criado_em TEXT NOT NULL,
                confirmado_em TEXT
            )
        """)

        conexao.execute("""
            CREATE INDEX IF NOT EXISTS idx_acessos_temporarios_status
            ON acessos_temporarios(status)
        """)

        conexao.execute("""
            CREATE TABLE IF NOT EXISTS acessos_equipe (
                codigo TEXT PRIMARY KEY,
                vaga TEXT NOT NULL,
                mac TEXT NOT NULL UNIQUE,
                ip TEXT,
                criado_em TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente'
            )
        """)


def db_limpar_antigas():
    limite = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).isoformat()

    with db_conectar() as conexao:
        conexao.execute(
            """
            DELETE FROM liberacoes
            WHERE criado_em < ?
            """,
            (limite,),
        )

        conexao.execute(
            """
            DELETE FROM acessos_temporarios
            WHERE criado_em < ?
            """,
            (limite,),
        )


db_inicializar()


# =========================================================
# FUNCOES AUXILIARES
# =========================================================

def mp_headers(json_body=False):
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
    }

    if json_body:
        headers["Content-Type"] = "application/json"

    return headers


def normalizar_mac(mac):
    mac_limpo = (
        (mac or "")
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .strip()
        .upper()
    )

    if len(mac_limpo) != 12:
        return ""

    if any(
        caractere not in "0123456789ABCDEF"
        for caractere in mac_limpo
    ):
        return ""

    return ":".join(
        mac_limpo[i:i + 2]
        for i in range(0, 12, 2)
    )


def consultar_order(order_id):
    url = f"{MP_API_BASE}/v1/orders/{order_id}"

    resposta = requests.get(
        url,
        headers=mp_headers(),
        timeout=REQUEST_TIMEOUT,
    )

    try:
        dados = resposta.json()
    except ValueError:
        dados = {
            "erro": "Resposta invalida do Mercado Pago",
            "texto": resposta.text[:500],
        }

    return resposta, dados


def order_esta_paga(dados):
    """
    Somente libera quando o Mercado Pago informa:
    status = processed
    status_detail = accredited
    """
    return (
        dados.get("status") == "processed"
        and dados.get("status_detail") == "accredited"
    )


def dados_da_referencia(referencia):
    """
    Formato usado por este sistema:
    mikrotik_<plano>_<mac-sem-separador>_<ip-com-hifen>_<id>
    """
    if not referencia:
        return None

    if not referencia.startswith("mikrotik_"):
        return None

    partes = referencia.split("_")

    if len(partes) < 5:
        return None

    plano_id = partes[1]
    mac_cliente = normalizar_mac(partes[2])
    ip_cliente = partes[3].replace("-", ".")

    if plano_id not in PLANOS:
        return None

    if not mac_cliente:
        return None

    if not ip_cliente:
        return None

    return {
        "plano": plano_id,
        "horas": PLANOS[plano_id]["horas"],
        "mac": mac_cliente,
        "ip": ip_cliente,
    }


def registrar_liberacao(dados_order, order_id):
    """
    Registra uma liberacao apenas quando a order foi realmente
    processada e creditada pelo Mercado Pago.
    """
    if not order_esta_paga(dados_order):
        return False

    referencia = dados_order.get(
        "external_reference",
        "",
    )

    cliente = dados_da_referencia(
        referencia
    )

    if not cliente:
        return False

    agora = datetime.now(timezone.utc).isoformat()

    with db_conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO liberacoes (
                order_id,
                mac,
                ip,
                plano,
                horas,
                status,
                criado_em
            )
            VALUES (?, ?, ?, ?, ?, 'pendente', ?)
            ON CONFLICT(order_id) DO NOTHING
            """,
            (
                order_id,
                cliente["mac"],
                cliente["ip"],
                cliente["plano"],
                cliente["horas"],
                agora,
            ),
        )

    print(
        "LIBERACAO REGISTRADA | "
        f"MAC={cliente['mac']} | "
        f"IP={cliente['ip']} | "
        f"PLANO={cliente['plano']} | "
        f"HORAS={cliente['horas']} | "
        f"ORDER={order_id}",
        flush=True,
    )

    return True


def registrar_acesso_temporario(order_id, mac, ip):
    """
    Coloca o cliente na fila de internet temporaria SOMENTE depois
    que uma order PIX valida foi criada.
    """
    mac = normalizar_mac(mac)

    if not order_id or not mac or not ip:
        return False

    agora = datetime.now(timezone.utc).isoformat()

    with db_conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO acessos_temporarios (
                order_id,
                mac,
                ip,
                status,
                criado_em
            )
            VALUES (?, ?, ?, 'pendente', ?)
            ON CONFLICT(order_id) DO UPDATE SET
                mac = excluded.mac,
                ip = excluded.ip,
                status = 'pendente',
                criado_em = excluded.criado_em,
                confirmado_em = NULL
            """,
            (
                order_id,
                mac,
                ip,
                agora,
            ),
        )

    print(
        "ACESSO TEMPORARIO SOLICITADO | "
        f"MAC={mac} | IP={ip} | ORDER={order_id}",
        flush=True,
    )

    return True


def buscar_proximo_acesso_temporario():
    db_limpar_antigas()

    with db_conectar() as conexao:
        linha = conexao.execute(
            """
            SELECT *
            FROM acessos_temporarios
            WHERE status = 'pendente'
            ORDER BY criado_em ASC
            LIMIT 1
            """
        ).fetchone()

    return linha


def confirmar_acesso_temporario_db(mac, order_id):
    mac = normalizar_mac(mac)
    agora = datetime.now(timezone.utc).isoformat()

    if not mac or not order_id:
        return None

    with db_conectar() as conexao:
        linha = conexao.execute(
            """
            SELECT *
            FROM acessos_temporarios
            WHERE order_id = ?
              AND mac = ?
              AND status = 'pendente'
            """,
            (order_id, mac),
        ).fetchone()

        if not linha:
            return None

        conexao.execute(
            """
            UPDATE acessos_temporarios
            SET status = 'confirmada',
                confirmado_em = ?
            WHERE order_id = ?
            """,
            (
                agora,
                order_id,
            ),
        )

    return linha


def buscar_liberacao_por_order(order_id):
    with db_conectar() as conexao:
        linha = conexao.execute(
            """
            SELECT *
            FROM liberacoes
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()

    return linha


def buscar_proxima_liberacao():
    db_limpar_antigas()

    with db_conectar() as conexao:
        linha = conexao.execute(
            """
            SELECT *
            FROM liberacoes
            WHERE status = 'pendente'
            ORDER BY criado_em ASC
            LIMIT 1
            """
        ).fetchone()

    return linha


def confirmar_liberacao_db(mac, order_id=""):
    agora = datetime.now(timezone.utc).isoformat()

    with db_conectar() as conexao:
        if order_id:
            linha = conexao.execute(
                """
                SELECT *
                FROM liberacoes
                WHERE order_id = ?
                  AND mac = ?
                  AND status = 'pendente'
                """,
                (order_id, mac),
            ).fetchone()
        else:
            linha = conexao.execute(
                """
                SELECT *
                FROM liberacoes
                WHERE mac = ?
                  AND status = 'pendente'
                ORDER BY criado_em ASC
                LIMIT 1
                """,
                (mac,),
            ).fetchone()

        if not linha:
            return None

        conexao.execute(
            """
            UPDATE liberacoes
            SET status = 'confirmada',
                confirmado_em = ?
            WHERE order_id = ?
            """,
            (
                agora,
                linha["order_id"],
            ),
        )

    return linha



# =========================================================
# ACESSO PERMANENTE DA EQUIPE
# =========================================================

@app.route("/acesso-equipe", methods=["GET", "POST"])
def acesso_equipe():
    mac = normalizar_mac(request.values.get("mac", ""))
    ip = request.values.get("ip", "").strip()
    mensagem = ""
    classe = ""

    if request.method == "POST":
        codigo = request.form.get("codigo", "").strip().upper()
        vaga = EQUIPE_CODIGOS.get(codigo)

        if not vaga or not mac:
            mensagem, classe = "Código ou aparelho inválido.", "erro"
        else:
            agora = datetime.now(timezone.utc).isoformat()
            with db_conectar() as conexao:
                por_codigo = conexao.execute(
                    "SELECT * FROM acessos_equipe WHERE codigo=?", (codigo,)
                ).fetchone()
                por_mac = conexao.execute(
                    "SELECT * FROM acessos_equipe WHERE mac=?", (mac,)
                ).fetchone()

                if por_codigo and por_codigo["mac"] != mac:
                    mensagem, classe = "Este código já pertence a outro aparelho.", "erro"
                elif por_codigo or por_mac:
                    mensagem, classe = "Este aparelho já está autorizado.", "ok"
                else:
                    conexao.execute(
                        """INSERT INTO acessos_equipe
                        (codigo,vaga,mac,ip,criado_em,status)
                        VALUES (?,?,?,?,?,'pendente')""",
                        (codigo, vaga, mac, ip, agora),
                    )
                    mensagem, classe = "Código aceito. Liberando este aparelho...", "ok"

    return f"""<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wi-Fi Pix - Equipe</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#000;color:#fff;font-family:Arial}}
.app{{max-width:430px;min-height:100vh;margin:auto;padding:55px 20px;text-align:center;background:radial-gradient(circle at 50% 0,#07253a,#02090e 40%,#000 75%)}}
.lock{{font-size:48px}}h1{{font-size:27px}}p{{color:#cfefff;font-size:13px}}
input{{width:100%;padding:16px;border:2px solid #00d9ff;border-radius:12px;background:#07131c;color:#fff;text-align:center;font-size:19px;font-weight:bold}}
button{{width:100%;margin-top:14px;padding:16px;border:2px solid #74ff00;border-radius:14px;background:#118d00;color:#fff;font-size:17px;font-weight:bold}}
.msg{{margin-top:20px;font-weight:bold}}.ok{{color:#74ff00}}.erro{{color:#ff5252}}
</style></head><body><div class="app"><div class="lock">🔒</div>
<h1>ACESSO DA EQUIPE</h1><p>Digite o código exclusivo deste aparelho.</p>
<form method="post"><input type="hidden" name="mac" value="{mac}">
<input type="hidden" name="ip" value="{ip}">
<input name="codigo" autocomplete="off" placeholder="CÓDIGO DA EQUIPE" required>
<button type="submit">LIBERAR ESTE APARELHO</button></form>
<div class="msg {classe}">{mensagem}</div></div></body></html>""", 200


@app.route("/equipe-pendente", methods=["GET"])
def equipe_pendente():
    with db_conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM acessos_equipe WHERE status='pendente' ORDER BY criado_em ASC LIMIT 1"
        ).fetchone()
    if not linha:
        return jsonify({"ok": True, "pendente": False}), 200
    return jsonify({"ok": True, "pendente": True, "vaga": linha["vaga"],
                    "mac": linha["mac"], "ip": linha["ip"] or ""}), 200


@app.route("/confirmar-equipe", methods=["GET", "POST"])
def confirmar_equipe():
    mac = normalizar_mac(request.values.get("mac", ""))
    if not mac:
        return jsonify({"ok": False, "erro": "MAC inválido"}), 400
    with db_conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM acessos_equipe WHERE mac=? AND status='pendente'", (mac,)
        ).fetchone()
        if not linha:
            return jsonify({"ok": False, "erro": "Acesso pendente não encontrado"}), 404
        conexao.execute(
            "UPDATE acessos_equipe SET status='confirmada' WHERE mac=?", (mac,)
        )
    return jsonify({"ok": True, "confirmado": True, "mac": mac}), 200


# =========================================================
# PAGINA PRINCIPAL / SAUDE
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "Mikrotik Hotspot", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "servico": "mikrotik-pix",
    }), 200


# =========================================================
# WEBHOOK MERCADO PAGO
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    O webhook recebe a notificacao do Mercado Pago.
    Para nao confiar somente no corpo recebido, o sistema pega
    o data.id e consulta a order diretamente na API oficial.
    """
    try:
        dados_webhook = (
            request.get_json(silent=True)
            or {}
        )

        print(
            "Webhook recebido:",
            dados_webhook,
            flush=True,
        )

        data_id = request.args.get(
            "data.id"
        )

        if not data_id:
            data = dados_webhook.get(
                "data",
                {},
            )

            if isinstance(data, dict):
                data_id = data.get("id")

        tipo = (
            request.args.get("type")
            or dados_webhook.get("type")
            or ""
        )

        action = dados_webhook.get(
            "action",
            "",
        )

        print(
            f"WEBHOOK | TIPO={tipo} | "
            f"ACTION={action} | "
            f"ID={data_id}",
            flush=True,
        )

        if (
            tipo == "order"
            or action.startswith("order.")
        ):
            if not data_id:
                return jsonify({
                    "status": "received",
                    "type": "order",
                    "aviso": "data.id ausente",
                }), 200

            if not MP_ACCESS_TOKEN:
                print(
                    "MP_ACCESS_TOKEN nao configurado",
                    flush=True,
                )

                return jsonify({
                    "status": "received",
                    "type": "order",
                    "id": data_id,
                }), 200

            resposta, dados_order = (
                consultar_order(data_id)
            )

            if resposta.status_code == 200:
                registrar_liberacao(
                    dados_order,
                    data_id,
                )
            else:
                print(
                    "Falha ao consultar order | "
                    f"HTTP={resposta.status_code} | "
                    f"DADOS={dados_order}",
                    flush=True,
                )

            return jsonify({
                "status": "received",
                "type": "order",
                "id": data_id,
            }), 200

        # Outros tipos sao recebidos, mas nao liberam internet.
        return jsonify({
            "status": "received",
            "type": tipo,
            "id": data_id,
        }), 200

    except Exception as erro:
        print(
            "Erro no webhook:",
            repr(erro),
            flush=True,
        )

        # Retorna 200 para evitar uma fila infinita de repeticoes.
        # O /status-pix tambem consulta o Mercado Pago e funciona
        # como redundancia para registrar a liberacao.
        return jsonify({
            "status": "received",
            "error": str(erro),
        }), 200


# =========================================================
# CLIENTE - SOLICITAR JANELA TEMPORARIA PARA PAGAMENTO
# =========================================================

@app.route(
    "/solicitar-acesso-temporario",
    methods=["GET", "POST"],
)
def solicitar_acesso_temporario():
    """
    So coloca o cliente na fila dos 2 minutos quando ele toca no
    botao da tela do QR Code. Antes disso, nenhuma internet geral
    e liberada.

    A order e consultada diretamente no Mercado Pago e o MAC/IP
    recebidos precisam ser os mesmos gravados na external_reference.
    """
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado",
            }), 500

        order_id = request.args.get(
            "order_id",
            "",
        ).strip()

        mac = normalizar_mac(
            request.args.get(
                "mac",
                "",
            )
        )

        ip = request.args.get(
            "ip",
            "",
        ).strip()

        if not order_id or not mac or not ip:
            return jsonify({
                "ok": False,
                "erro": "order_id, MAC ou IP invalido",
            }), 400

        resposta, dados_order = consultar_order(
            order_id
        )

        if resposta.status_code != 200:
            return jsonify({
                "ok": False,
                "erro": "Nao foi possivel validar a order no Mercado Pago",
                "status_code": resposta.status_code,
            }), resposta.status_code

        referencia = dados_order.get(
            "external_reference",
            "",
        )

        cliente = dados_da_referencia(
            referencia
        )

        if not cliente:
            return jsonify({
                "ok": False,
                "erro": "Order sem referencia valida para este hotspot",
            }), 400

        if (
            cliente["mac"] != mac
            or cliente["ip"] != ip
        ):
            return jsonify({
                "ok": False,
                "erro": "MAC/IP nao correspondem a order",
            }), 403

        # Se o pagamento ja foi aprovado, nao faz sentido abrir
        # uma janela temporaria. Registra a liberacao definitiva.
        if order_esta_paga(dados_order):
            registrar_liberacao(
                dados_order,
                order_id,
            )

            return jsonify({
                "ok": True,
                "pago": True,
                "solicitado": False,
                "mensagem": "Pagamento ja aprovado. Liberando o plano comprado.",
            }), 200

        status_order = (
            dados_order.get("status")
            or ""
        ).lower()

        if status_order in {
            "cancelled",
            "canceled",
            "failed",
            "expired",
            "rejected",
        }:
            return jsonify({
                "ok": False,
                "erro": "Esta order nao esta mais disponivel para pagamento",
                "status": status_order,
            }), 409

        registrar_acesso_temporario(
            order_id,
            mac,
            ip,
        )

        return jsonify({
            "ok": True,
            "pago": False,
            "solicitado": True,
            "segundos": 120,
            "mensagem": "Internet temporaria solicitada. Abra o banco e conclua o PIX.",
        }), 200

    except Exception as erro:
        print(
            "Erro solicitar-acesso-temporario:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# MIKROTIK - JANELA TEMPORARIA PARA PAGAMENTO
# =========================================================

@app.route(
    "/acesso-temporario-pendente",
    methods=["GET"],
)
def acesso_temporario_pendente():
    """
    O MikroTik consulta esta rota e, quando houver uma order PIX
    recem-criada, libera somente 2 minutos para aquele MAC/IP.
    """
    try:
        dados = buscar_proximo_acesso_temporario()

        if not dados:
            return jsonify({
                "ok": True,
                "pendente": False,
            }), 200

        return jsonify({
            "ok": True,
            "pendente": True,
            "mac": dados["mac"],
            "ip": dados["ip"],
            "order_id": dados["order_id"],
            "segundos": 120,
            "rate_limit": "1M/1M",
        }), 200

    except Exception as erro:
        print(
            "Erro acesso-temporario-pendente:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


@app.route(
    "/confirmar-acesso-temporario",
    methods=["GET", "POST"],
)
def confirmar_acesso_temporario():
    try:
        mac = normalizar_mac(
            request.args.get(
                "mac",
                "",
            )
        )

        order_id = request.args.get(
            "order_id",
            "",
        ).strip()

        if not mac or not order_id:
            return jsonify({
                "ok": False,
                "erro": "MAC ou order_id invalido",
            }), 400

        acesso = confirmar_acesso_temporario_db(
            mac,
            order_id,
        )

        if not acesso:
            return jsonify({
                "ok": False,
                "erro": "Acesso temporario pendente nao encontrado",
            }), 404

        print(
            "ACESSO TEMPORARIO CONFIRMADO PELO MIKROTIK | "
            f"MAC={mac} | ORDER={order_id}",
            flush=True,
        )

        return jsonify({
            "ok": True,
            "confirmado": True,
            "mac": mac,
            "order_id": order_id,
        }), 200

    except Exception as erro:
        print(
            "Erro confirmar-acesso-temporario:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# CONSULTAR STATUS DO PIX
# =========================================================

@app.route(
    "/status-pix/<order_id>",
    methods=["GET"],
)
def status_pix(order_id):
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado",
            }), 500

        resposta, dados = consultar_order(
            order_id
        )

        if resposta.status_code != 200:
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados,
            }), resposta.status_code

        status = dados.get(
            "status",
            "",
        )

        status_detail = dados.get(
            "status_detail",
            "",
        )

        pago = order_esta_paga(
            dados
        )

        if pago:
            registrar_liberacao(
                dados,
                order_id,
            )

        liberacao = buscar_liberacao_por_order(
            order_id
        )

        liberada = bool(
            liberacao
            and liberacao["status"] == "confirmada"
        )

        return jsonify({
            "ok": True,
            "status": status,
            "status_detail": status_detail,
            "pago": pago,
            "liberada": liberada,
        }), 200

    except Exception as erro:
        print(
            "Erro no status PIX:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# MIKROTIK - CONSULTAR LIBERACAO PENDENTE
# =========================================================

@app.route(
    "/liberacao-pendente",
    methods=["GET"],
)
def liberacao_pendente():
    try:
        dados = buscar_proxima_liberacao()

        if not dados:
            return jsonify({
                "ok": True,
                "pendente": False,
            }), 200

        return jsonify({
            "ok": True,
            "pendente": True,
            "mac": dados["mac"],
            "ip": dados["ip"],
            "plano": dados["plano"],
            "horas": dados["horas"],
            "order_id": dados["order_id"],
        }), 200

    except Exception as erro:
        print(
            "Erro liberacao-pendente:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# MIKROTIK - CONFIRMAR LIBERACAO
# =========================================================

@app.route(
    "/confirmar-liberacao",
    methods=["GET", "POST"],
)
def confirmar_liberacao():
    try:
        mac = normalizar_mac(
            request.args.get(
                "mac",
                "",
            )
        )

        order_id = request.args.get(
            "order_id",
            "",
        ).strip()

        if not mac:
            return jsonify({
                "ok": False,
                "erro": "MAC nao informado ou invalido",
            }), 400

        liberacao = confirmar_liberacao_db(
            mac,
            order_id,
        )

        if not liberacao:
            return jsonify({
                "ok": False,
                "erro": "Liberacao pendente nao encontrada",
            }), 404

        print(
            "LIBERACAO CONFIRMADA PELO MIKROTIK | "
            f"MAC={mac} | "
            f"ORDER={liberacao['order_id']}",
            flush=True,
        )

        return jsonify({
            "ok": True,
            "confirmado": True,
            "mac": mac,
            "order_id": liberacao["order_id"],
        }), 200

    except Exception as erro:
        print(
            "Erro confirmar-liberacao:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# CRIAR PIX / ESCOLHER PLANO
# =========================================================

@app.route(
    "/criar-pix",
    methods=["GET"],
)
def criar_pix():
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado",
            }), 500

        # Dados enviados pelo Hotspot MikroTik.
        mac_original = request.args.get(
            "mac",
            "",
        ).strip()

        ip = request.args.get(
            "ip",
            "",
        ).strip()

        link_login = request.args.get(
            "link-login",
            "",
        ).strip()

        link_orig = request.args.get(
            "link-orig",
            "",
        ).strip()

        mac_normalizado = normalizar_mac(
            mac_original
        )

        mac = (
            mac_normalizado
            if mac_normalizado
            else mac_original
        )

        print(
            f"CLIENTE HOTSPOT | "
            f"MAC={mac} | "
            f"IP={ip} | "
            f"LINK_LOGIN={link_login} | "
            f"LINK_ORIG={link_orig}",
            flush=True,
        )

        plano_id = request.args.get(
            "plano",
            "",
        )

        # =====================================================
        # TELA PARA ESCOLHER O PLANO
        # =====================================================

        if plano_id not in PLANOS:

            def link_plano(id_plano):
                query = urlencode({
                    "plano": id_plano,
                    "mac": mac,
                    "ip": ip,
                    "link-login": link_login,
                    "link-orig": link_orig,
                })

                return (
                    f"/criar-pix?{query}"
                )

            link_equipe = "/acesso-equipe?" + urlencode({"mac": mac, "ip": ip})

            pagina_planos = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Wi-Fi Pix - Planos</title>
<style>
* {{box-sizing:border-box}}
:root {{--cyan:#00d9ff;--blue:#007bff;--lime:#74ff00;--yellow:#ffe600;--purple:#9b18ff;--bg:#02070b}}
body {{margin:0;min-height:100vh;font-family:Arial,Helvetica,sans-serif;background:#000;color:#fff}}
.app {{width:100%;max-width:430px;min-height:100vh;margin:auto;background:radial-gradient(circle at 50% -10%,#07253a 0,#02090e 34%,#000 70%);padding-bottom:22px}}
.bar {{height:47px;border-bottom:1px solid #008cff;box-shadow:0 2px 12px #007cff66;display:flex;align-items:center;justify-content:space-between;padding:0 18px;font-size:15px}}
.brand {{display:flex;align-items:center;gap:9px}} .wifiicon {{font-size:25px;color:#00eaff}} .signal {{color:#d8f7ff;font-size:20px}}
.content {{padding:14px 14px 0}}
h1 {{font-size:27px;font-weight:1000;font-style:italic;margin:0;text-align:center;letter-spacing:-1px}}
.sub {{font-size:14px;text-align:center;margin:3px 0 17px;color:#eee}}
.plan {{display:flex;align-items:center;min-height:94px;border-radius:14px;padding:12px 14px;margin:10px 0;text-decoration:none;color:#fff;position:relative;border:2px solid;box-shadow:0 0 14px currentColor,inset 0 0 22px #ffffff13}}
.clock {{width:42px;height:42px;border:3px solid #fff;border-radius:50%;margin-right:12px;position:relative;flex:0 0 42px}}
.clock:before {{content:"";position:absolute;width:2px;height:12px;background:#fff;left:18px;top:8px;transform-origin:bottom;transform:rotate(0deg)}}
.clock:after {{content:"";position:absolute;width:10px;height:2px;background:#fff;left:18px;top:19px;transform:rotate(35deg);transform-origin:left}}
.info {{flex:1;min-width:0}} .hours {{font-size:22px;font-weight:1000}} .speed {{font-size:13px;font-weight:1000;color:#eaff00;margin-top:2px}}
.desc {{font-size:12px;line-height:1.2;margin-top:2px}} .price {{font-size:20px;font-weight:1000;white-space:nowrap;margin-left:7px}} .arrow {{font-size:34px;margin-left:8px}}
.p1 {{background:linear-gradient(105deg,#002d83,#006cff);border-color:#00c8ff;color:#00bfff}}
.p2 {{background:linear-gradient(105deg,#003d13,#00b71f);border-color:#24ff40;color:#3cff43}}
.p3 {{background:linear-gradient(105deg,#6b4a00,#d69d00);border-color:#ffe900;color:#ffe600}}
.p4 {{background:linear-gradient(105deg,#28005e,#7100bb);border-color:#c426ff;color:#bd27ff}}
.plan * {{color:#fff}} .p1 .speed,.p2 .speed,.p3 .speed,.p4 .speed {{color:#eaff00}}
.features {{display:flex;justify-content:space-around;text-align:center;margin:27px 0 30px}}
.feature {{width:30%;font-size:11px;line-height:1.25}} .round {{width:45px;height:45px;border:2px solid #00c8ff;border-radius:50%;margin:0 auto 8px;display:flex;align-items:center;justify-content:center;font-size:23px;box-shadow:0 0 12px #00bfff}}
.feature:nth-child(2) .round {{border-color:#72ff00;box-shadow:0 0 12px #72ff00}} .feature:nth-child(3) .round {{border-color:#00c8ff}}
.back {{display:inline-flex;align-items:center;gap:15px;border:1px solid #00a7ff;border-radius:6px;padding:11px 20px;color:#fff;text-decoration:none;font-weight:800;font-size:12px}}
.signature {{float:right;color:#7dff00;font-size:25px;font-style:italic;margin:8px 8px 0 0}} .teamaccess{{display:block;clear:both;padding-top:20px;text-align:center;color:#8ea6b3;text-decoration:none;font-size:11px}}
</style>
</head>
<body><div class="app">
<div class="bar"><div class="brand"><span class="wifiicon">◉</span><span>Wi-Fi Pix</span></div><span class="signal">◔</span></div>
<div class="content">
<h1>ESCOLHA SEU PLANO</h1><div class="sub">Internet de qualidade para você aproveitar<br>o evento sem limites.</div>
<a class="plan p1" href="{link_plano('30min')}"><div class="clock"></div><div class="info"><div class="hours">30 MINUTOS</div><div class="speed">1 a 2 Megas</div><div class="desc">Apenas WhatsApp e apps<br>de pagamento.</div></div><div class="price">R$ 5,00</div><div class="arrow">›</div></a>
<a class="plan p2" href="{link_plano('2h')}"><div class="clock"></div><div class="info"><div class="hours">2 HORAS</div><div class="speed">1 a 2 Megas</div><div class="desc">Apenas WhatsApp e apps<br>de pagamento.</div></div><div class="price">R$ 10,00</div><div class="arrow">›</div></a>
<a class="plan p3" href="{link_plano('3h')}"><div class="clock"></div><div class="info"><div class="hours">3 HORAS</div><div class="speed">3 a 5 Megas</div><div class="desc"><b>Acesso completo</b><br>Redes sociais liberadas<br>(WhatsApp, Instagram, TikTok, etc).</div></div><div class="price">R$ 15,00</div><div class="arrow">›</div></a>
<a class="plan p4" href="{link_plano('5h')}"><div class="clock"></div><div class="info"><div class="hours">5 HORAS</div><div class="speed">3 a 5 Megas</div><div class="desc"><b>Acesso completo</b><br>Redes sociais liberadas<br>(WhatsApp, Instagram, TikTok, etc).</div></div><div class="price">R$ 20,00</div><div class="arrow">›</div></a>
<div class="features"><div class="feature"><div class="round">∞</div>Sem cadastro<br>complicado</div><div class="feature"><div class="round">✓</div>Pagamento<br>seguro</div><div class="feature"><div class="round">➤</div>Conecte-se<br>e aproveite</div></div>
<a class="back" href="javascript:history.back()">‹ &nbsp;&nbsp; VOLTAR</a><div class="signature">Wi-Fi Pix</div><a class="teamaccess" href="{link_equipe}">🔒 Acesso da equipe</a>
</div></div></body></html>
"""
            return pagina_planos, 200

        # =====================================================
        # PLANO ESCOLHIDO
        # =====================================================

        plano = PLANOS[
            plano_id
        ]

        valor = plano[
            "valor"
        ]

        nome_plano = plano[
            "nome"
        ]

        horas = plano[
            "horas"
        ]

        if not mac_normalizado:
            return jsonify({
                "ok": False,
                "erro": "MAC do cliente nao informado ou invalido",
            }), 400

        if not ip:
            return jsonify({
                "ok": False,
                "erro": "IP do cliente nao informado",
            }), 400

        # =====================================================
        # CRIAR ORDER PIX NO MERCADO PAGO
        # =====================================================

        url = (
            f"{MP_API_BASE}/v1/orders"
        )

        headers = mp_headers(
            json_body=True
        )

        headers[
            "X-Idempotency-Key"
        ] = str(
            uuid.uuid4()
        )

        mac_ref = (
            mac_normalizado
            .replace(":", "")
        )

        ip_ref = ip.replace(
            ".",
            "-",
        )

        referencia = (
            f"mikrotik_{plano_id}_"
            f"{mac_ref}_{ip_ref}_"
            f"{uuid.uuid4().hex[:8]}"
        )

        pedido = {
            "type": "online",
            "processing_mode": "automatic",
            "external_reference": referencia,
            "total_amount": valor,

            "payer": {
                "email": MP_PAYER_EMAIL,
            },

            "transactions": {
                "payments": [
                    {
                        "amount": valor,

                        "payment_method": {
                            "id": "pix",
                            "type": "bank_transfer",
                        },
                    }
                ]
            },
        }

        resposta = requests.post(
            url,
            headers=headers,
            json=pedido,
            timeout=REQUEST_TIMEOUT,
        )

        try:
            dados = resposta.json()
        except ValueError:
            dados = {
                "erro": "Resposta invalida do Mercado Pago",
                "texto": resposta.text[:500],
            }

        print(
            "Resposta criacao PIX:",
            dados,
            flush=True,
        )

        if resposta.status_code not in (
            200,
            201,
        ):
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados,
            }), resposta.status_code

        pagamentos = (
            dados
            .get(
                "transactions",
                {},
            )
            .get(
                "payments",
                [],
            )
        )

        if not pagamentos:
            return jsonify({
                "ok": False,
                "erro": "Order criada sem pagamento",
                "order": dados,
            }), 500

        pagamento = pagamentos[0]

        metodo = pagamento.get(
            "payment_method",
            {},
        )

        qr_code = metodo.get(
            "qr_code",
            "",
        )

        qr_code_base64 = metodo.get(
            "qr_code_base64",
            "",
        )

        order_id = dados.get(
            "id",
            "",
        )

        if not order_id:
            return jsonify({
                "ok": False,
                "erro": "Mercado Pago nao retornou o ID da order",
                "order": dados,
            }), 500

        if (
            not qr_code
            and not qr_code_base64
        ):
            return jsonify({
                "ok": False,
                "erro": "Mercado Pago nao retornou QR Code PIX",
                "order": dados,
            }), 500

        # =====================================================
        # TELA DO QR CODE PIX
        # =====================================================

        imagem_qr = ""

        if qr_code_base64:
            imagem_qr = f"""
<img
    src="data:image/png;base64,{qr_code_base64}"
    alt="QR Code PIX"
>
"""

        query_acesso_temporario = urlencode({
            "order_id": order_id,
            "mac": mac_normalizado,
            "ip": ip,
        })

        url_acesso_temporario = (
            f"/solicitar-acesso-temporario?{query_acesso_temporario}"
        )

        pagina = f"""
<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Wi-Fi Pix - Pagamento</title>
<style>
* {{box-sizing:border-box}} :root{{--cyan:#00d9ff;--lime:#63ff00;--yellow:#ffe600}}
body{{margin:0;min-height:100vh;font-family:Arial,Helvetica,sans-serif;background:#000;color:#fff}}
.app{{width:100%;max-width:430px;min-height:100vh;margin:auto;background:radial-gradient(circle at 50% -10%,#07253a 0,#02090e 34%,#000 72%);padding-bottom:22px}}
.bar{{height:47px;border-bottom:1px solid #008cff;box-shadow:0 2px 12px #007cff66;display:flex;align-items:center;justify-content:space-between;padding:0 18px;font-size:15px}}
.brand{{display:flex;align-items:center;gap:9px}} .wifiicon{{font-size:25px;color:#00eaff}} .lock{{font-size:17px}}
.content{{padding:15px 15px 0;text-align:center}} h1{{font-size:23px;font-weight:1000;margin:0 0 4px}} .lead{{font-size:13px;margin-bottom:13px}}
.summary{{border:2px solid #ffe600;border-radius:13px;background:linear-gradient(90deg,#5d4d00,#9c7800);box-shadow:0 0 16px #ffe600;padding:11px 13px;display:flex;align-items:center;text-align:left;margin-bottom:17px}}
.clock{{font-size:31px;margin-right:12px}} .sumname{{flex:1;font-size:12px}} .sumname b{{display:block;font-size:18px;margin-top:2px}} .value{{font-size:20px;font-weight:1000}}
.qrframe{{width:205px;min-height:205px;margin:0 auto 12px;border:3px solid #00e5ff;border-radius:15px;padding:9px;background:#fff;box-shadow:0 0 15px #00c8ff}} .qrframe img{{display:block;width:100%;height:auto;margin:0}}
.pixline{{height:38px;border:1px solid #006fa7;background:#06131f;border-radius:9px;display:flex;align-items:center;padding:0 10px;margin-bottom:9px}} textarea{{flex:1;height:25px;border:0;resize:none;background:transparent;color:#fff;font-size:10px;outline:0;white-space:nowrap;overflow:hidden}} .copymini{{font-size:18px}}
button{{width:100%;border-radius:14px;padding:14px 10px;border:2px solid;font-size:17px;font-weight:1000;cursor:pointer;color:#fff;margin:7px 0}}
.copy{{background:linear-gradient(90deg,#007bd8,#006cff);border-color:#00eaff;box-shadow:0 0 15px #00d9ff}}
.temp{{background:linear-gradient(90deg,#087800,#14ad00);border-color:#72ff00;box-shadow:0 0 15px #56ff00}} .temp:disabled{{opacity:.7}}
.auto{{font-size:12px;margin:5px 0 14px}} .tip{{display:flex;text-align:left;gap:9px;border:1px solid #007ab7;border-radius:9px;padding:10px;color:#dbefff;font-size:11px;line-height:1.35}} .tip strong{{font-size:20px;color:#00d9ff}}
.status{{margin-top:10px;color:#ffe600;font-size:12px;font-weight:800}} .backrow{{margin-top:18px;text-align:left}} .back{{display:inline-flex;border:1px solid #008bd0;border-radius:6px;padding:10px 17px;color:#fff;text-decoration:none;font-size:12px;font-weight:800}} .signature{{float:right;color:#76ff00;font-size:24px;font-style:italic;margin-top:7px}}
</style></head><body><div class="app">
<div class="bar"><div class="brand"><span class="wifiicon">◉</span><span>Wi-Fi Pix</span></div><span class="lock">♙</span></div>
<div class="content"><h1>PAGAMENTO VIA PIX</h1><div class="lead">Escaneie o QR Code ou copie a chave PIX.</div>
<div class="summary"><div class="clock">◷</div><div class="sumname">Plano selecionado:<b>{nome_plano.upper()}</b></div><div class="value">R$ {valor.replace('.', ',')}</div></div>
<div class="qrframe">{imagem_qr}</div>
<div class="pixline"><textarea id="pix" readonly>{qr_code}</textarea><span class="copymini">▣</span></div>
<button class="copy" onclick="copiarPix()">▣ &nbsp; COPIAR PIX</button>
<div class="auto">Após o pagamento, sua internet será<br>liberada automaticamente.</div>
<button id="btn-acesso-temporario" class="temp" onclick="liberarInternetPagamento()">◉ &nbsp; LIBERAR 2 MINUTOS GRÁTIS<br><small>PARA PAGAR</small></button>
<div class="tip" id="aviso-temporario"><strong>ⓘ</strong><span>Sem acesso ao 4G/5G?<br>Use os 2 minutos grátis para abrir o app do banco e realizar o pagamento.</span></div>
<div class="status" id="status-pagamento">Aguardando pagamento...</div>
<div class="backrow"><a class="back" href="javascript:history.back()">‹ &nbsp;&nbsp; VOLTAR</a><div class="signature">Wi-Fi Pix</div></div>
</div></div>
<script>
function copiarPix(){{const codigo=document.getElementById('pix').value;navigator.clipboard.writeText(codigo).then(function(){{alert('Código PIX copiado!');}});}}
async function liberarInternetPagamento(){{const botao=document.getElementById('btn-acesso-temporario');const aviso=document.getElementById('aviso-temporario');botao.disabled=true;botao.textContent='LIBERANDO...';try{{const resposta=await fetch('{url_acesso_temporario}',{{method:'GET',cache:'no-store'}});const dados=await resposta.json();if(dados.ok&&dados.pago){{aviso.innerHTML='<strong>✓</strong><span>Pagamento já aprovado. Liberando o plano comprado...</span>';botao.textContent='PAGAMENTO APROVADO';return;}}if(!resposta.ok||!dados.ok)throw new Error(dados.erro||'Falha');botao.textContent='2 MINUTOS SOLICITADOS';aviso.innerHTML='<strong>✓</strong><span>Solicitação enviada. Abra o aplicativo do banco e conclua o PIX. A liberação será feita pelo Wi-Fi Pix.</span>';setTimeout(function(){{botao.disabled=false;botao.textContent='SOLICITAR 2 MINUTOS NOVAMENTE';}},150000);}}catch(erro){{botao.disabled=false;botao.textContent='TENTAR LIBERAR 2 MINUTOS NOVAMENTE';aviso.innerHTML='<strong>!</strong><span>Não foi possível solicitar os 2 minutos. Tente novamente.</span>';}}}}
async function verificarPagamento(){{try{{const resposta=await fetch('/status-pix/{order_id}',{{cache:'no-store'}});const dados=await resposta.json();const tela=document.getElementById('status-pagamento');if(dados.ok&&dados.pago&&dados.liberada){{tela.textContent='PAGAMENTO APROVADO! INTERNET LIBERADA.';tela.style.color='#63ff00';clearInterval(timerPagamento);}}else if(dados.ok&&dados.pago)tela.textContent='Pagamento aprovado! Liberando internet...';else if(dados.ok)tela.textContent='Aguardando pagamento...';}}catch(erro){{console.log(erro);}}}}
let timerPagamento=setInterval(verificarPagamento,5000);verificarPagamento();
</script></body></html>
"""
        return pagina, 200

    except Exception as erro:
        print(
            "Erro ao criar PIX:",
            repr(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# EXECUCAO LOCAL
# =========================================================

if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
