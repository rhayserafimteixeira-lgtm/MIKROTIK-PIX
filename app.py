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
DB_PATH = os.getenv("DB_PATH", "/tmp/mikrotik_pix.db")

PLANOS = {
    "1h": {"nome": "1 hora", "valor": "5.00", "horas": 1},
    "2h": {"nome": "2 horas", "valor": "10.00", "horas": 2},
    "5h": {"nome": "5 horas", "valor": "15.00", "horas": 5},
    "10h": {"nome": "10 horas", "valor": "20.00", "horas": 10},
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
                horas INTEGER NOT NULL,
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
            ON CONFLICT(order_id) DO NOTHING
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

            pagina_planos = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Wi-Fi Pix - Planos</title>
<style>
* {{ box-sizing:border-box }}
body {{ margin:0; min-height:100vh; font-family:Arial,sans-serif; background:linear-gradient(160deg,#061a31,#0a2b4b); color:#142033; padding:18px 14px 26px }}
.app {{ max-width:430px; margin:auto }}
.topo {{ text-align:center; color:#fff; padding:8px 0 16px }}
.marca {{ font-size:38px; font-weight:900; letter-spacing:-1.5px }}
.marca .wifi {{ color:#3b82f6 }} .marca .pix {{ color:#32c39a }}
.topo p {{ margin:6px 0 0; color:#cbd8e7; font-size:15px }}
.painel {{ background:#fff; border-radius:24px; padding:18px; box-shadow:0 16px 45px rgba(0,0,0,.28) }}
.titulo {{ font-size:20px; font-weight:900; margin:0 0 4px }}
.sub {{ color:#6b778c; font-size:13px; margin-bottom:14px }}
.plano {{ display:flex; align-items:center; gap:12px; text-decoration:none; color:#172033; background:#fff; border:1px solid #e5ebf2; border-radius:17px; padding:13px 14px; margin:10px 0; box-shadow:0 5px 16px rgba(13,35,64,.08) }}
.icone {{ width:46px; height:46px; flex:0 0 46px; border-radius:14px; display:flex; align-items:center; justify-content:center; color:#fff; font-size:23px; font-weight:900 }}
.p1 .icone {{ background:linear-gradient(135deg,#247cf2,#42a5f5) }} .p2 .icone {{ background:linear-gradient(135deg,#14a987,#45c79d) }} .p3 .icone {{ background:linear-gradient(135deg,#6b4fd3,#8b5cf6) }} .p4 .icone {{ background:linear-gradient(135deg,#e98725,#f3a72f) }}
.dados {{ flex:1; text-align:left; min-width:0 }} .horas {{ font-size:17px; font-weight:900 }} .beneficio {{ color:#6d7889; font-size:11px; margin-top:3px }}
.preco {{ text-align:right; font-size:22px; font-weight:900; white-space:nowrap }} .preco small {{ display:block; color:#8490a2; font-size:10px; font-weight:700; margin-top:2px }}
.tag {{ display:inline-block; margin-top:5px; padding:3px 7px; border-radius:20px; background:#edf5ff; color:#247cf2; font-size:10px; font-weight:900 }}
.p3 .tag,.p4 .tag {{ background:#f0eaff; color:#7048df }}
.aviso {{ margin-top:14px; border-radius:14px; background:#f4f7fb; padding:12px 13px; text-align:left; color:#5e6a7c; font-size:11px; line-height:1.45 }} .aviso b {{ color:#172033 }}
.rodape {{ text-align:center; color:#9fb0c3; font-size:10px; margin-top:12px }}
</style>
</head>
<body><div class="app">
<div class="topo"><div class="marca"><span class="wifi">Wi-Fi</span> <span class="pix">Pix</span></div><p>Escolha seu plano e conecte em segundos</p></div>
<div class="painel"><div class="titulo">Escolha seu plano</div><div class="sub">Pagamento rápido e seguro via PIX</div>
<a class="plano p1" href="{link_plano('1h')}"><div class="icone">1h</div><div class="dados"><div class="horas">1 hora</div><div class="beneficio">WhatsApp + apps de pagamento</div><span class="tag">1 MEGA</span></div><div class="preco">R$ 5<small>acesso individual</small></div></a>
<a class="plano p2" href="{link_plano('2h')}"><div class="icone">2h</div><div class="dados"><div class="horas">2 horas</div><div class="beneficio">WhatsApp + apps de pagamento</div><span class="tag">2 MEGAS</span></div><div class="preco">R$ 10<small>acesso individual</small></div></a>
<a class="plano p3" href="{link_plano('5h')}"><div class="icone">5h</div><div class="dados"><div class="horas">5 horas</div><div class="beneficio">WhatsApp + pagamentos + redes sociais</div><span class="tag">REDES SOCIAIS</span></div><div class="preco">R$ 15<small>mais completo</small></div></a>
<a class="plano p4" href="{link_plano('10h')}"><div class="icone">10h</div><div class="dados"><div class="horas">10 horas</div><div class="beneficio">WhatsApp + pagamentos + redes sociais</div><span class="tag">REDES SOCIAIS</span></div><div class="preco">R$ 20<small>melhor duração</small></div></a>
<div class="aviso"><b>Importante:</b> planos de R$ 5 e R$ 10 são indicados para WhatsApp e aplicativos de pagamento. Planos de R$ 15 e R$ 20 também liberam acesso às redes sociais.</div>
</div><div class="rodape">Wi-Fi Pix • Internet fácil onde você estiver</div></div></body></html>
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
* {{ box-sizing:border-box }} body {{ margin:0; min-height:100vh; font-family:Arial,sans-serif; background:linear-gradient(160deg,#061a31,#0a2b4b); color:#142033; padding:16px 14px 25px }}
.app {{ max-width:430px; margin:auto }} .topo {{ text-align:center; color:#fff; padding:5px 0 13px }} .marca {{ font-size:34px; font-weight:900; letter-spacing:-1px }} .wifi {{ color:#3b82f6 }} .pixc {{ color:#32c39a }}
.painel {{ background:#fff; border-radius:24px; padding:17px; box-shadow:0 16px 45px rgba(0,0,0,.28) }}
.resumo {{ display:flex; align-items:center; justify-content:space-between; gap:10px; background:#f4f7fb; border-radius:15px; padding:11px 13px; margin-bottom:12px }} .resumo .nome {{ text-align:left; font-size:12px; color:#6b778c }} .resumo .nome b {{ display:block; color:#172033; font-size:17px; margin-top:2px }} .valor {{ color:#20a77e; font-size:26px; font-weight:900; white-space:nowrap }}
.instrucao {{ font-size:12px; color:#697588; margin:4px 0 8px }} img {{ display:block; width:190px; max-width:65vw; margin:8px auto 11px; border-radius:10px }}
.pixbox {{ background:#f7f9fc; border:1px solid #e0e6ee; border-radius:13px; padding:10px }} .pixlabel {{ text-align:left; font-size:11px; font-weight:900; color:#667386; margin-bottom:6px }} textarea {{ width:100%; height:48px; border:0; background:transparent; resize:none; font-size:10px; color:#5f6875; outline:none }}
button {{ width:100%; border:0; border-radius:14px; padding:14px 10px; margin-top:10px; color:#fff; font-size:14px; font-weight:900; cursor:pointer }} .copiar {{ background:linear-gradient(90deg,#13a984,#43c69d) }} .temporario {{ background:linear-gradient(90deg,#247cf2,#42a5f5) }} .temporario:disabled {{ opacity:.65 }}
.dica {{ display:flex; gap:8px; align-items:flex-start; margin-top:10px; background:#eef6ff; color:#526173; border-radius:12px; padding:10px; text-align:left; font-size:10px; line-height:1.4 }} .dica strong {{ color:#247cf2 }}
.status {{ margin-top:10px; border-radius:12px; background:#f4f7fb; padding:10px; font-size:12px; font-weight:900 }} .pedido {{ margin-top:9px; text-align:center; color:#9aa4b2; font-size:9px }}
</style></head><body><div class="app"><div class="topo"><div class="marca"><span class="wifi">Wi-Fi</span> <span class="pixc">Pix</span></div></div><div class="painel">
<div class="resumo"><div class="nome">Seu plano<b>{nome_plano}</b></div><div class="valor">R$ {valor.replace('.', ',')}</div></div>
<p class="instrucao">Escaneie o QR Code ou copie o código PIX abaixo.</p>{imagem_qr}
<div class="pixbox"><div class="pixlabel">PIX COPIA E COLA</div><textarea id="pix" readonly>{qr_code}</textarea></div>
<button class="copiar" onclick="copiarPix()">COPIAR CÓDIGO PIX</button>
<button id="btn-acesso-temporario" class="temporario" onclick="liberarInternetPagamento()">LIBERAR 2 MINUTOS PARA PAGAR</button>
<div class="dica" id="aviso-temporario"><strong>2 MIN</strong><span>Primeiro copie o PIX. Quando estiver pronto para abrir o banco, toque no botão azul. O tempo começa somente nesse momento.</span></div>
<div class="status" id="status-pagamento">Aguardando pagamento...</div><div class="pedido">Pedido {order_id} • {horas} hora(s)</div>
</div></div><script>
function copiarPix() {{ const codigo=document.getElementById('pix').value; navigator.clipboard.writeText(codigo).then(function(){{ alert('Código PIX copiado!'); }}); }}
async function liberarInternetPagamento() {{ const botao=document.getElementById('btn-acesso-temporario'); const aviso=document.getElementById('aviso-temporario'); botao.disabled=true; botao.textContent='LIBERANDO...'; aviso.innerHTML='<strong>2 MIN</strong><span>Solicitando internet temporária ao MikroTik...</span>'; try {{ const resposta=await fetch('{url_acesso_temporario}',{{method:'POST',cache:'no-store'}}); const dados=await resposta.json(); if(dados.ok&&dados.pago){{ aviso.innerHTML='<strong>OK</strong><span>Pagamento já aprovado. Liberando o plano comprado...</span>'; botao.textContent='PAGAMENTO APROVADO'; return; }} if(!resposta.ok||!dados.ok) throw new Error(dados.erro||'Falha'); botao.textContent='2 MINUTOS LIBERADOS'; aviso.innerHTML='<strong>AGORA</strong><span>Abra o aplicativo do banco e conclua o PIX. O acesso temporário será encerrado automaticamente.</span>'; }} catch(erro){{ botao.disabled=false; botao.textContent='TENTAR LIBERAR 2 MINUTOS NOVAMENTE'; aviso.innerHTML='<strong>ERRO</strong><span>Não foi possível solicitar os 2 minutos. Tente novamente.</span>'; }} }}
async function verificarPagamento() {{ try {{ const resposta=await fetch('/status-pix/{order_id}',{{cache:'no-store'}}); const dados=await resposta.json(); const tela=document.getElementById('status-pagamento'); if(dados.ok&&dados.pago&&dados.liberada){{tela.textContent='Pagamento aprovado! Internet liberada.';clearInterval(timerPagamento);}} else if(dados.ok&&dados.pago) tela.textContent='Pagamento aprovado! Liberando internet...'; else if(dados.ok) tela.textContent='Aguardando pagamento...'; }} catch(erro){{ console.log(erro); }} }}
let timerPagamento=setInterval(verificarPagamento,5000); verificarPagamento();
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
