import re
from datetime import datetime
from playwright.sync_api import sync_playwright

# ================== CONFIG ==================
QUANTOS_LEADS = 1

URL_INICIAL = "https://app.paineldocorretor.com.br/?hash=yKR2qB"
URL_LISTA = "https://app.paineldocorretor.com.br/indicacao"
URL_PLANILHA = "https://docs.google.com/spreadsheets/d/1I6eGGR-GTcWzaGa8vci0ut-iG7puKxgw4fXqEQCSof8/edit#gid=0"

PASTA_SESSAO = "./sessao_painel"
PASTA_SESSAO_WA = "./sessao_whatsapp"

ATENDENTE = "Kaoe"
STATUS_LIXO = "13"
PRODUTO_NENHUM = "4"
MSG_EXISTE = "Em contato para cliente"
MSG_NAO_EXISTE = "Nao consegui contato com os numeros cadastrados / os numeros nao existem"
# ============================================


def limpar_nome(bruto):
    nome = re.sub(r'[0-9./\-]+', ' ', bruto)
    nome = re.sub(r'\s+', ' ', nome).strip()
    return nome


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
    if len(ddd) != 2 or len(numero) < 8:
        return ""
    return "55" + ddd + numero


def aplicar_filtro_status(page, lista_status, timing=None):
    timing = timing or {}
    page.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(_tempo(timing, "apos_navegacao", 3000))
    _expandir_opcoes_filtro(page)

    selecao_atual = _status_selecionados_atual(page)
    esperado = _normalizar_lista_status(lista_status)
    if _normalizar_lista_status(selecao_atual) == esperado:
        _clicar_pesquisar(page)
        _aguardar_tabela(page, timing)
        return {"total": _contar_leads_lista(page), "reutilizado": True, "status": lista_status}

    if not _aplicar_status_via_kendo(page, lista_status):
        _abrir_dropdown_status(page, timing)
        _clicar_opcao_filtro(page, "Nenhum", timing)

        for status in lista_status:
            if not _marcar_status_exato(page, status, timing):
                raise RuntimeError(f"Status '{status}' não encontrado no filtro — verifique config/modos.json")

        page.mouse.click(20, 20)
        page.wait_for_timeout(_tempo(timing, "entre_acoes", 800))

    _clicar_pesquisar(page)
    _aguardar_tabela(page, timing)
    return {"total": _contar_leads_lista(page), "reutilizado": False, "status": lista_status}


def _tempo(timing, chave, padrao):
    return int((timing or {}).get(chave, padrao))


def _normalizar_lista_status(status):
    return sorted([(item or "").strip().lower() for item in status])


def _status_selecionados_atual(page):
    return page.evaluate(
        """
        () => {
          const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const findStatusSelect = () => {
            const candidates = [...document.querySelectorAll('select')];
            const byOptions = candidates.find((select) => {
              const optionTexts = [...select.options].map((option) => norm(option.textContent));
              return ['prospectar', 'retrabalhar', 'lixo', 'duplicidade'].some((label) => optionTexts.includes(label));
            });
            if (byOptions) return { select: byOptions };

            const labels = [...document.querySelectorAll('label, span, div, td, th')]
              .filter((el) => norm(el.textContent) === 'status');
            for (const label of labels) {
              let node = label;
              for (let i = 0; i < 4 && node; i += 1) {
                const select = node.querySelector && node.querySelector('select');
                if (select) return { select };
                const next = node.nextElementSibling;
                if (next) {
                  const nextSelect = next.querySelector && next.querySelector('select');
                  if (nextSelect) return { select: nextSelect };
                }
                node = node.parentElement;
              }
            }
            return null;
          };
          const info = findStatusSelect();
          if (!info || !info.select) return [];
          const select = info.select;
          const widget = window.jQuery ? window.jQuery(select).data("kendoMultiSelect") : null;
          const values = widget ? widget.value() : [...select.selectedOptions].map((option) => option.value);
          return values
            .map((value) => {
              const option = [...select.options].find((opt) => opt.value == value);
              return option ? option.textContent.trim() : null;
            })
            .filter(Boolean);
        }
        """
    )


