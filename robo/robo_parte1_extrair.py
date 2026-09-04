import re
from playwright.sync_api import sync_playwright

URL_INICIAL = "https://app.paineldocorretor.com.br/?hash=yKR2qB"
URL_LISTA = "https://app.paineldocorretor.com.br/indicacao"
PASTA_SESSAO = "./sessao_painel"


def normalizar_telefone(bruto):
    if not bruto:
        return ""
    d = re.sub(r"\D", "", bruto)
    if not d.startswith("55"):
        d = "55" + d
    ddd = d[2:4]
    numero = d[4:]
    if len(numero) == 9 and numero[0] == "9":
        numero = numero[1:]
    return "55" + ddd + numero


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PASTA_SESSAO, headless=False, channel="chrome",
            viewport={"width": 1400, "height": 900},
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(">> Abrindo o Painel...")
        try:
            pg.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            print("   (faca login manualmente na janela do Chrome)")

        print("\n" + "=" * 60)
        print(" Logue e navegue ate a LISTA de leads (codigos em rosa).")
        print("=" * 60)
        input(">> ENTER quando estiver VENDO a lista... ")

        if "/indicacao" not in pg.url:
            try:
                pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                input(">> Navegue ate a lista e aperte ENTER... ")

        print(">> Pegando o primeiro lead...")
        pg.wait_for_selector("table[role='grid'] tbody tr", timeout=30000)
        primeiro = pg.query_selector("tbody tr a[href*='/indicacao/detail/']")
        codigo = primeiro.get_attribute("href").rstrip("/").split("/")[-1]
        print(f">> Lead {codigo}")

        primeiro.click()
        pg.wait_for_load_state("domcontentloaded")
        pg.wait_for_selector("#Nome", timeout=30000)
        pg.wait_for_timeout(1500)

        nome = pg.eval_on_selector("#Nome", "el => el.value") or ""
        f1_bruto = pg.eval_on_selector("#FonePrincipal", "el => el.value") or ""
        f2_bruto = pg.eval_on_selector("#FoneCelular", "el => el.value") or ""
        f1 = normalizar_telefone(f1_bruto)
        f2 = normalizar_telefone(f2_bruto)

        print("\n" + "=" * 60)
        print(f" Nome  : {nome!r}")
        print(f" Fone1 : {f1_bruto!r} -> {f1!r}")
        print(f" Fone2 : {f2_bruto!r} -> {f2!r}")
        print(f" Iguais: {f1 == f2 and f1 != ''}")
        print("=" * 60)
        input("\nENTER pra fechar...")
        ctx.close()


if __name__ == "__main__":
    main()