import re
import threading
from datetime import datetime
import tkinter as tk
from tkinter import scrolledtext, messagebox
from playwright.sync_api import sync_playwright

# ================== CONFIG ==================
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
    return re.sub(r'\s+', ' ', nome).strip()


def normalizar_telefone(bruto):
    if not bruto:
        return ""
    d = re.sub(r"\D", "", bruto)
    if not d.startswith("55"):
        d = "55" + d
    ddd, numero = d[2:4], d[4:]
    if len(numero) == 9 and numero[0] == "9":
        numero = numero[1:]
    if len(ddd) != 2 or len(numero) < 8:
        return ""
    return "55" + ddd + numero


class RoboApp:
    def __init__(self, root):
        self.root = root
        root.title("Robo Painel do Corretor - Mazza")
        root.geometry("560x520")
        root.configure(bg="#0f172a")

        tk.Label(root, text="ROBO DE PROSPECCAO", bg="#0f172a", fg="#38bdf8",
                 font=("Segoe UI", 16, "bold")).pack(pady=(16, 4))
        tk.Label(root, text="Painel do Corretor -> valida -> planilha",
                 bg="#0f172a", fg="#94a3b8", font=("Segoe UI", 9)).pack()

        frm = tk.Frame(root, bg="#0f172a")
        frm.pack(pady=16)
        tk.Label(frm, text="Quantos leads hoje?", bg="#0f172a", fg="#e2e8f0",
                 font=("Segoe UI", 11)).grid(row=0, column=0, padx=6)
        self.qtd = tk.Entry(frm, width=8, font=("Segoe UI", 13), justify="center")
        self.qtd.insert(0, "50")
        self.qtd.grid(row=0, column=1, padx=6)

        btns = tk.Frame(root, bg="#0f172a")
        btns.pack(pady=6)
        self.btn_iniciar = tk.Button(btns, text="INICIAR", bg="#22c55e", fg="white",
                                     font=("Segoe UI", 12, "bold"), width=14, height=1,
                                     command=self.iniciar, relief="flat", cursor="hand2")
        self.btn_iniciar.grid(row=0, column=0, padx=6)
        self.btn_limpar = tk.Button(btns, text="LIMPAR PLANILHA", bg="#ef4444", fg="white",
                                    font=("Segoe UI", 12, "bold"), width=16, height=1,
                                    command=self.limpar, relief="flat", cursor="hand2")
        self.btn_limpar.grid(row=0, column=1, padx=6)

        self.log = scrolledtext.ScrolledText(root, width=64, height=16, bg="#1e293b",
                                             fg="#e2e8f0", font=("Consolas", 9), relief="flat")
        self.log.pack(padx=16, pady=12)
        self.rodando = False

    def escreve(self, txt):
        self.log.insert(tk.END, txt + "\n")
        self.log.see(tk.END)
        self.root.update_idletasks()

    def iniciar(self):
        if self.rodando:
            return
        try:
            n = int(self.qtd.get())
            if n < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Erro", "Digite um numero valido de leads.")
            return
        self.rodando = True
        self.btn_iniciar.config(state="disabled", text="RODANDO...")
        self.btn_limpar.config(state="disabled")
        threading.Thread(target=self._rodar_robo, args=(n,), daemon=True).start()

    def limpar(self):
        if self.rodando:
            return
        if not messagebox.askyesno("Confirmar", "Apagar TODOS os dados da planilha (menos o cabecalho)?"):
            return
        self.rodando = True
        self.btn_limpar.config(state="disabled", text="LIMPANDO...")
        self.btn_iniciar.config(state="disabled")
        threading.Thread(target=self._limpar_planilha, daemon=True).start()

    def _limpar_planilha(self):
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    PASTA_SESSAO, headless=False, channel="chrome",
                    viewport={"width": 1300, "height": 850})
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                self.escreve(">> Abrindo planilha...")
                pg.goto(URL_PLANILHA, wait_until="domcontentloaded")
                pg.wait_for_selector("#t-name-box", timeout=30000)
                pg.wait_for_timeout(1500)
                cx = pg.query_selector("#t-name-box")
                cx.click(); cx.press("Control+a"); cx.press("Delete")
                pg.wait_for_timeout(150)
                cx.type("A2:I500", delay=40); cx.press("Enter")
                pg.wait_for_timeout(400)
                pg.keyboard.press("Delete")
                pg.wait_for_timeout(1500)
                self.escreve(">> Planilha limpa! (cabecalho mantido)")
                pg.wait_for_timeout(1000)
                ctx.close()
        except Exception as e:
            self.escreve(f"!! Erro ao limpar: {e}")
        finally:
            self.rodando = False
            self.root.after(0, lambda: self.btn_limpar.config(state="normal", text="LIMPAR PLANILHA"))
            self.root.after(0, lambda: self.btn_iniciar.config(state="normal"))

    def _validar_wa(self, pg_wa, numero):
        if not numero:
            return False
        try:
            pg_wa.goto(f"https://web.whatsapp.com/send?phone={numero}", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            return False
        for _ in range(50):
            pg_wa.wait_for_timeout(500)
            try:
                html = pg_wa.content().lower()
            except Exception:
                continue
            sinais = ["url is invalid", "url é inválido", "url e invalido",
                      "compartilhado através de url", "compartilhado atraves de url",
                      "phone number shared", "não está no whatsapp", "nao esta no whatsapp",
                      "isn't on whatsapp", "is not on whatsapp"]
            if any(s in html for s in sinais):
                return False
            if pg_wa.query_selector("div[contenteditable='true'][data-tab]"):
                return True
        return False

    def _achar_linha_vazia(self, pg_pl):
        def valor(cel):
            cx = pg_pl.query_selector("#t-name-box")
            cx.click(); cx.press("Control+a"); cx.press("Delete")
            pg_pl.wait_for_timeout(80)
            cx.type(cel, delay=30); cx.press("Enter")
            pg_pl.wait_for_timeout(200)
            v = pg_pl.eval_on_selector("#t-formula-bar-input", "el => el ? el.textContent : ''")
            return (v or "").strip()
        linha = 2
        while linha < 600:
            if valor(f"A{linha}") == "":
                return linha
            linha += 1
        return linha

    def _colar(self, pg_pl, linha, nome, numero_bruto):
        def ir(cel):
            cx = pg_pl.query_selector("#t-name-box")
            cx.click(); cx.press("Control+a"); cx.press("Delete")
            pg_pl.wait_for_timeout(120)
            cx.type(cel, delay=40); cx.press("Enter")
            pg_pl.wait_for_timeout(300)
        def esc(cel, val):
            ir(cel)
            pg_pl.keyboard.type(str(val), delay=30)
            pg_pl.keyboard.press("Enter")
            pg_pl.wait_for_timeout(300)
        esc(f"A{linha}", nome)
        esc(f"C{linha}", ATENDENTE)
        esc(f"I{linha}", numero_bruto)

    def _rodar_robo(self, quantos):
        try:
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
                try: pg.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
                except Exception: pass
                try: pg_pl.goto(URL_PLANILHA, wait_until="domcontentloaded", timeout=60000)
                except Exception: pass
                try: pg_wa.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
                except Exception: pass

                self.escreve("=" * 50)
                self.escreve(" PREPARE (nas janelas que abriram):")
                self.escreve(" 1. Logue no PAINEL e va ate a LISTA de leads")
                self.escreve(" 2. Confirme a PLANILHA aberta")
                self.escreve(" 3. Logue no WHATSAPP WEB (chip de validacao)")
                self.escreve("=" * 50)

                ok = {"v": False}
                ev = threading.Event()
                def wrap():
                    ok["v"] = messagebox.askokcancel("Pronto?",
                        "Ja logou no Painel (na lista), Planilha e WhatsApp?\n\nClique OK para comecar.")
                    ev.set()
                self.root.after(0, wrap)
                ev.wait()
                if not ok["v"]:
                    self.escreve(">> Cancelado.")
                    ctx.close(); ctx_wa.close()
                    return

                processados = 0; colados = 0
                linha_atual = self._achar_linha_vazia(pg_pl)
                self.escreve(f">> Vou colar a partir da linha {linha_atual}")

                while processados < quantos:
                    pg.bring_to_front()
                    # ---- carrega a lista com ate 3 tentativas ----
                    primeiro = None
                    for tentativa in range(3):
                        try:
                            pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
                            pg.wait_for_selector("table[role='grid'] tbody tr", timeout=45000)
                            primeiro = pg.query_selector("tbody tr a[href*='/indicacao/detail/']")
                            if primeiro:
                                break
                        except Exception:
                            self.escreve(f"   (lista demorou, tentativa {tentativa+1}/3, recarregando...)")
                            pg.wait_for_timeout(3000)
                    if not primeiro:
                        self.escreve(">> Lista vazia ou nao carregou apos 3 tentativas. Parando.")
                        break

                    codigo = primeiro.get_attribute("href").rstrip("/").split("/")[-1]
                    self.escreve(f"\n[{processados+1}/{quantos}] Lead {codigo}")

                    # ---- abre o lead (com protecao) ----
                    try:
                        primeiro.click()
                        pg.wait_for_load_state("domcontentloaded")
                        pg.wait_for_selector("#Nome", timeout=30000)
                        pg.wait_for_timeout(1000)
                    except Exception:
                        self.escreve("   (erro ao abrir o lead, pulando...)")
                        pg.wait_for_timeout(2000)
                        continue

                    nome = limpar_nome(pg.eval_on_selector("#Nome", "el => el.value") or "")
                    f1b = pg.eval_on_selector("#FonePrincipal", "el => el.value") or ""
                    f2b = pg.eval_on_selector("#FoneCelular", "el => el.value") or ""
                    f1 = normalizar_telefone(f1b); f2 = normalizar_telefone(f2b)
                    self.escreve(f"   {nome}")

                    pg_wa.bring_to_front()
                    existe = False; num_bruto = ""
                    if f1 and f1 == f2:
                        existe = self._validar_wa(pg_wa, f1)
                        if existe: num_bruto = f1b
                    else:
                        if f1:
                            existe = self._validar_wa(pg_wa, f1)
                            if existe: num_bruto = f1b
                        if not existe and f2:
                            existe = self._validar_wa(pg_wa, f2)
                            if existe: num_bruto = f2b
                    self.escreve(f"   WhatsApp: {'EXISTE' if existe else 'nao existe'}")

                    if existe:
                        pg_pl.bring_to_front(); pg_pl.wait_for_timeout(600)
                        try:
                            self._colar(pg_pl, linha_atual, nome, num_bruto)
                            self.escreve(f"   -> colado na linha {linha_atual}")
                            linha_atual += 1
                            colados += 1
                        except Exception:
                            self.escreve("   (erro ao colar na planilha, continuando...)")
                        msg = MSG_EXISTE
                    else:
                        msg = MSG_NAO_EXISTE

                    # ---- fecha o lead no Painel (com protecao) ----
                    try:
                        pg.bring_to_front()
                        hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
                        pg.eval_on_selector("#DtRetorno", f"el => el.value = '{hoje}'")
                        pg.wait_for_timeout(250)
                        pg.fill("#historico-textarea", msg)
                        pg.wait_for_timeout(350)
                        pg.click("text=Registrar")
                        pg.wait_for_timeout(1800)
                        pg.select_option("#IdStatus", STATUS_LIXO)
                        pg.wait_for_timeout(350)
                        pg.select_option("#IdProduto", PRODUTO_NENHUM)
                        pg.wait_for_timeout(350)
                        pg.click("button[type='submit'].btn-primary")
                        pg.wait_for_timeout(2200)
                    except Exception:
                        self.escreve("   (erro ao fechar o lead no Painel, continuando...)")
                        pg.wait_for_timeout(2000)

                    processados += 1

                self.escreve("\n" + "=" * 50)
                self.escreve(f" TERMINOU! Processados: {processados} | Colados: {colados}")
                self.escreve(" >>> PRONTO, PODE DISPARAR! <<<")
                self.escreve("=" * 50)
                self.root.after(0, lambda: messagebox.showinfo("Pronto!",
                    f"Terminou!\n\nProcessados: {processados}\nColados na planilha: {colados}\n\nPODE DISPARAR!"))
                self.escreve(">> Chromes deixados abertos. Feche a telinha quando quiser encerrar.")
                # mantem os Chromes abertos ate voce fechar a telinha
                while True:
                    pg.wait_for_timeout(5000)
        except Exception as e:
            self.escreve(f"!! ERRO: {e}")
        finally:
            self.rodando = False
            self.root.after(0, lambda: self.btn_iniciar.config(state="normal", text="INICIAR"))
            self.root.after(0, lambda: self.btn_limpar.config(state="normal"))


if __name__ == "__main__":
    root = tk.Tk()
    app = RoboApp(root)
    root.mainloop()