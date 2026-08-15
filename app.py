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
<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>
<title>Internet via PIX</title>

<style>
body {{
    font-family: Arial, sans-serif;
    background: #f2f4f7;
    margin: 0;
    padding: 18px;
    text-align: center;
}}

.caixa {{
    max-width: 420px;
    margin: 20px auto;
    background: white;
    padding: 25px;
    border-radius: 18px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.15);
}}

h1 {{
    margin-bottom: 8px;
}}

.subtitulo {{
    color: #555;
    margin-bottom: 22px;
}}

.plano {{
    display: block;
    text-decoration: none;
    color: #111;
    border: 2px solid #00a650;
    border-radius: 14px;
    padding: 18px;
    margin: 13px 0;
    font-size: 20px;
    font-weight: bold;
}}

.plano span {{
    display: block;
    color: #00a650;
    font-size: 25px;
    margin-top: 5px;
}}

.plano:hover {{
    background: #f0fff7;
}}
</style>
</head>

<body>
<div class="caixa">

<h1>🌐 Internet Wi-Fi</h1>

<p class="subtitulo">
Escolha seu plano de acesso
</p>

<a
    class="plano"
    href="{link_plano('1h')}"
>
1 hora
<span>R$ 5,00</span>
</a>

<a
    class="plano"
    href="{link_plano('2h')}"
>
2 horas
<span>R$ 10,00</span>
</a>

<a
    class="plano"
    href="{link_plano('5h')}"
>
5 horas
<span>R$ 15,00</span>
</a>

<a
    class="plano"
    href="{link_plano('10h')}"
>
10 horas
<span>R$ 20,00</span>
</a>

</div>
</body>
</html>
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

        pagina = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>
<title>Internet via PIX</title>

<style>
body {{
    font-family: Arial, sans-serif;
    background: #f2f4f7;
    margin: 0;
    padding: 18px;
    text-align: center;
}}

.caixa {{
    max-width: 420px;
    margin: 20px auto;
    background: white;
    padding: 25px;
    border-radius: 18px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.15);
}}

h1 {{
    margin-bottom: 8px;
}}

.plano {{
    font-size: 20px;
    font-weight: bold;
}}

.valor {{
    font-size: 32px;
    font-weight: bold;
    margin: 15px 0;
    color: #00a650;
}}

img {{
    width: 260px;
    max-width: 90%;
    margin: 15px 0;
}}

textarea {{
    box-sizing: border-box;
    width: 100%;
    height: 105px;
    padding: 10px;
    border-radius: 8px;
    resize: none;
}}

button {{
    width: 100%;
    padding: 15px;
    margin-top: 12px;
    border: none;
    border-radius: 10px;
    font-size: 17px;
    cursor: pointer;
    background: #00a650;
    color: white;
}}

.status {{
    margin-top: 20px;
    font-weight: bold;
    font-size: 18px;
}}

.codigo {{
    font-size: 12px;
    margin-top: 15px;
    color: #666;
}}
</style>
</head>

<body>

<div class="caixa">

<h1>🌐 Internet via PIX</h1>

<div class="plano">
Plano de {nome_plano}
</div>

<div class="valor">
R$ {valor.replace(".", ",")}
</div>

<p>
Escaneie o QR Code para pagar:
</p>

{imagem_qr}

<p>
<strong>PIX Copia e Cola</strong>
</p>

<textarea
    id="pix"
    readonly
>{qr_code}</textarea>

<button onclick="copiarPix()">
Copiar código PIX
</button>

<div
    class="status"
    id="status-pagamento"
>
Aguardando pagamento...
</div>

<div class="codigo">
Pedido: {order_id}
<br>
Plano: {nome_plano}
<br>
Tempo: {horas} hora(s)
</div>

</div>

<script>
function copiarPix() {{
    const codigo =
        document.getElementById(
            "pix"
        ).value;

    navigator.clipboard
        .writeText(codigo)
        .then(function() {{
            alert(
                "Código PIX copiado!"
            );
        }});
}}


async function verificarPagamento() {{
    try {{
        const resposta =
            await fetch(
                "/status-pix/{order_id}",
                {{
                    cache: "no-store"
                }}
            );

        const dados =
            await resposta.json();

        const statusTela =
            document.getElementById(
                "status-pagamento"
            );

        if (
            dados.ok
            && dados.pago
            && dados.liberada
        ) {{
            statusTela.textContent =
                "Pagamento aprovado! Internet liberada.";

            clearInterval(
                timerPagamento
            );

        }} else if (
            dados.ok
            && dados.pago
        ) {{
            statusTela.textContent =
                "Pagamento aprovado! Liberando internet...";

        }} else if (dados.ok) {{
            statusTela.textContent =
                "Aguardando pagamento...";
        }}

    }} catch (erro) {{
        console.log(
            "Erro na consulta do pagamento:",
            erro
        );
    }}
}}


let timerPagamento =
    setInterval(
        verificarPagamento,
        5000
    );

verificarPagamento();
</script>

</body>
</html>
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
