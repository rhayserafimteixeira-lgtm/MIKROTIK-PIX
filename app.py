import os
import uuid
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
MP_PAYER_EMAIL = os.getenv("MP_PAYER_EMAIL", "rhayr8@gmail.com")

MP_API_BASE = "https://api.mercadopago.com"
REQUEST_TIMEOUT = 20

PLANOS = {
    "1h": {"nome": "1 hora", "valor": "5.00", "horas": 1},
    "2h": {"nome": "2 horas", "valor": "10.00", "horas": 2},
    "5h": {"nome": "5 horas", "valor": "15.00", "horas": 5},
    "10h": {"nome": "10 horas", "valor": "20.00", "horas": 10},
}


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
    Considera pago somente quando:
    status = processed
    status_detail = accredited

    Isso evita liberar uma order processada que esteja, por exemplo,
    parcialmente reembolsada.
    """
    return (
        dados.get("status") == "processed"
        and dados.get("status_detail") == "accredited"
    )


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

    return ":".join(
        mac_limpo[i:i + 2]
        for i in range(0, 12, 2)
    )


def dados_da_referencia(referencia):
    """
    Formato criado pelo sistema:
    mikrotik_<plano>_<mac-sem-separador>_<ip-com-hifen>_<id>
    """
    if not referencia or not referencia.startswith("mikrotik_"):
        return None

    partes = referencia.split("_")

    if len(partes) < 5:
        return None

    plano_id = partes[1]
    mac_cliente = normalizar_mac(partes[2])
    ip_cliente = partes[3].replace("-", ".")

    if plano_id not in PLANOS or not mac_cliente:
        return None

    return {
        "plano": plano_id,
        "horas": PLANOS[plano_id]["horas"],
        "mac": mac_cliente,
        "ip": ip_cliente,
    }


def registrar_liberacao(dados_order, order_id):
    """
    Registra uma liberacao pendente apenas se a order estiver
    realmente paga e tiver sido criada por este sistema.
    """
    if not order_esta_paga(dados_order):
        return False

    referencia = dados_order.get("external_reference", "")
    cliente = dados_da_referencia(referencia)

    if not cliente:
        return False

    liberacoes = app.config.setdefault(
        "LIBERACOES_PENDENTES",
        {},
    )

    mac_cliente = cliente["mac"]

    # Evita duplicar a mesma liberacao.
    existente = liberacoes.get(mac_cliente)
    if existente and existente.get("order_id") == order_id:
        return True

    liberacoes[mac_cliente] = {
        "mac": mac_cliente,
        "ip": cliente["ip"],
        "plano": cliente["plano"],
        "horas": cliente["horas"],
        "order_id": order_id,
    }

    print(
        "LIBERACAO PENDENTE | "
        f"MAC={mac_cliente} | "
        f"IP={cliente['ip']} | "
        f"PLANO={cliente['plano']} | "
        f"HORAS={cliente['horas']} | "
        f"ORDER={order_id}",
        flush=True,
    )

    return True


# =========================================================
# PÁGINA PRINCIPAL
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "Mikrotik Hotspot", 200


# =========================================================
# WEBHOOK MERCADO PAGO
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    O webhook do Mercado Pago informa principalmente o ID do recurso.
    Por seguranca e confiabilidade, o servidor consulta a order
    diretamente na API do Mercado Pago antes de liberar o cliente.
    """
    try:
        dados_webhook = request.get_json(silent=True) or {}

        print(
            "Webhook recebido:",
            dados_webhook,
            flush=True,
        )

        data_id = request.args.get("data.id")

        if not data_id:
            data = dados_webhook.get("data", {})
            if isinstance(data, dict):
                data_id = data.get("id")

        tipo = request.args.get("type") or dados_webhook.get("type")
        action = dados_webhook.get("action", "")

        print(
            f"Tipo={tipo} | Action={action} | ID={data_id}",
            flush=True,
        )

        # Para Orders API, so processamos notificacoes de order.
        if tipo == "order" or action.startswith("order."):
            if not data_id:
                return jsonify({
                    "status": "received",
                    "type": "order",
                    "aviso": "data.id ausente",
                }), 200

            if not MP_ACCESS_TOKEN:
                print(
                    "Webhook: MP_ACCESS_TOKEN nao configurado",
                    flush=True,
                )
                return jsonify({
                    "status": "received",
                    "type": "order",
                    "id": data_id,
                }), 200

            resposta, dados_order = consultar_order(data_id)

            if resposta.status_code == 200:
                registrar_liberacao(
                    dados_order,
                    data_id,
                )
            else:
                print(
                    "Webhook: falha ao consultar order | "
                    f"HTTP={resposta.status_code} | "
                    f"DADOS={dados_order}",
                    flush=True,
                )

            return jsonify({
                "status": "received",
                "type": "order",
                "id": data_id,
            }), 200

        # Mantemos resposta 200 para outros eventos,
        # mas eles nao liberam internet.
        return jsonify({
            "status": "received",
            "type": tipo,
            "id": data_id,
        }), 200

    except Exception as erro:
        print(
            "Erro no webhook:",
            str(erro),
            flush=True,
        )

        # Mercado Pago espera 200/201 para confirmar o recebimento.
        # A consulta /status-pix tambem serve como redundancia.
        return jsonify({
            "status": "received",
            "error": str(erro),
        }), 200


