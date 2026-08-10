import os
import uuid
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")


@app.route("/", methods=["GET"])
def home():
    return "Mikrotik Hotspot", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        dados = request.get_json(silent=True) or {}

        print("Webhook recebido:", dados, flush=True)

        data_id = request.args.get("data.id")

        if not data_id:
            data = dados.get("data", {})
            if isinstance(data, dict):
                data_id = data.get("id")

        tipo = request.args.get("type") or dados.get("type")
        action = dados.get("action", "")

        print(
            f"Tipo: {tipo} | Action: {action} | ID: {data_id}",
            flush=True
        )

        if tipo == "order" or action.startswith("order."):
            print(
                f"Evento ORDER recebido: {data_id}",
                flush=True
            )

            return jsonify({
                "status": "received",
                "type": "order",
                "id": data_id
            }), 200

        if tipo == "payment":
            return jsonify({
                "status": "received",
                "type": "payment",
                "id": data_id
            }), 200

        return jsonify({
            "status": "received",
            "type": tipo
        }), 200

    except Exception as erro:
        print("Erro no webhook:", str(erro), flush=True)

        return jsonify({
            "status": "received",
            "error": str(erro)
        }), 200


@app.route("/criar-pix", methods=["GET"])
def criar_pix():
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado"
            }), 500

        url = "https://api.mercadopago.com/v1/orders"

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(uuid.uuid4())
        }

        pedido = {
            "type": "online",
            "processing_mode": "automatic",
            "external_reference": f"mikrotik_{uuid.uuid4().hex[:10]}",
            "total_amount": "5.00",
            "transactions": {
                "payments": [
                    {
                        "amount": "5.00",
                        "payment_method": {
                            "id": "pix",
                            "type": "bank_transfer"
                        }
                    }
                ]
            }
        }

        resposta = requests.post(
            url,
            headers=headers,
            json=pedido,
            timeout=20
        )

        dados = resposta.json()

        print(
            "Resposta criacao PIX:",
            dados,
            flush=True
        )

        if resposta.status_code not in (200, 201):
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados
            }), resposta.status_code

        pagamentos = (
            dados.get("transactions", {})
            .get("payments", [])
        )

        if not pagamentos:
            return jsonify({
                "ok": False,
                "erro": "Order criada sem pagamento",
                "order": dados
            }), 500

        pagamento = pagamentos[0]
        metodo = pagamento.get("payment_method", {})

        qr_code = metodo.get("qr_code", "")
        qr_code_base64 = metodo.get("qr_code_base64", "")
        ticket_url = metodo.get("ticket_url", "")
        order_id = dados.get("id", "")

        pagina = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

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
    margin-bottom: 5px;
}}

.valor {{
    font-size: 32px;
    font-weight: bold;
    margin: 15px 0;
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
}}
</style>
</head>

<body>

<div class="caixa">

<h1>Internet via PIX</h1>

<p>Plano de 1 hora</p>

<div class="valor">
R$ 5,00
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

<div class="status">
Aguardando pagamento...
</div>

<div class="codigo">
Pedido: {order_id}
</div>

</div>

<script>
function copiarPix() {{
    const codigo = document.getElementById("pix").value;

    navigator.clipboard.writeText(codigo).then(function() {{
        alert("Código PIX copiado!");
    }});
}}
</script>

</body>
</html>
"""

        return pagina, 200

    except Exception as erro:
        print(
            "Erro ao criar PIX:",
            str(erro),
            flush=True
        )

        return jsonify({
            "ok": False,
            "erro": str(erro)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
