@app.route("/criar-pix", methods=["GET"])
def criar_pix():
    try:
        if not MP_ACCESS_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "MP_ACCESS_TOKEN nao configurado"
            }), 500

        # Dados vindos do MikroTik
        mac = request.args.get("mac", "")
        ip = request.args.get("ip", "")
        link_login = request.args.get("link-login", "")
        link_orig = request.args.get("link-orig", "")

        # Planos disponíveis
        planos = {
            "1h": {
                "nome": "1 hora",
                "valor": "5.00",
                "horas": 1
            },
            "2h": {
                "nome": "2 horas",
                "valor": "10.00",
                "horas": 2
            },
            "5h": {
                "nome": "5 horas",
                "valor": "15.00",
                "horas": 5
            },
            "10h": {
                "nome": "10 horas",
                "valor": "20.00",
                "horas": 10
            }
        }

        plano_id = request.args.get("plano", "")

        # Se ainda não escolheu plano, mostra a tela de seleção
        if plano_id not in planos:
            pagina_planos = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>Escolha seu plano</title>

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
Escolha o tempo de acesso
</p>

<a class="plano"
href="/criar-pix?plano=1h&mac={mac}&ip={ip}&link-login={link_login}&link-orig={link_orig}">
1 hora
<span>R$ 5,00</span>
</a>

<a class="plano"
href="/criar-pix?plano=2h&mac={mac}&ip={ip}&link-login={link_login}&link-orig={link_orig}">
2 horas
<span>R$ 10,00</span>
</a>

<a class="plano"
href="/criar-pix?plano=5h&mac={mac}&ip={ip}&link-login={link_login}&link-orig={link_orig}">
5 horas
<span>R$ 15,00</span>
</a>

<a class="plano"
href="/criar-pix?plano=10h&mac={mac}&ip={ip}&link-login={link_login}&link-orig={link_orig}">
10 horas
<span>R$ 20,00</span>
</a>

</div>

</body>
</html>
"""
            return pagina_planos, 200

        plano = planos[plano_id]
        valor = plano["valor"]
        nome_plano = plano["nome"]
        horas = plano["horas"]

        url = "https://api.mercadopago.com/v1/orders"

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(uuid.uuid4())
        }

        referencia = (
            f"mikrotik_{plano_id}_"
            f"{uuid.uuid4().hex[:10]}"
        )

        pedido = {
            "type": "online",
            "processing_mode": "automatic",
            "external_reference": referencia,
            "total_amount": valor,
            "payer": {
                "email": "rhayr8@gmail.com"
            },
            "transactions": {
                "payments": [
                    {
                        "amount": valor,
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

.valor {{
    font-size: 32px;
    font-weight: bold;
    margin: 15px 0;
}}

.plano {{
    font-size: 20px;
    font-weight: bold;
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
Pedido: {order_id}<br>
Plano: {nome_plano}<br>
Tempo: {horas} hora(s)
</div>

</div>

<script>
function copiarPix() {{
    const codigo =
        document.getElementById("pix").value;

    navigator.clipboard.writeText(codigo)
        .then(function() {{
            alert("Código PIX copiado!");
        }});
}}

async function verificarPagamento() {{
    try {{
        const resposta =
            await fetch("/status-pix/{order_id}");

        const dados =
            await resposta.json();

        const statusTela =
            document.getElementById(
                "status-pagamento"
            );

        if (dados.ok && dados.pago) {{
            statusTela.textContent =
                "Pagamento aprovado!";

            if (timerPagamento) {{
                clearInterval(timerPagamento);
            }}

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
    setInterval(verificarPagamento, 5000);

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
            flush=True
        )

        return jsonify({
            "ok": False,
            "erro": str(erro)
        }), 500