def _aplicar_status_via_kendo(page, lista_status):
    return page.evaluate(
        """
        (labels) => {
          const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();

          window.__mazzaStatusMultiSelect = window.__mazzaStatusMultiSelect || (() => {
            const candidates = [...document.querySelectorAll('select')];
            const byOptions = candidates.find((select) => {
              const optionTexts = [...select.options].map((option) => norm(option.textContent));
              return ['prospectar', 'retrabalhar', 'lixo', 'duplicidade'].some((label) => optionTexts.includes(label));
            });
            if (byOptions) return { select: byOptions };

            const labels = [...document.querySelectorAll('label, span, div, td, th')]
              .filter((el) => norm(el.textContent) === 'status');
            for (const label of labels) {
              let node = label;
              for (let i = 0; i < 4 && node; i += 1) {
                const select = node.querySelector && node.querySelector('select');
                if (select) return { select };
                const next = node.nextElementSibling;
                if (next) {
                  const nextSelect = next.querySelector && next.querySelector('select');
                  if (nextSelect) return { select: nextSelect };
                }
                node = node.parentElement;
              }
            }
            return null;
          });

          const info = window.__mazzaStatusMultiSelect();
          if (!info || !info.select) return false;
          const select = info.select;
          const wantedValues = [];
          for (const label of labels) {
            const option = [...select.options].find((opt) => norm(opt.textContent) === norm(label));
            if (!option) return false;
            wantedValues.push(option.value);
          }

          const jq = window.jQuery || window.$;
          const widget = jq ? jq(select).data('kendoMultiSelect') : null;
          if (widget && typeof widget.value === 'function') {
            widget.value(wantedValues);
            if (typeof widget.trigger === 'function') widget.trigger('change');
            if (jq) jq(select).trigger('change');
            return true;
          }

          [...select.options].forEach((option) => {
            option.selected = wantedValues.includes(option.value);
          });
          select.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
        """,
        lista_status,
    )


def _expandir_opcoes_filtro(page):
    if _status_dropdown_disponivel(page):
        return

    gatilho = page.get_by_text(re.compile(r"OPÇÕES DE FILTRO|OPCOES DE FILTRO", re.I)).first
    if gatilho.count():
        gatilho.click()
        page.wait_for_timeout(_tempo(None, "entre_acoes", 800))


def _status_dropdown_disponivel(page):
    return (
        page.locator("text=/^\\s*Status\\s*$/i").first.is_visible(timeout=500)
        or page.locator("select[multiple]:visible").count() > 0
        or page.locator("button.multiselect.dropdown-toggle:visible").count() > 0
    )


def _abrir_dropdown_status(page, timing=None):
    seletores = [
        "label:has-text('Status') + span.k-widget",
        "label:has-text('Status') ~ span.k-widget",
        "label:has-text('Status') + div button",
        "label:has-text('Status') ~ div button",
        "button:has-text('Status')",
        "button.multiselect.dropdown-toggle",
        ".multiselect.dropdown-toggle",
    ]
    for seletor in seletores:
        locator = page.locator(seletor).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.click()
            page.wait_for_timeout(_tempo(timing, "apos_abrir_dropdown", 600))
            if _dropdown_aberto(page):
                return

    status_label = page.get_by_text(re.compile(r"^\s*Status\s*$", re.I)).first
    if status_label.count():
        status_label.scroll_into_view_if_needed()
        status_label.click()
        page.wait_for_timeout(_tempo(timing, "apos_abrir_dropdown", 600))
        if _dropdown_aberto(page):
            return

    raise RuntimeError("Dropdown de Status não encontrado no painel de filtros.")


def _dropdown_aberto(page):
    return page.locator(".multiselect-container:visible, .dropdown-menu:visible, .k-list-container:visible, .k-animation-container:visible").count() > 0


def _container_dropdown(page):
    container = page.locator(".multiselect-container:visible").last
    if container.count():
        return container
    container = page.locator(".k-list-container:visible").last
    if container.count():
        return container
    container = page.locator(".k-animation-container:visible").last
    if container.count():
        return container
    return page.locator(".dropdown-menu:visible").last


def _clicar_opcao_filtro(page, texto, timing=None):
    container = _container_dropdown(page)
    opcao = container.get_by_text(re.compile(rf"^\s*(✔|✓|✖|x|X)?\s*{re.escape(texto)}\s*$", re.I)).first
    if opcao.count():
        opcao.scroll_into_view_if_needed()
        opcao.click()
        page.wait_for_timeout(_tempo(timing, "entre_acoes", 800))
        return

    raise RuntimeError(f"Opção '{texto}' não encontrada no dropdown de Status.")


