@app.route("/criar-pix", methods=["GET"])
def criar_pix():
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN não configurado"
            }), 500

        url = "https://api.mercadopago.com/v1/orders"

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
            "Content-Type": "application/json"
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

        print("Resposta criação PIX:", dados, flush=True)

        if resposta.status_code not in (200, 201):
            return jsonify({
                "ok": False,
                "status_code": resposta.status_code,
                "mercado_pago": dados
            }), resposta.status_code

        payments = (
            dados.get("transactions", {})
                .get("payments", [])
        )

        if not payments:
            return jsonify({
                "ok": False,
                "erro": "Order criada sem dados de pagamento",
                "order": dados
            }), 500

        pagamento = payments[0]
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
        print("Erro ao criar PIX:", str(erro), flush=True)

        return jsonify({
            "ok": False,
            "erro": str(erro)
        }), 500import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")


@app.route("/", methods=["GET"])
def home():
    return "Mikrotik Hotspot - Sistema PIX Online", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        dados = request.get_json(silent=True) or {}

        print("Webhook recebido:", dados, flush=True)

        # Mercado Pago pode mandar o ID pela URL ou pelo JSON
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

        # Simulações do painel do Mercado Pago usam IDs fictícios.
        # O webhook deve confirmar o recebimento sem gerar erro 500.
        if not data_id:
            return jsonify({
                "status": "received",
                "message": "Webhook recebido sem ID"
            }), 200

        # Eventos do tipo payment
        if tipo == "payment":
            if not MP_ACCESS_TOKEN:
                print("MP_ACCESS_TOKEN não configurado", flush=True)
                return jsonify({
                    "status": "received",
                    "message": "Access token não configurado"
                }), 200

            url = f"https://api.mercadopago.com/v1/payments/{data_id}"

            headers = {
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
            }

            resposta = requests.get(
                url,
                headers=headers,
                timeout=15
            )

            # ID fictício usado pelo simulador
            if resposta.status_code == 404:
                print(
                    f"Pagamento {data_id} não encontrado. "
                    "Provavelmente notificação de teste.",
                    flush=True
                )

                return jsonify({
                    "status": "received",
                    "message": "Notificação de teste recebida"
                }), 200

            resposta.raise_for_status()

            pagamento = resposta.json()

            print(
                "Status do pagamento:",
                pagamento.get("status"),
                flush=True
            )

            if pagamento.get("status") == "approved":
                print(
                    f"PIX APROVADO - pagamento {data_id}",
                    flush=True
                )

                # Depois colocaremos aqui a liberação
                # automática do cliente no MikroTik.

            return jsonify({
                "status": "received",
                "payment_status": pagamento.get("status")
            }), 200

        # Eventos Order
        if tipo == "order" or action.startswith("order."):
            print(
                f"Evento ORDER recebido: {data_id}",
                flush=True
            )

            # Neste momento apenas confirmamos o webhook.
            # A consulta correta da Order será adicionada
            # na próxima etapa.
            return jsonify({
                "status": "received",
                "type": "order",
                "id": data_id
            }), 200

        # Qualquer outro evento é confirmado
        return jsonify({
            "status": "received",
            "type": tipo
        }), 200

    except Exception as erro:
        print("Erro no webhook:", str(erro), flush=True)

        # Confirma o recebimento para evitar repetição
        # da notificação durante esta fase de configuração.
        return jsonify({
            "status": "received",
            "error": str(erro)
        }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
