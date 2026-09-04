from playwright.sync_api import sync_playwright

PASTA_SESSAO_WA = "./sessao_whatsapp"

NUMEROS_TESTE = [
    "554196639204",   # troque por um numero que EXISTE
    "5541000000000",  # proposital "nao existe"
]


def validar_numero(pagina, numero):
    url = f"https://web.whatsapp.com/send?phone={numero}"
    pagina.goto(url, wait_until="domcontentloaded")
    for _ in range(50):
        pagina.wait_for_timeout(500)
        html = pagina.content().lower()
        sinais = [
            "url is invalid", "url é inválido", "url e invalido",
            "compartilhado através de url", "compartilhado atraves de url",
            "phone number shared", "não está no whatsapp", "nao esta no whatsapp",
            "isn't on whatsapp", "is not on whatsapp",
        ]
        if any(s in html for s in sinais):
            return "NAO_EXISTE"
        caixa = pagina.query_selector("div[contenteditable='true'][data-tab]")
        if caixa:
            return "EXISTE"
    return "ERRO"


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PASTA_SESSAO_WA, headless=False, channel="chrome",
            viewport={"width": 1200, "height": 800},
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(">> Abrindo WhatsApp Web...")
        pg.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(" Se aparecer QR CODE, escaneie com o CHIP DE VALIDACAO.")
        print(" Espere carregar as conversas.")
        print("=" * 60)
        input(">> ENTER quando o WhatsApp Web carregar... ")

        print("\n>> Validando...\n")
        for num in NUMEROS_TESTE:
            print(f"   {num} ...", end=" ", flush=True)
            print(validar_numero(pg, num))
            pg.wait_for_timeout(3000)

        print("\n EXISTE = tem WhatsApp | NAO_EXISTE = nao tem | ERRO = me avisa")
        input("\nENTER pra fechar...")
        ctx.close()


if __name__ == "__main__":
    main()