# =========================================================
# CONSULTAR STATUS DO PIX
# =========================================================

@app.route("/status-pix/<order_id>", methods=["GET"])
def status_pix(order_id):
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado",
            }), 500

        resposta, dados = consultar_order(order_id)

        if resposta.status_code != 200:
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados,
            }), resposta.status_code

        status = dados.get("status", "")
        status_detail = dados.get("status_detail", "")
        pago = order_esta_paga(dados)

        if pago:
            registrar_liberacao(
                dados,
                order_id,
            )

        return jsonify({
            "ok": True,
            "status": status,
            "status_detail": status_detail,
            "pago": pago,
        }), 200

    except Exception as erro:
        print(
            "Erro no status PIX:",
            str(erro),
            flush=True,
        )

        return jsonify({
            "ok": False,
            "erro": str(erro),
        }), 500


# =========================================================
# MIKROTIK - CONSULTAR LIBERACAO PENDENTE
# =========================================================

@app.route("/liberacao-pendente", methods=["GET"])
def liberacao_pendente():
    liberacoes = app.config.setdefault(
        "LIBERACOES_PENDENTES",
        {},
    )

    if not liberacoes:
        return jsonify({
            "ok": True,
            "pendente": False,
        }), 200

    mac, dados = next(iter(liberacoes.items()))

    return jsonify({
        "ok": True,
        "pendente": True,
        "mac": dados.get("mac", mac),
        "ip": dados.get("ip", ""),
        "plano": dados.get("plano", ""),
        "horas": dados.get("horas", 0),
        "order_id": dados.get("order_id", ""),
    }), 200


# =========================================================
# MIKROTIK - CONFIRMAR LIBERACAO
# =========================================================

@app.route("/confirmar-liberacao", methods=["GET"])
def confirmar_liberacao():
    mac = normalizar_mac(
        request.args.get("mac", "")
    )

    if not mac:
        return jsonify({
            "ok": False,
            "erro": "MAC nao informado ou invalido",
        }), 400

    liberacoes = app.config.setdefault(
        "LIBERACOES_PENDENTES",
        {},
    )

    if mac not in liberacoes:
        return jsonify({
            "ok": False,
            "erro": "Liberacao nao encontrada",
        }), 404

    liberacoes.pop(mac, None)

    return jsonify({
        "ok": True,
        "confirmado": True,
        "mac": mac,
    }), 200


