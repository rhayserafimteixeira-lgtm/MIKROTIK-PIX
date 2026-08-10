import os
import requests
import uuid
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
            if not MP_ACCESS_TOKEN:
                return jsonify({
                    "status": "received",
                    "message": "Access Token nao configurado"
                }), 200

            url = f"https://api.mercadopago.com/v1/payments/{data_id}"

            resposta = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
                },
                timeout=15
            )

            if resposta.status_code == 404:
                return jsonify({
                    "status": "received",
                    "message": "Notificacao de teste recebida"
                }), 200

            resposta.raise_for_status()

            pagamento = resposta.json()

            if pagamento.get("status") == "approved":
                print(
                    f"PIX APROVADO - pagamento {data_id}",
                    flush=True
                )

            return jsonify({
                "status": "received",
                "payment_status": pagamento.get("status")
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
            "X-idempotency-key": str(uuid.uuid4())
        }

        pedido = {
            "type": "online",
            "external_reference": "teste_mikrotik_pix",
            "total_amount": "5.00",
            "payer": {
                "email": "test_user_br@testuser.com",
                "first_name": "APRO"
            },
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

        return jsonify({
            "ok": True,
            "order_id": dados.get("id"),
            "status": dados.get("status"),
            "status_detail": dados.get("status_detail"),
            "payment_id": pagamento.get("id"),
            "qr_code": metodo.get("qr_code"),
            "qr_code_base64": metodo.get("qr_code_base64"),
            "ticket_url": metodo.get("ticket_url")
        }), 200

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