def _marcar_status_exato(page, status, timing=None):
    container = _container_dropdown(page)
    label = container.locator("label").filter(
        has_text=re.compile(rf"^\s*{re.escape(status)}\s*$", re.I)
    ).first
    if label.count():
        label.scroll_into_view_if_needed()
        checkbox = label.locator("input[type='checkbox']").first
        if checkbox.count():
            if not checkbox.is_checked():
                label.click()
        else:
            label.click()
        page.wait_for_timeout(_tempo(timing, "entre_acoes", 800))
        return True

    return page.evaluate(
        """
        (status) => {
          const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const menus = [...document.querySelectorAll('.multiselect-container, .dropdown-menu, .k-list-container, .k-animation-container')]
            .filter((el) => {
              const style = window.getComputedStyle(el);
              return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
            });
          const root = menus[menus.length - 1] || document;
          const labels = [...root.querySelectorAll('label')];
          const label = labels.find((el) => norm(el.textContent) === norm(status));
          if (!label) return false;
          label.scrollIntoView({block: 'nearest'});
          const input = label.querySelector("input[type='checkbox']");
          if (!input || !input.checked) label.click();
          return true;
        }
        """,
        status,
    )


def _clicar_pesquisar(page):
    candidatos = [
        "button:has-text('Pesquisar')",
        "input[type='submit'][value*='Pesquisar']",
        "a:has-text('Pesquisar')",
    ]
    for seletor in candidatos:
        botao = page.locator(seletor).first
        if botao.count():
            botao.scroll_into_view_if_needed()
            botao.click()
            return
    page.get_by_text("Pesquisar", exact=True).click()


def _aguardar_tabela(page, timing=None):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(_tempo(timing, "apos_pesquisar", 2500))
    page.wait_for_selector("table[role='grid'] tbody tr", timeout=45000)


def _contar_leads_lista(page):
    linhas = page.locator("table[role='grid'] tbody tr")
    try:
        return linhas.count()
    except Exception:
        return None


def validar_no_whatsapp(pg_wa, numero):
    if not numero:
        return False
    pg_wa.goto(f"https://web.whatsapp.com/send?phone={numero}", wait_until="domcontentloaded")
    for _ in range(50):
        pg_wa.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        html = pg_wa.content().lower()
        sinais = ["url is invalid", "url é inválido", "url e invalido",
                  "compartilhado através de url", "compartilhado atraves de url",
                  "phone number shared", "não está no whatsapp", "nao esta no whatsapp",
                  "isn't on whatsapp", "is not on whatsapp"]
        if any(s in html for s in sinais):
            return False
        if pg_wa.query_selector("div[contenteditable='true'][data-tab]"):
            return True
    return False


def achar_primeira_linha_vazia_A(pg_pl):
    """Le a coluna A inteira e retorna a primeira linha (>=2) com nome vazio."""
    # seleciona a coluna A e copia os valores via JS do Sheets nao da direto,
    # entao usamos a caixa de nome + leitura celula a celula ate achar vazia.
    def valor_da_celula(cel):
        cx = pg_pl.query_selector("#t-name-box")
        cx.click()
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        cx.press("Control+a"); cx.press("Delete")
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        cx.type(cel, delay=40); cx.press("Enter")
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        # le o conteudo da barra de formula
        val = pg_pl.eval_on_selector("#t-formula-bar-input", "el => el ? el.textContent : ''")
        return (val or "").strip()

    linha = 2
    while linha < 600:
        v = valor_da_celula(f"A{linha}")
        if v == "":
            return linha
        linha += 1
    return linha