# =========================================================
# CRIAR PIX / ESCOLHER PLANO
# =========================================================

@app.route("/criar-pix", methods=["GET"])
def criar_pix():
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado",
            }), 500

        # Dados enviados pelo Hotspot MikroTik.
        mac = request.args.get("mac", "").strip()
        ip = request.args.get("ip", "").strip()
        link_login = request.args.get("link-login", "").strip()
        link_orig = request.args.get("link-orig", "").strip()

        mac_normalizado = normalizar_mac(mac)

        if mac and mac_normalizado:
            mac = mac_normalizado

        print(
            f"CLIENTE HOTSPOT | MAC={mac} | IP={ip} | "
            f"LINK_LOGIN={link_login} | LINK_ORIG={link_orig}",
            flush=True,
        )

        plano_id = request.args.get("plano", "")

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
                return f"/criar-pix?{query}"

            pagina_planos = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
<p class="subtitulo">Escolha seu plano de acesso</p>

<a class="plano" href="{link_plano('1h')}">
1 hora
<span>R$ 5,00</span>
</a>

<a class="plano" href="{link_plano('2h')}">
2 horas
<span>R$ 10,00</span>
</a>

<a class="plano" href="{link_plano('5h')}">
5 horas
<span>R$ 15,00</span>
</a>

<a class="plano" href="{link_plano('10h')}">
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

        plano = PLANOS[plano_id]
        valor = plano["valor"]
        nome_plano = plano["nome"]
        horas = plano["horas"]

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

        url = f"{MP_API_BASE}/v1/orders"

        headers = mp_headers(json_body=True)
        headers["X-Idempotency-Key"] = str(uuid.uuid4())

        mac_ref = mac_normalizado.replace(":", "")
        ip_ref = ip.replace(".", "-")

        referencia = (
            f"mikrotik_{plano_id}_{mac_ref}_{ip_ref}_"
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

        if resposta.status_code not in (200, 201):
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados,
            }), resposta.status_code

        pagamentos = (
            dados
            .get("transactions", {})
            .get("payments", [])
        )

        if not pagamentos:
            return jsonify({
                "ok": False,
                "erro": "Order criada sem pagamento",
                "order": dados,
            }), 500

        pagamento = pagamentos[0]
        metodo = pagamento.get("payment_method", {})

        qr_code = metodo.get("qr_code", "")
        qr_code_base64 = metodo.get("qr_code_base64", "")
        order_id = dados.get("id", "")

        if not order_id:
            return jsonify({
                "ok": False,
                "erro": "Mercado Pago nao retornou o ID da order",
                "order": dados,
            }), 500

        if not qr_code and not qr_code_base64:
            return jsonify({
                "ok": False,
                "erro": "Mercado Pago nao retornou QR Code PIX",
                "order": dados,
            }), 500

        # =====================================================
        # TELA DO QR CODE PIX
        # =====================================================

        pagina = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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

<p>Escaneie o QR Code para pagar:</p>

<img
src="data:image/png;base64,{qr_code_base64}"
alt="QR Code PIX"
>

<p><strong>PIX Copia e Cola</strong></p>

<textarea id="pix" readonly>{qr_code}</textarea>

<button onclick="copiarPix()">
Copiar código PIX
</button>

<div class="status" id="status-pagamento">
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
        document.getElementById("pix").value;

    navigator.clipboard
        .writeText(codigo)
        .then(function() {{
            alert("Código PIX copiado!");
        }});
}}

async function verificarPagamento() {{
    try {{
        const resposta =
            await fetch(
                "/status-pix/{order_id}",
                {{ cache: "no-store" }}
            );

        const dados =
            await resposta.json();

        const statusTela =
            document.getElementById(
                "status-pagamento"
            );

        if (dados.ok && dados.pago) {{
            statusTela.textContent =
                "Pagamento aprovado! Internet liberada.";

            clearInterval(timerPagamento);

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
            str(erro),
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
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
