from playwright.sync_api import sync_playwright

# MESMA sessao do Painel (voce ja loga no Google nela)
PASTA_SESSAO = "./sessao_painel"

# Link da sua planilha Disparo Mazza
URL_PLANILHA = "https://docs.google.com/spreadsheets/d/1I6eGGR-GTcWzaGa8vci0ut-iG7puKxgw4fXqEQCSof8/edit#gid=0"

ATENDENTE = "Kaoe"  # valor fixo da coluna C

# ---- DADO DE TESTE (nome + numero bruto) ----
NOME_TESTE = "TESTE ROBO"
NUMERO_TESTE = "(41) 99999-7297"
# ---------------------------------------------


def ir_para_celula(pg, celula):
    """Usa a Caixa de Nome do Sheets pra pular direto pra uma celula (ex: A5)."""
    # a caixa de nome fica no canto superior esquerdo
    caixa = pg.query_selector("#t-name-box")
    caixa.click()
    pg.keyboard.press("Control+A")
    pg.keyboard.type(celula)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)


def escrever(pg, celula, valor):
    ir_para_celula(pg, celula)
    pg.keyboard.type(str(valor))
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)


def achar_primeira_linha_vazia(pg):
    """
    Vai na coluna A e usa Ctrl+Seta pra baixo pra achar o fim dos dados.
    Retorna o numero da proxima linha vazia.
    """
    ir_para_celula(pg, "A1")
    pg.keyboard.press("Control+ArrowDown")
    pg.wait_for_timeout(400)
    # le em qual celula parou pela caixa de nome
    ref = pg.eval_on_selector("#t-name-box", "el => el.value")  # ex "A7"
    # extrai o numero
    num = int("".join(c for c in ref if c.isdigit()) or "1")
    # se A1 estiver vazia, ctrl+down vai la pro fim; tratamos:
    # proxima linha vazia = num + 1 (assumindo cabecalho na linha 1)
    return num + 1


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PASTA_SESSAO, headless=False, channel="chrome",
            viewport={"width": 1400, "height": 900},
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(">> Abrindo a planilha...")
        pg.goto(URL_PLANILHA, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(" Se pedir login no GOOGLE, faca o login.")
        print(" Espere a planilha CARREGAR (ver as celulas).")
        print("=" * 60)
        input(">> ENTER quando a planilha estiver aberta... ")

        # garante foco na planilha
        pg.wait_for_selector("#t-name-box", timeout=30000)

        linha = achar_primeira_linha_vazia(pg)
        print(f">> Primeira linha vazia: {linha}")

        print(">> Escrevendo dados de teste...")
        escrever(pg, f"A{linha}", NOME_TESTE)      # nome
        escrever(pg, f"C{linha}", ATENDENTE)       # atendente
        escrever(pg, f"I{linha}", NUMERO_TESTE)    # numero bruto (formula B trata)

        print("\n" + "=" * 60)
        print(f" PRONTO! Colei na linha {linha}:")
        print(f"   A{linha} = {NOME_TESTE}")
        print(f"   C{linha} = {ATENDENTE}")
        print(f"   I{linha} = {NUMERO_TESTE}")
        print(f"   >> CONFIRA se a B{linha} mostrou o numero tratado!")
        print("=" * 60)
        input("\nENTER pra fechar...")
        ctx.close()


if __name__ == "__main__":
    main()