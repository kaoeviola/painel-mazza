#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHECADOR DE LEADS - Painel do Corretor -> Telegram
====================================================
Gatilho: COR LARANJA do badge do codigo (pega 'Nova' E leads
transferidos por admin, qualquer origem/status/horario).

Regras:
  - Lead laranja aparece -> avisa na hora (com botao "Abrir no painel").
  - Continua laranja e voce nao mexeu -> re-avisa a cada 10 min.
  - Voce trabalha o lead (sai do laranja) -> para sozinho.
  - Roda 24h. Nao clica em nenhum lead (le so a lista).

Segredos via env (EasyPanel -> Ambiente):
  PAINEL_USER, PAINEL_PASS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os, json, time, traceback, re
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
from playwright.sync_api import sync_playwright

TZ = ZoneInfo("America/Sao_Paulo")
PASTA_SESSAO = "./sessao_painel"
ARQ_ESTADO = "checador_estado.json"
URL_INICIAL = "https://app.paineldocorretor.com.br/?hash=yKR2qB"
URL_LISTA = "https://app.paineldocorretor.com.br/indicacao"
URL_DETALHE = "https://app.paineldocorretor.com.br/indicacao/detail/"

PAINEL_USER = os.environ.get("PAINEL_USER", "")
PAINEL_PASS = os.environ.get("PAINEL_PASS", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

INTERVALO_SEG = int(os.environ.get("CHECADOR_INTERVALO_SEG", "120"))
REAVISO_MIN = int(os.environ.get("CHECADOR_REAVISO_MIN", "10"))  # re-aviso a cada 10 min
# Diagnostico: loga todas as cores encontradas (util nas 1as semanas).
# Desligue pondo CHECADOR_DEBUG_CORES=0 no env quando estiver confiante.
DEBUG_CORES = os.environ.get("CHECADOR_DEBUG_CORES", "1") == "1"

def log(msg):
    print(f"[{datetime.now(TZ).strftime('%d/%m %H:%M:%S')}] {msg}", flush=True)

def carrega_estado():
    if not os.path.exists(ARQ_ESTADO): return {}
    try:
        with open(ARQ_ESTADO) as f: return json.load(f)
    except Exception: return {}

def salva_estado(estado):
    tmp = ARQ_ESTADO + ".tmp"
    with open(tmp, "w") as f: json.dump(estado, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQ_ESTADO)

def envia_telegram(texto, botao_url=None, botao_texto=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("!! TELEGRAM_TOKEN/CHAT_ID nao configurados."); return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if botao_url and botao_texto:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": botao_texto, "url": botao_url}]]}
    try:
        r = httpx.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log(f"!! Telegram {r.status_code}: {r.text[:200]}"); return False
        return True
    except Exception as e:
        log(f"!! Erro Telegram: {e}"); return False

def cor_para_rgb(cor):
    """Converte '#F89B21' ou 'rgb(248, 155, 33)' em (r,g,b). None se falhar."""
    if not cor: return None
    cor = cor.strip()
    m = re.match(r"#([0-9a-fA-F]{6})", cor)
    if m:
        h = m.group(1)
        return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16))
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", cor)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None

def eh_laranja(cor):
    """Detecta a familia laranja: vermelho alto, verde medio, azul baixo.
    Cobre #F89B21 e tons vizinhos. Nao colide com azul/verde/preto conhecidos."""
    rgb = cor_para_rgb(cor)
    if not rgb: return False
    r, g, b = rgb
    return (r >= 200 and 100 <= g <= 200 and b <= 100 and (r - b) >= 120)

def le_leads(pg):
    pg.wait_for_selector("table[role='grid'] tbody tr", timeout=25000)
    pg.wait_for_timeout(1500)
    leads = []
    for tr in pg.query_selector_all("table[role='grid'] tbody tr"):
        badge = tr.query_selector("span.badge")
        if not badge: continue
        tds = tr.query_selector_all("td")
        def td(i): return tds[i].inner_text().strip() if len(tds) > i else ""
        st = badge.get_attribute("style") or ""
        cor = st.replace("background-color:", "").replace(";", "").strip()
        leads.append({"codigo": badge.inner_text().strip(),
            "status": (badge.get_attribute("title") or "").strip(),
            "cor": cor, "solicitacao": td(2), "nome": td(4),
            "cidade": td(5), "fonte": td(6), "modalidade": td(7)})
    return leads