def colar_na_planilha(pg_pl, nome, numero_bruto):
    def ir(cel):
        cx = pg_pl.query_selector("#t-name-box")
        cx.click()
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        cx.press("Control+a"); cx.press("Delete")
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
        cx.type(cel, delay=50); cx.press("Enter")
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))

    linha = achar_primeira_linha_vazia_A(pg_pl)
    print(f"      [planilha] primeira linha vazia (coluna A): {linha}")

    def escrever(cel, val):
        ir(cel)
        pg_pl.keyboard.type(str(val), delay=35)
        pg_pl.keyboard.press("Enter")
        pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))

    escrever(f"A{linha}", nome)
    escrever(f"C{linha}", ATENDENTE)
    escrever(f"I{linha}", numero_bruto)
    return linha


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PASTA_SESSAO, headless=False, channel="chrome",
            viewport={"width": 1400, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg_pl = ctx.new_page()

        ctx_wa = p.chromium.launch_persistent_context(
            PASTA_SESSAO_WA, headless=False, channel="chrome",
            viewport={"width": 1100, "height": 800})
        pg_wa = ctx_wa.pages[0] if ctx_wa.pages else ctx_wa.new_page()

        pg.bring_to_front()
        try:
            pg.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            pg_pl.goto(URL_PLANILHA, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            pg_wa.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        print("\n" + "=" * 60)
        print(" Prepare: PAINEL na lista | PLANILHA aberta | WHATSAPP logado")
        print("=" * 60)
        input(">> ENTER quando tudo estiver pronto... ")

        processados = 0
        colados = 0
        while processados < QUANTOS_LEADS:
            pg.bring_to_front()
            if "/indicacao" not in pg.url:
                pg.goto(URL_LISTA, wait_until="domcontentloaded")
            pg.wait_for_selector("table[role='grid'] tbody tr", timeout=30000)

            primeiro = pg.query_selector("tbody tr a[href*='/indicacao/detail/']")
            if not primeiro:
                print(">> Acabaram os leads!")
                break
            codigo = primeiro.get_attribute("href").rstrip("/").split("/")[-1]
            print(f"\n--- Lead {processados+1}/{QUANTOS_LEADS} (codigo {codigo}) ---")

            primeiro.click()
            pg.wait_for_load_state("domcontentloaded")
            pg.wait_for_selector("#Nome", timeout=30000)
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))

            nome_bruto = pg.eval_on_selector("#Nome", "el => el.value") or ""
            nome = limpar_nome(nome_bruto)
            f1_bruto = pg.eval_on_selector("#FonePrincipal", "el => el.value") or ""
            f2_bruto = pg.eval_on_selector("#FoneCelular", "el => el.value") or ""
            f1 = normalizar_telefone(f1_bruto)
            f2 = normalizar_telefone(f2_bruto)
            print(f"   Nome: {nome!r} (bruto: {nome_bruto!r})")
            print(f"   F1: {f1} | F2: {f2}")

            pg_wa.bring_to_front()
            existe = False
            numero_valido_bruto = ""
            if f1 and f1 == f2:
                existe = validar_no_whatsapp(pg_wa, f1)
                if existe:
                    numero_valido_bruto = f1_bruto
            else:
                if f1:
                    existe = validar_no_whatsapp(pg_wa, f1)
                    if existe:
                        numero_valido_bruto = f1_bruto
                if not existe and f2:
                    existe = validar_no_whatsapp(pg_wa, f2)
                    if existe:
                        numero_valido_bruto = f2_bruto
            print(f"   Existe no WhatsApp? {existe}")

            if existe:
                pg_pl.bring_to_front()
                pg_pl.wait_for_timeout(_tempo(None, "entre_acoes", 800))
                linha = colar_na_planilha(pg_pl, nome, numero_valido_bruto)
                print(f"   >> Colado na planilha (linha {linha})")
                colados += 1
                msg = MSG_EXISTE
            else:
                msg = MSG_NAO_EXISTE

            pg.bring_to_front()
            hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
            pg.eval_on_selector("#DtRetorno", f"el => el.value = '{hoje}'")
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))
            pg.fill("#historico-textarea", msg)
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))
            pg.click("text=Registrar")
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))
            pg.select_option("#IdStatus", STATUS_LIXO)
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))
            pg.select_option("#IdProduto", PRODUTO_NENHUM)
            pg.wait_for_timeout(_tempo(None, "entre_acoes", 800))
            pg.click("button[type='submit'].btn-primary")
            pg.wait_for_timeout(_tempo(None, "apos_confirmar_lead", 2000))
            print("   Confirmado no Painel.")
            processados += 1

        print("\n" + "=" * 60)
        print(f" TERMINOU! Processados: {processados} | Colados: {colados}")
        print(" >>> PRONTO, PODE DISPARAR! <<<")
        print("=" * 60)
        input("\nENTER pra fechar...")
        ctx.close()
        ctx_wa.close()


if __name__ == "__main__":
    main()
