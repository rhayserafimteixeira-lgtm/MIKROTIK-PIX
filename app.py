import os
import requests
from flask import Flask, request, jsonify

app = Flask(_name_)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")


@app.route("/", methods=["GET"])
def home():
    return "MIKROTIK PIX ONLINE", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        dados = request.get_json(silent=True) or {}

        print("Webhook recebido:", dados)

        payment_id = None

        if isinstance(dados.get("data"), dict):
            payment_id = dados["data"].get("id")

        if not payment_id:
            return jsonify({
                "status": "recebido",
                "message": "Sem ID de pagamento"
            }), 200

        if not MP_ACCESS_TOKEN:
            print("MP_ACCESS_TOKEN não configurado")
            return jsonify({"error": "Servidor sem Access Token"}), 500

        url = f"https://api.mercadopago.com/v1/payments/{payment_id}"

        resposta = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
            },
            timeout=15
        )

        resposta.raise_for_status()
        pagamento = resposta.json()

        status = pagamento.get("status")

        print("Pagamento:", payment_id)
        print("Status:", status)

        if status == "approved":
            print("PIX APROVADO - preparar liberação do MikroTik")

            # Na próxima etapa entra aqui
            # a comunicação com o MikroTik.

        return jsonify({
            "ok": True,
            "payment_id": payment_id,
            "payment_status": status
        }), 200

    except Exception as erro:
        print("Erro:", str(erro))
        return jsonify({"error": str(erro)}), 500


if _name_ == "_main_":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