def garante_logado(pg):
    pg.goto(URL_INICIAL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(2000)
    if pg.query_selector("input[type='password']"):
        log(">> Sessao caiu. Logando...")
        for c in pg.query_selector_all("input"):
            if (c.get_attribute("type") or "") in ("text", "email"):
                c.fill(PAINEL_USER); break
        pg.query_selector("input[type='password']").fill(PAINEL_PASS)
        chk = pg.query_selector("input[type='checkbox']")
        if chk:
            try: chk.check()
            except Exception: pass
        b = pg.query_selector("button:has-text('Entrar'), input[value='Entrar']")
        (b.click() if b else pg.query_selector("input[type='password']").press("Enter"))
        pg.wait_for_timeout(4000); log(">> Login ok.")
    pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)

def monta_msg(lead):
    return ("🔔 <b>LEAD PRA ATENDER — Mazza</b>\n"
        f"<b>Código:</b> {lead['codigo']}\n"
        f"<b>Nome:</b> {lead['nome'] or '(sem nome)'}\n"
        f"<b>Cidade:</b> {lead['cidade'] or '-'}\n"
        f"<b>Fonte:</b> {lead['fonte'] or '-'} · {lead['modalidade'] or '-'}\n"
        f"<b>Entrou:</b> {lead['solicitacao'] or '-'}")

def processa(leads, estado):
    agora = datetime.now(TZ)
    agora_ts = agora.timestamp()
    vistos = set()
    mudou = False

    if DEBUG_CORES:
        cores = [f"{l['codigo']}:{l['cor']}" for l in leads[:10]]
        log(f"   [debug cores] {cores}")

    for lead in leads:
        if not eh_laranja(lead["cor"]):
            continue  # so laranja dispara
        cod = lead["codigo"]
        vistos.add(cod)
        reg = estado.get(cod, {})
        ultimo = reg.get("ultimo_aviso_ts", 0)
        minutos_desde = (agora_ts - ultimo) / 60.0

        # primeira vez que vejo, ou ja passou o intervalo de re-aviso
        if ultimo == 0 or minutos_desde >= REAVISO_MIN:
            botao_url = URL_DETALHE + cod
            primeira = (ultimo == 0)
            texto = monta_msg(lead)
            if not primeira:
                texto = "♻️ <b>(ainda pendente)</b>\n" + texto
            if envia_telegram(texto, botao_url=botao_url, botao_texto="🔗 Abrir no painel"):
                reg["ultimo_aviso_ts"] = agora_ts
                estado[cod] = reg
                mudou = True
                tag = "1o aviso" if primeira else "re-aviso"
                log(f">> Avisei cod {cod} [{tag}] cor {lead['cor']}")

    # limpeza: leads que nao estao mais laranja saem do estado
    for cod in list(estado.keys()):
        if cod not in vistos:
            del estado[cod]; mudou = True
    if mudou: salva_estado(estado)

def uma_rodada():
    estado = carrega_estado()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PASTA_SESSAO, headless=True,
            viewport={"width": 1400, "height": 900})
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            garante_logado(pg)
            leads = le_leads(pg)
            n = sum(1 for l in leads if eh_laranja(l["cor"]))
            log(f">> Li {len(leads)} leads. Laranjas: {n}")
            processa(leads, estado)
        finally: ctx.close()

def main():
    log("=== CHECADOR INICIADO (gatilho: cor laranja) ===")
    log(f"Intervalo {INTERVALO_SEG}s | Re-aviso {REAVISO_MIN}min | 24h | Debug cores: {DEBUG_CORES}")
    envia_telegram("🟢 Checador Mazza iniciado e no ar.")
    while True:
        try:
            uma_rodada()
        except Exception as e:
            log(f"!! ERRO NA RODADA: {e}")
            log(traceback.format_exc())
        time.sleep(INTERVALO_SEG)

if __name__ == "__main__":
    main()
