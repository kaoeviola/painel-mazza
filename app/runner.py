import asyncio
import contextlib
import io
import json
import re
import shutil
import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.config import BASE_DIR, buscar_atendente, carregar_timings, config_do_modo, status_do_modo
from app.displays import display_manager
from app.execucoes import registrar_execucao
from app.notificador import enviar_resumo
from app.planilha import colar_leads, normalizar_telefone_planilha, telefones_existentes
from robo.robo_parte4_completo import aplicar_filtro_status, limpar_nome, normalizar_telefone


URL_BASE_PAINEL = "https://app.paineldocorretor.com.br"
URL_LISTA = "https://app.paineldocorretor.com.br/indicacao"
STATUS_LIXO = "13"
PRODUTO_NENHUM = "4"
MSG_EXISTE = "Em contato para cliente"
MSG_NAO_EXISTE = "Nao consegui contato com os numeros cadastrados / os numeros nao existem"
try:
    TIMEZONE_ROBO = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    TIMEZONE_ROBO = timezone(timedelta(hours=-3))

ESTADO_IDLE = "IDLE"
ESTADO_RODANDO = "RODANDO"
ESTADO_PAUSADO = "PAUSADO"
ESTADO_AGUARDANDO_CONFIRMACAO = "AGUARDANDO_CONFIRMACAO"
ESTADO_ERRO = "ERRO"
ESTADO_CONCLUIDO = "CONCLUIDO"
ESTADO_CANCELADO = "CANCELADO"
TIMEOUT_CONFIRMACAO_SEGUNDOS = 10 * 60

PERFIS_DIR = BASE_DIR / "perfis"
LOGS_DIR = BASE_DIR / "logs"


class ExecucaoInterrompida(Exception):
    def __init__(self, motivo: str = "Execucao interrompida pelo usuario.", estado_final: str = ESTADO_CONCLUIDO):
        super().__init__(motivo)
        self.motivo = motivo
        self.estado_final = estado_final


@dataclass
class Job:
    atendente_id: str
    tipo: str
    quantidade: int
    criado_em: datetime = field(default_factory=datetime.now)


class StreamCapturer(io.TextIOBase):
    def __init__(self, logger):
        self.logger = logger
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            linha, self._buffer = self._buffer.split("\n", 1)
            if linha.strip():
                self.logger(linha.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self.logger(self._buffer.rstrip())
        self._buffer = ""


class Runner:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._estado = ESTADO_IDLE
        self._erro: str | None = None
        self._current: Job | None = None
        self._fila: deque[Job] = deque()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._continue_event = threading.Event()
        self._contexts: list[Any] = []
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._recent_messages: deque[dict[str, Any]] = deque(maxlen=300)
        self._log_file = None
        self._contadores = self._contadores_iniciais()
        self._leads_com_erro: list[dict[str, str]] = []
        self._execucao_meta: dict[str, Any] | None = None
        self._ultimo_passo_fechamento_erro: str | None = None
        self._cancelamento_solicitado = False

    def _contadores_iniciais(self) -> dict[str, int]:
        return {
            "extraidos": 0,
            "validos": 0,
            "invalidos": 0,
            "colados": 0,
            "ja_existiam": 0,
            "erro_fechamento": 0,
        }

    def _tempo(self, timing: dict[str, Any] | None, chave: str) -> int:
        defaults = {
            "apos_navegacao": 3000,
            "entre_acoes": 800,
            "apos_abrir_dropdown": 600,
            "apos_pesquisar": 2500,
            "entre_leads": 1500,
            "apos_confirmar_lead": 2000,
            "validacao_whatsapp": 2500,
        }
        return int((timing or {}).get(chave, defaults[chave]))

    def iniciar(self, atendente_id: str, tipo: str, quantidade: int) -> dict[str, Any]:
        job = Job(atendente_id=atendente_id, tipo=tipo, quantidade=quantidade)
        with self._lock:
            if self._estado in {ESTADO_RODANDO, ESTADO_PAUSADO, ESTADO_AGUARDANDO_CONFIRMACAO} and self._current:
                self._fila.append(job)
                self._broadcast_estado()
                self._emitir_log(f"Execucao de {atendente_id} entrou na fila.")
                return {"status": "na_fila", "posicao": len(self._fila)}

            self._current = job
            self._estado = ESTADO_RODANDO
            self._erro = None
            self._stop_event.clear()
            self._continue_event.clear()
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()
            self._broadcast_estado()
            return {"status": "iniciado"}

    def parar(self, atendente_id: str) -> dict[str, Any]:
        with self._lock:
            if self._current and self._current.atendente_id == atendente_id:
                self._emitir_log("Solicitacao de parada recebida. Fechando navegadores...")
                self._cancelamento_solicitado = True
                self._stop_event.set()
                self._continue_event.set()
                self._estado = ESTADO_IDLE
                for ctx in list(self._contexts):
                    with contextlib.suppress(Exception):
                        ctx.close()
                self._broadcast_estado()
                return {"status": "parando"}

            removidos = 0
            nova_fila: deque[Job] = deque()
            for job in self._fila:
                if job.atendente_id == atendente_id:
                    removidos += 1
                else:
                    nova_fila.append(job)
            self._fila = nova_fila
            self._broadcast_estado()
            if removidos:
                return {"status": "removido_da_fila", "removidos": removidos}

            return {"status": "sem_execucao"}

    def continuar(self, atendente_id: str) -> dict[str, Any]:
        with self._lock:
            if not self._current or self._current.atendente_id != atendente_id:
                return {"status": "sem_execucao"}
            if self._estado != ESTADO_AGUARDANDO_CONFIRMACAO:
                return {"status": "estado_invalido", "estado": self._estado}

            self._continue_event.set()
            return {"status": "continuando"}

    def status(self, atendente_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            na_fila = [
                {"atendente_id": job.atendente_id, "tipo": job.tipo, "quantidade": job.quantidade}
                for job in self._fila
            ]
            minha_posicao = None
            if atendente_id:
                for index, job in enumerate(self._fila, start=1):
                    if job.atendente_id == atendente_id:
                        minha_posicao = index
                        break
            return {
                "estado": self._estado,
                "erro": self._erro,
                "rodando": self._estado == ESTADO_RODANDO,
                "aguardando_confirmacao": self._estado == ESTADO_AGUARDANDO_CONFIRMACAO,
                "atendente_dono": self._current.atendente_id if self._current else None,
                "janelas_ativas": bool(self._contexts),
                "tipo_extracao": self._current.tipo if self._current else None,
                "quantidade": self._current.quantidade if self._current else None,
                "fila": na_fila,
                "minha_posicao_fila": minha_posicao,
                "contadores": dict(self._contadores),
            }

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers[queue] = loop
            mensagens = list(self._recent_messages)
            estado = self.status()
        for mensagem in mensagens:
            await queue.put(mensagem)
        await queue.put({"tipo": "estado", **estado})
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                job = self._current
                self._estado = ESTADO_RODANDO
                self._erro = None
                self._contadores = self._contadores_iniciais()
                self._leads_com_erro = []
                self._ultimo_passo_fechamento_erro = None
                self._cancelamento_solicitado = False
                self._execucao_meta = self._nova_execucao_meta(job) if job else None
                self._continue_event.clear()
                self._broadcast_estado()

            if not job:
                return

            try:
                self._abrir_log(job)
                self._emitir_log("=" * 50)
                self._emitir_log(f"Iniciando extracao: atendente={job.atendente_id}, tipo={job.tipo}, quantidade={job.quantidade}")
                with contextlib.redirect_stdout(StreamCapturer(self._emitir_log)):
                    self._executar_job(job)
            except ExecucaoInterrompida as exc:
                self._emitir_log(exc.motivo)
                with self._lock:
                    self._estado = exc.estado_final
                    self._erro = exc.motivo if exc.estado_final == ESTADO_ERRO else None
            except PlaywrightError as exc:
                if self._erro_browser_fechado(exc):
                    self._emitir_log("Janelas fechadas pelo usuário — execução cancelada")
                    with self._lock:
                        self._estado = ESTADO_CANCELADO
                        self._erro = None
                else:
                    erro = traceback.format_exc()
                    self._emitir_log(erro)
                    with self._lock:
                        self._estado = ESTADO_ERRO
                        self._erro = erro
            except Exception:
                erro = traceback.format_exc()
                self._emitir_log(erro)
                with self._lock:
                    self._estado = ESTADO_ERRO
                    self._erro = erro
            finally:
                self._fechar_contextos()
                self._registrar_execucao_final(job)
                self._fechar_log()
                self._broadcast_estado()

            with self._lock:
                if self._fila:
                    self._current = self._fila.popleft()
                    self._stop_event.clear()
                    continue
                self._current = None
                self._stop_event.clear()
                self._broadcast_estado()
                return

    def _executar_job(self, job: Job) -> None:
        atendente = buscar_atendente(job.atendente_id)
        if not atendente:
            raise RuntimeError(f"Atendente {job.atendente_id!r} nao encontrado ou inativo.")

        self._preparar_perfil(job.atendente_id)
        pasta_painel = PERFIS_DIR / job.atendente_id / "sessao_painel"
        pasta_whatsapp = PERFIS_DIR / job.atendente_id / "sessao_whatsapp"
        planilha_id = atendente["planilha_id"]
        painel_hash = atendente.get("painel_hash", "")
        nome_atendente = atendente["nome"]
        modo_config = config_do_modo(job.tipo)
        timing = carregar_timings()

        if not painel_hash:
            self._emitir_log(f"painel_hash não configurado para o atendente {job.atendente_id} — configure em config/atendentes.json")
            raise ExecucaoInterrompida("Execução cancelada: painel_hash não configurado.", ESTADO_ERRO)

        display_painel = display_manager.garantir_display(atendente, "painel")
        display_whatsapp = display_manager.garantir_display(atendente, "whatsapp")
        if display_painel:
            self._emitir_log(f"Display Painel ativo para {job.atendente_id}: {display_painel.display} (noVNC {display_painel.novnc_port}).")
        if display_whatsapp:
            self._emitir_log(f"Display WhatsApp ativo para {job.atendente_id}: {display_whatsapp.display} (noVNC {display_whatsapp.novnc_port}).")

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(pasta_painel),
                **self._launch_options(atendente, "painel", {"width": 1400, "height": 900}),
                viewport={"width": 1400, "height": 900},
            )
            self._registrar_contexto(ctx)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()

            ctx_wa = p.chromium.launch_persistent_context(
                str(pasta_whatsapp),
                **self._launch_options(atendente, "whatsapp", {"width": 1400, "height": 900}),
                viewport={"width": 1400, "height": 900},
            )
            self._registrar_contexto(ctx_wa)
            pg_wa = ctx_wa.pages[0] if ctx_wa.pages else ctx_wa.new_page()

            self._emitir_log(f"Timings carregados: {timing}")
            self._abrir_paginas_iniciais(pg, pg_wa, painel_hash)
            self._aguardar_confirmacao_usuario()
            self._validar_prontidao_apos_confirmacao(pg, timing)

            self._emitir_log("Escrita na planilha sera feita pela API do Google Sheets.")
            status_filtro = status_do_modo(job.tipo)
            resultado_filtro = self._aplicar_filtro_com_debug(pg, status_filtro, timing)
            total_lista = resultado_filtro.get("total")
            if resultado_filtro.get("reutilizado"):
                self._emitir_log(f"Filtro já está correto ({', '.join(status_filtro)}), reutilizando")
            if total_lista is None:
                self._emitir_log(f"Filtro aplicado: {', '.join(status_filtro)}")
            else:
                self._emitir_log(f"Filtro aplicado: {', '.join(status_filtro)} — {total_lista} leads na lista")

            processados = 0
            colados = 0
            formula_coluna_b_logada = False
            ids_processados: set[str] = set()
            telefones_planilha = telefones_existentes(planilha_id)
            self._emitir_log(f"Telefones ja carregados da planilha: {len(telefones_planilha)}")
            while processados < job.quantidade:
                self._checar_parada()
                pg.bring_to_front()
                primeiro, codigo = self._primeiro_lead(pg, ids_processados)
                if not primeiro:
                    self._emitir_log("Lista vazia, nao carregou ou todos os leads visiveis ja foram processados. Parando.")
                    break

                ids_processados.add(codigo)
                self._emitir_log(f"[{processados + 1}/{job.quantidade}] Lead {codigo}")

                try:
                    primeiro.click()
                    pg.wait_for_load_state("domcontentloaded")
                    pg.wait_for_selector("#Nome", timeout=30000)
                    pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
                except PlaywrightError as exc:
                    if self._erro_browser_fechado(exc):
                        raise
                    self._emitir_log("Erro ao abrir o lead, pulando...")
                    self._emitir_log(traceback.format_exc())
                    pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
                    continue
                except Exception:
                    self._emitir_log("Erro ao abrir o lead, pulando...")
                    self._emitir_log(traceback.format_exc())
                    pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
                    continue

                nome = limpar_nome(pg.eval_on_selector("#Nome", "el => el.value") or "")
                f1b = pg.eval_on_selector("#FonePrincipal", "el => el.value") or ""
                f2b = pg.eval_on_selector("#FoneCelular", "el => el.value") or ""
                f1 = normalizar_telefone(f1b)
                f2 = normalizar_telefone(f2b)
                self._emitir_log(f"Nome: {nome}")

                pg_wa.bring_to_front()
                existe, num_bruto = self._validar_telefones(pg_wa, f1, f2, f1b, f2b)
                self._emitir_log(f"WhatsApp: {'EXISTE' if existe else 'nao existe'}")
                if existe:
                    self._incrementar("validos")
                else:
                    self._incrementar("invalidos")

                if existe:
                    try:
                        telefone_chave = normalizar_telefone_planilha(num_bruto)
                        if telefone_chave and telefone_chave in telefones_planilha:
                            self._emitir_log(f"Lead {nome} ja esta na planilha - pulando colagem.")
                            self._incrementar("ja_existiam")
                        else:
                            resultado_colagem = colar_leads(
                                planilha_id,
                                [
                                    {
                                        "nome": nome,
                                        "atendente": nome_atendente,
                                        "numero_bruto": num_bruto,
                                    }
                                ],
                            )
                            if telefone_chave:
                                telefones_planilha.add(telefone_chave)
                            if not formula_coluna_b_logada:
                                self._logar_formula_coluna_b(resultado_colagem)
                                formula_coluna_b_logada = True
                            escritos = resultado_colagem["escritos"]
                            linha_inicial = resultado_colagem["linha_inicial"]
                            self._emitir_log(f"Colado via API na linha {linha_inicial}.")
                            colados += escritos
                            self._set_contador("colados", colados)
                    except Exception as exc:
                        if isinstance(exc, PlaywrightError) and self._erro_browser_fechado(exc):
                            raise
                        self._emitir_log(f"Erro ao colar na planilha via API: {exc}")
                    msg = MSG_EXISTE
                else:
                    msg = MSG_NAO_EXISTE

                if not self._fechar_lead(pg, nome, modo_config, msg, timing):
                    self._emitir_log(f"Lead {nome}: erro_fechamento")
                    self._registrar_erro_fechamento(nome, self._ultimo_passo_fechamento_erro or "fechamento")
                processados += 1
                self._set_contador("extraidos", processados)
                pg.wait_for_timeout(self._tempo(timing, "entre_leads"))

            self._emitir_log("=" * 50)
            self._emitir_resumo_final(processados, colados)
            with self._lock:
                self._estado = ESTADO_CONCLUIDO

    def _launch_options(self, atendente: dict, servico: str, window_size: dict[str, int]) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": False,
            "env": display_manager.playwright_env(atendente, servico),
            "args": [f"--window-size={window_size['width']},{window_size['height']}"],
        }
        if not display_manager.em_vps():
            options["channel"] = "chrome"
        return options

    def _nova_execucao_meta(self, job: Job | None) -> dict[str, Any] | None:
        if not job:
            return None
        return {
            "id": str(uuid.uuid4()),
            "atendente": job.atendente_id,
            "modo": job.tipo,
            "quantidade_pedida": job.quantidade,
            "inicio_dt": datetime.now(TIMEZONE_ROBO),
        }

    def _registrar_execucao_final(self, job: Job | None) -> None:
        if not job or not self._execucao_meta:
            return

        fim_dt = datetime.now(TIMEZONE_ROBO)
        inicio_dt = self._execucao_meta["inicio_dt"]
        with self._lock:
            contadores = dict(self._contadores)
            leads_com_erro = list(self._leads_com_erro)
            erro_msg = self._erro
            estado = self._estado

        status_final = self._status_final_execucao(estado, contadores, leads_com_erro)
        registro = {
            "id": self._execucao_meta["id"],
            "atendente": self._execucao_meta["atendente"],
            "modo": self._execucao_meta["modo"],
            "quantidade_pedida": self._execucao_meta["quantidade_pedida"],
            "inicio": inicio_dt.isoformat(),
            "fim": fim_dt.isoformat(),
            "duracao_seg": int((fim_dt - inicio_dt).total_seconds()),
            "status_final": status_final,
            "contadores": contadores,
            "leads_com_erro": leads_com_erro,
            "erro_msg": erro_msg,
        }
        registrar_execucao(registro)
        self._notificar_execucao_final(job, registro)
        self._execucao_meta = None

    def _notificar_execucao_final(self, job: Job, registro: dict[str, Any]) -> None:
        if self._cancelamento_solicitado:
            return
        atendente = buscar_atendente(job.atendente_id)
        if not atendente:
            return
        try:
            ok, mensagem = enviar_resumo(atendente, registro)
        except Exception as exc:
            self._emitir_log(f"Falha ao enviar notificação WhatsApp: {exc}")
            return
        if mensagem:
            self._emitir_log(mensagem)

    def _status_final_execucao(self, estado: str, contadores: dict[str, int], leads_com_erro: list[dict[str, str]]) -> str:
        if estado == ESTADO_ERRO:
            return "erro"
        if estado == ESTADO_CANCELADO or self._cancelamento_solicitado:
            return "cancelado"
        if leads_com_erro or contadores.get("erro_fechamento", 0) > 0:
            return "concluido_com_avisos"
        return "concluido"

    def _abrir_paginas_iniciais(self, pg, pg_wa, painel_hash: str) -> None:
        url_inicial = f"{URL_BASE_PAINEL}/?hash={painel_hash}"
        for page, url in (
            (pg, url_inicial),
            (pg_wa, "https://web.whatsapp.com"),
        ):
            self._checar_parada()
            with contextlib.suppress(Exception):
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(self._tempo(carregar_timings(), "apos_navegacao"))

    def _aguardar_confirmacao_usuario(self) -> None:
        self._checar_parada()
        with self._lock:
            self._estado = ESTADO_AGUARDANDO_CONFIRMACAO
            self._continue_event.clear()
            self._broadcast_estado()

        self._emitir_log(
            "Janelas abertas. Verifique com calma o login do Painel e o WhatsApp Web. "
            "Clique em CONTINUAR quando estiver tudo pronto."
        )

        confirmado = self._continue_event.wait(TIMEOUT_CONFIRMACAO_SEGUNDOS)
        self._checar_parada()
        if not confirmado:
            raise ExecucaoInterrompida(
                "Tempo limite de 10 minutos aguardando confirmação. Execução cancelada.",
                ESTADO_IDLE,
            )

        with self._lock:
            self._estado = ESTADO_RODANDO
            self._broadcast_estado()

    def _validar_prontidao_apos_confirmacao(self, pg, timing: dict[str, Any]) -> None:
        if not self._painel_pronto(pg, logar=True, timing=timing):
            with self._lock:
                self._estado = ESTADO_AGUARDANDO_CONFIRMACAO
                self._continue_event.clear()
                self._broadcast_estado()
            self._emitir_log("Ainda na tela de login do Painel — faça o login e clique Continuar novamente.")
            self._aguardar_confirmacao_usuario()
            self._validar_prontidao_apos_confirmacao(pg, timing)

    def _painel_pronto(self, pg, logar: bool, timing: dict[str, Any] | None = None) -> bool:
        self._checar_parada()
        try:
            pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))

            if self._parece_login_painel(pg):
                if logar:
                    self._emitir_log("Ainda na tela de login do Painel — faça o login e clique Continuar novamente.")
                return False

            if "/indicacao" not in pg.url:
                pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(self._tempo(timing, "apos_navegacao"))

            if self._parece_login_painel(pg):
                if logar:
                    self._emitir_log("Painel redirecionou para login ao abrir Indicações — faça o login e clique Continuar novamente.")
                return False

            try:
                pg.wait_for_function(
                    """
                    () => {
                      const texto = document.body?.innerText || '';
                      return document.querySelector("table[role='grid']")
                        || document.querySelector("#IdStatus")
                        || /OPÇÕES DE FILTRO|OPCOES DE FILTRO/i.test(texto);
                    }
                    """,
                    timeout=30000,
                )
            except Exception:
                if logar:
                    self._emitir_log("Painel ainda não carregou a lista de indicações. Aguarde carregar e clique Continuar novamente.")
                return False

            self._expandir_filtros_se_possivel(pg, timing)
            return True
        except Exception as exc:
            if logar:
                self._emitir_log(f"Não foi possível validar o Painel do Corretor: {exc}. Clique Continuar novamente após ajustar.")
            return False

    def _parece_login_painel(self, pg) -> bool:
        try:
            url = pg.url.lower()
            if "login" in url or "account" in url:
                return True
            campos_senha = pg.locator("input[type='password']:visible").count()
            tabela = pg.locator("table[role='grid']").count()
            filtros = pg.get_by_text("OPÇÕES DE FILTRO").count() or pg.get_by_text("OPCOES DE FILTRO").count()
            return campos_senha > 0 and tabela == 0 and filtros == 0
        except Exception:
            return False

    def _whatsapp_pronto(self, pg_wa, logar: bool) -> bool:
        self._checar_parada()
        try:
            if "web.whatsapp.com" not in pg_wa.url:
                pg_wa.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
            pg_wa.wait_for_timeout(self._tempo(carregar_timings(), "apos_navegacao"))
            pronto = pg_wa.locator("div[contenteditable='true'][data-tab], [data-testid='chat-list'], #pane-side").count() > 0
            qr_visivel = pg_wa.locator("canvas, [data-testid='qrcode']").count() > 0
            if pronto:
                return True
            if logar:
                if qr_visivel:
                    self._emitir_log("WhatsApp Web ainda aguarda QR Code/pareamento — confirme e clique Continuar novamente.")
                else:
                    self._emitir_log("WhatsApp Web ainda não parece carregado — aguarde e clique Continuar novamente.")
            return False
        except Exception as exc:
            if logar:
                self._emitir_log(f"Não foi possível validar o WhatsApp Web: {exc}. Clique Continuar novamente após ajustar.")
            return False

    def _expandir_filtros_se_possivel(self, pg, timing: dict[str, Any] | None = None) -> None:
        if pg.locator("button.multiselect.dropdown-toggle:visible, .multiselect.dropdown-toggle:visible").count():
            return
        gatilho = pg.get_by_text("OPÇÕES DE FILTRO").first
        if not gatilho.count():
            gatilho = pg.get_by_text("OPCOES DE FILTRO").first
        if gatilho.count():
            with contextlib.suppress(Exception):
                gatilho.click()
                pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))

    def _aplicar_filtro_com_debug(self, pg, status_filtro: list[str], timing: dict[str, Any]) -> dict[str, Any]:
        dump = self._dump_filtro_status(pg)
        self._emitir_log(f"Diagnóstico filtro — URL atual: {dump['url']}")
        self._emitir_log(f"Diagnóstico filtro — HTML: {dump['html']}")
        self._emitir_log(f"Diagnóstico filtro — elementos k-: {dump['k_classes']}")
        try:
            return aplicar_filtro_status(pg, status_filtro, timing)
        except Exception:
            self._emitir_log(f"Falha ao aplicar filtro de status. URL atual: {pg.url}")
            self._emitir_log(f"HTML da região de filtros: {self._dump_filtro_status(pg)['html']}")
            raise

    def _dump_filtro_status(self, pg) -> dict[str, str]:
        try:
            dump = pg.evaluate(
                """
                () => {
                  const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const labels = [...document.querySelectorAll('label, span, div, td, th')]
                    .filter((el) => norm(el.textContent).toLowerCase() === 'status');
                  const label = labels[0];
                  let container = label || document.body;
                  for (let i = 0; i < 2 && container.parentElement; i += 1) {
                    container = container.parentElement;
                  }
                  const html = (container.outerHTML || document.body.outerHTML || '').slice(0, 3000);
                  const kClasses = [...container.querySelectorAll('[class*="k-"]')]
                    .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].filter((c) => c.startsWith('k-')).join('.')}`)
                    .filter((item) => !item.endsWith('.'))
                    .slice(0, 80);
                  return { html, kClasses };
                }
                """
            )
            return {
                "url": pg.url,
                "html": " ".join((dump.get("html") or "").split()),
                "k_classes": ", ".join(dump.get("kClasses") or []),
            }
        except Exception as exc:
            return {"url": pg.url, "html": f"(não foi possível capturar HTML dos filtros: {exc})", "k_classes": ""}

    def _primeiro_lead(self, pg, ids_processados: set[str]) -> tuple[Any | None, str | None]:
        for tentativa in range(3):
            self._checar_parada()
            try:
                pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_selector("table[role='grid'] tbody tr", timeout=45000)
                pg.wait_for_timeout(self._tempo(carregar_timings(), "apos_navegacao"))
                links = pg.query_selector_all("tbody tr a[href*='/indicacao/detail/']")
                for link in links:
                    href = link.get_attribute("href") or ""
                    codigo = href.rstrip("/").split("/")[-1]
                    if codigo and codigo not in ids_processados:
                        return link, codigo
                if links:
                    self._emitir_log("Todos os leads visiveis ja foram processados nesta execucao. Encerrando para evitar duplicidade.")
                    return None, None
            except PlaywrightError as exc:
                if self._erro_browser_fechado(exc):
                    raise
                self._emitir_log(f"Lista demorou, tentativa {tentativa + 1}/3, recarregando...")
                self._emitir_log(traceback.format_exc())
                pg.wait_for_timeout(self._tempo(carregar_timings(), "apos_navegacao"))
            except Exception:
                self._emitir_log(f"Lista demorou, tentativa {tentativa + 1}/3, recarregando...")
                self._emitir_log(traceback.format_exc())
                pg.wait_for_timeout(self._tempo(carregar_timings(), "apos_navegacao"))
        return None, None

    def _validar_telefones(self, pg_wa, f1: str, f2: str, f1b: str, f2b: str) -> tuple[bool, str]:
        existe = False
        num_bruto = ""
        if f1 and f1 == f2:
            existe = self._validar_wa(pg_wa, f1)
            if existe:
                num_bruto = f1b
        else:
            if f1:
                existe = self._validar_wa(pg_wa, f1)
                if existe:
                    num_bruto = f1b
            if not existe and f2:
                existe = self._validar_wa(pg_wa, f2)
                if existe:
                    num_bruto = f2b
        return existe, num_bruto

    def _validar_wa(self, pg_wa, numero: str) -> bool:
        self._checar_parada()
        if not numero:
            return False
        try:
            pg_wa.goto(f"https://web.whatsapp.com/send?phone={numero}", wait_until="domcontentloaded", timeout=60000)
            pg_wa.wait_for_timeout(self._tempo(carregar_timings(), "validacao_whatsapp"))
        except PlaywrightError as exc:
            if self._erro_browser_fechado(exc):
                raise
            self._checar_parada()
            return False
        except Exception:
            self._checar_parada()
            return False
        for _ in range(50):
            self._checar_parada()
            pg_wa.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))
            try:
                html = pg_wa.content().lower()
            except PlaywrightError as exc:
                if self._erro_browser_fechado(exc):
                    raise
                continue
            except Exception:
                continue
            sinais = [
                "url is invalid", "url Ã© invÃ¡lido", "url e invalido",
                "compartilhado atravÃ©s de url", "compartilhado atraves de url",
                "phone number shared", "nÃ£o estÃ¡ no whatsapp", "nao esta no whatsapp",
                "isn't on whatsapp", "is not on whatsapp",
            ]
            if any(s in html for s in sinais):
                return False
            if pg_wa.query_selector("div[contenteditable='true'][data-tab]"):
                return True
        return False

    def _achar_linha_vazia(self, pg_pl) -> int:
        def valor(cel: str) -> str:
            self._checar_parada()
            cx = pg_pl.query_selector("#t-name-box")
            cx.click()
            cx.press("Control+a")
            cx.press("Delete")
            pg_pl.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))
            cx.type(cel, delay=30)
            cx.press("Enter")
            pg_pl.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))
            v = pg_pl.eval_on_selector("#t-formula-bar-input", "el => el ? el.textContent : ''")
            return (v or "").strip()

        linha = 2
        while linha < 600:
            if valor(f"A{linha}") == "":
                return linha
            linha += 1
        return linha

    def _colar(self, pg_pl, linha: int, nome: str, atendente_nome: str, numero_bruto: str) -> None:
        def ir(cel: str) -> None:
            self._checar_parada()
            cx = pg_pl.query_selector("#t-name-box")
            cx.click()
            cx.press("Control+a")
            cx.press("Delete")
            pg_pl.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))
            cx.type(cel, delay=40)
            cx.press("Enter")
            pg_pl.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))

        def esc(cel: str, val: str) -> None:
            ir(cel)
            pg_pl.keyboard.type(str(val), delay=30)
            pg_pl.keyboard.press("Enter")
            pg_pl.wait_for_timeout(self._tempo(carregar_timings(), "entre_acoes"))

        esc(f"A{linha}", nome)
        esc(f"C{linha}", atendente_nome)
        esc(f"I{linha}", numero_bruto)

    def _logar_formula_coluna_b(self, resultado_colagem: dict[str, Any]) -> None:
        modo = resultado_colagem.get("formula_coluna_b")
        if modo == "arrayformula":
            self._emitir_log("Coluna B: ARRAYFORMULA detectada; nenhuma formula por linha foi copiada.")
        elif modo == "formula_por_linha":
            self._emitir_log("Coluna B: formula por linha detectada; formula copiada para a nova linha via API.")
        elif modo == "sem_formula_detectada":
            self._emitir_log("Coluna B: nenhuma formula detectada em B2:B5.")

    def _fechar_lead(self, pg, nome: str, modo_config: dict[str, Any], msg_fallback: str, timing: dict[str, Any]) -> bool:
        self._checar_parada()
        self._ultimo_passo_fechamento_erro = None
        status_destino = modo_config.get("status_destino") or "Lixo"
        produto_destino = modo_config.get("produto_destino") or "Nenhum"
        historico_msg = modo_config.get("historico_msg") or msg_fallback
        retorno = self._calcular_retorno(modo_config)
        pg.bring_to_front()

        if modo_config.get("preencher_data", True):
            if not self._executar_passo_fechamento("data", lambda: self._preencher_data_retorno_original(pg, retorno, timing)):
                self._voltar_lista_apos_erro_fechamento(pg)
                return False

        if not self._executar_passo_fechamento("status", lambda: self._selecionar_status_original(pg, status_destino, timing)):
            self._voltar_lista_apos_erro_fechamento(pg)
            return False

        if not self._executar_passo_fechamento("motivo", lambda: self._selecionar_motivo_status_original(pg, modo_config.get("status_motivo"), timing)):
            self._voltar_lista_apos_erro_fechamento(pg)
            return False

        if not self._executar_passo_fechamento("produto", lambda: self._selecionar_produto_original(pg, produto_destino, timing)):
            self._voltar_lista_apos_erro_fechamento(pg)
            return False

        if not self._executar_passo_fechamento("historico", lambda: self._registrar_historico_original(pg, historico_msg, timing)):
            self._voltar_lista_apos_erro_fechamento(pg)
            return False

        if not self._executar_passo_fechamento("confirmar", lambda: self._confirmar_lead_original(pg, timing)):
            self._voltar_lista_apos_erro_fechamento(pg)
            return False

        self._emitir_log(f"Lead {nome}: retorno {retorno} + status {status_destino} + produto {produto_destino} registrados")
        return True

    def _executar_passo_fechamento(self, passo: str, acao) -> bool:
        for tentativa in range(2):
            try:
                acao()
                return True
            except PlaywrightError as exc:
                if self._erro_browser_fechado(exc):
                    raise
                self._emitir_log(f"Erro no passo {passo} do fechamento (tentativa {tentativa + 1}/2):")
                self._emitir_log(traceback.format_exc())
            except Exception:
                self._checar_parada()
                self._emitir_log(f"Erro no passo {passo} do fechamento (tentativa {tentativa + 1}/2):")
                self._emitir_log(traceback.format_exc())
        self._ultimo_passo_fechamento_erro = passo
        return False

    def _calcular_retorno(self, modo_config: dict[str, Any]) -> str:
        dias = int(modo_config.get("retorno_dias") or 0)
        retorno = datetime.now(TIMEZONE_ROBO) + timedelta(days=dias)
        return retorno.strftime("%d/%m/%Y %H:%M")

    def _preencher_data_retorno_original(self, pg, retorno: str, timing: dict[str, Any]) -> None:
        pg.eval_on_selector("#DtRetorno", f"el => el.value = '{retorno}'")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))

    def _selecionar_status_original(self, pg, status_destino: str, timing: dict[str, Any]) -> None:
        value = self._valor_option_por_texto(pg, "#IdStatus", status_destino)
        if not value:
            raise RuntimeError(f"Status destino '{status_destino}' não encontrado em #IdStatus.")
        pg.select_option("#IdStatus", value)
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))

    def _selecionar_motivo_status_original(self, pg, status_motivo: str | None, timing: dict[str, Any]) -> None:
        motivo = pg.locator("#IdStatusMotivo").first
        if not motivo.count() or not motivo.is_visible(timeout=1000):
            return

        opcoes = motivo.locator("option").count()
        if opcoes <= 1:
            return

        if not status_motivo:
            self._emitir_log("Aviso: #IdStatusMotivo apareceu, mas status_motivo não está configurado em config/modos.json.")
            return

        value = self._valor_option_por_texto(pg, "#IdStatusMotivo", status_motivo)
        if not value:
            raise RuntimeError(f"Motivo de status '{status_motivo}' não encontrado em #IdStatusMotivo.")
        pg.select_option("#IdStatusMotivo", value)
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))

    def _selecionar_produto_original(self, pg, produto_destino: str, timing: dict[str, Any]) -> None:
        value = self._valor_option_por_texto(pg, "#IdProduto", produto_destino)
        if not value:
            raise RuntimeError(f"Produto destino '{produto_destino}' nao encontrado em #IdProduto.")
        pg.select_option("#IdProduto", value)
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
        selecionado = pg.eval_on_selector(
            "#IdProduto",
            "el => (el.options[el.selectedIndex]?.textContent || '').replace(/\\s+/g, ' ').trim()",
        )
        if selecionado != produto_destino:
            raise RuntimeError(f"Produto selecionado ficou '{selecionado}', esperado '{produto_destino}'.")
        self._emitir_log(f"produto: {selecionado}")

    def _registrar_historico_original(self, pg, mensagem: str, timing: dict[str, Any]) -> None:
        pg.fill("#historico-textarea", mensagem)
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
        pg.click("text=Registrar")
        pg.wait_for_timeout(max(self._tempo(timing, "entre_acoes"), 1800))

    def _confirmar_lead_original(self, pg, timing: dict[str, Any]) -> None:
        botao = pg.locator("#indicacao-form .form-actions button[type='submit']").filter(
            has_text=re.compile(r"^\s*Confirmar\s*$")
        ).last
        if not botao.count():
            raise RuntimeError("Botao Confirmar nao encontrado no formulario principal #indicacao-form.")

        botao.scroll_into_view_if_needed(timeout=5000)
        pg.wait_for_timeout(self._tempo(timing, "entre_acoes"))
        botao.click(timeout=10000)

        if not self._aguardar_volta_lista(pg, timeout=15000):
            validacao = self._texto_validacao_formulario(pg)
            if validacao:
                self._emitir_log(f"Validacao do formulario: {validacao}")
            self._emitir_log("Confirmar clicado mas pagina nao voltou a lista")
            raise RuntimeError("Confirmar nao retornou para a lista de indicacoes.")

        pg.wait_for_timeout(max(self._tempo(timing, "apos_confirmar_lead"), 2200))

    def _aguardar_volta_lista(self, pg, timeout: int) -> bool:
        try:
            pg.wait_for_function(
                """
                () => {
                  const path = window.location.pathname.replace(/\\/$/, '');
                  const estaNaLista = path === '/indicacao';
                  const tabelaLista = document.querySelector("table[role='grid'] tbody tr");
                  const formDetalhe = document.querySelector("#indicacao-form");
                  return estaNaLista || (!!tabelaLista && !formDetalhe);
                }
                """,
                timeout=timeout,
            )
            return True
        except PlaywrightError as exc:
            if self._erro_browser_fechado(exc):
                raise
            return False
        except Exception:
            return False

    def _texto_validacao_formulario(self, pg) -> str:
        try:
            textos = pg.locator(
                ".validation-summary:visible, .validation-summary-errors:visible, "
                ".field-validation-error:visible, .help-block.error:visible"
            ).all_text_contents()
            return " | ".join(" ".join(texto.split()) for texto in textos if texto.strip())
        except Exception:
            return ""

    def _valor_option_por_texto(self, pg, seletor: str, texto: str) -> str | None:
        return pg.evaluate(
            """
            ({ seletor, texto }) => {
              const normalizar = (valor) => (valor || '').replace(/\\s+/g, ' ').trim();
              const select = document.querySelector(seletor);
              if (!select) return null;
              const option = [...select.options].find((item) => normalizar(item.textContent) === normalizar(texto));
              return option ? option.value : null;
            }
            """,
            {"seletor": seletor, "texto": texto},
        )

    def _voltar_lista_apos_erro_fechamento(self, pg) -> None:
        with contextlib.suppress(Exception):
            pg.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_selector("table[role='grid'] tbody tr", timeout=45000)

    def _erro_browser_fechado(self, exc: Exception) -> bool:
        texto = str(exc).lower()
        sinais = [
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "context closed",
            "page closed",
        ]
        return any(sinal in texto for sinal in sinais)

    def _preparar_perfil(self, atendente_id: str) -> None:
        perfil = PERFIS_DIR / atendente_id
        painel_dest = perfil / "sessao_painel"
        whatsapp_dest = perfil / "sessao_whatsapp"
        painel_dest.mkdir(parents=True, exist_ok=True)
        whatsapp_dest.mkdir(parents=True, exist_ok=True)

        if atendente_id == "kaoe":
            for origem_nome, destino in (
                ("sessao_painel", painel_dest),
                ("sessao_whatsapp", whatsapp_dest),
            ):
                origem = BASE_DIR / origem_nome
                marcador = destino / ".migrado_kaoe"
                if origem.exists() and not marcador.exists():
                    self._emitir_log(f"Migrando perfil existente {origem_nome} para {destino}.")
                    shutil.copytree(
                        origem,
                        destino,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("LOCK", "Singleton*", "*.tmp"),
                    )
                    marcador.write_text(datetime.now().isoformat(), encoding="utf-8")

    def _url_planilha(self, planilha_id: str) -> str:
        if planilha_id.startswith("http"):
            return planilha_id
        return f"https://docs.google.com/spreadsheets/d/{planilha_id}/edit#gid=0"

    def _checar_parada(self) -> None:
        if self._stop_event.is_set():
            raise ExecucaoInterrompida("Execução cancelada pelo usuário.", ESTADO_IDLE)

    def _registrar_contexto(self, ctx) -> None:
        with self._lock:
            self._contexts.append(ctx)

    def _fechar_contextos(self) -> None:
        with self._lock:
            contexts = list(self._contexts)
            self._contexts.clear()
        for ctx in contexts:
            with contextlib.suppress(Exception):
                ctx.close()

    def _abrir_log(self, job: Job) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        caminho = LOGS_DIR / f"{job.atendente_id}_{timestamp}.log"
        self._log_file = caminho.open("a", encoding="utf-8")

    def _fechar_log(self) -> None:
        if self._log_file:
            with contextlib.suppress(Exception):
                self._log_file.close()
        self._log_file = None

    def _emitir_log(self, linha: str) -> None:
        mensagem = {"tipo": "log", "linha": linha, "hora": datetime.now().strftime("%H:%M:%S")}
        self._salvar_linha_log(mensagem)
        self._broadcast(mensagem)

    def _salvar_linha_log(self, mensagem: dict[str, Any]) -> None:
        if self._log_file:
            with contextlib.suppress(Exception):
                self._log_file.write(f"[{mensagem['hora']}] {mensagem['linha']}\n")
                self._log_file.flush()

    def _incrementar(self, campo: str) -> None:
        with self._lock:
            self._contadores[campo] += 1
            valor = self._contadores[campo]
        self._broadcast({"tipo": "contador", "campo": campo, "valor": valor})

    def _set_contador(self, campo: str, valor: int) -> None:
        with self._lock:
            self._contadores[campo] = valor
        self._broadcast({"tipo": "contador", "campo": campo, "valor": valor})

    def _registrar_erro_fechamento(self, nome: str, passo: str) -> None:
        with self._lock:
            self._contadores["erro_fechamento"] += 1
            valor = self._contadores["erro_fechamento"]
            self._leads_com_erro.append({"nome": nome or "(sem nome)", "passo": passo})
        self._broadcast({"tipo": "contador", "campo": "erro_fechamento", "valor": valor})

    def _emitir_resumo_final(self, processados: int, colados: int) -> None:
        with self._lock:
            ja_existiam = self._contadores.get("ja_existiam", 0)
            erros = self._contadores.get("erro_fechamento", 0)
            leads_com_erro = list(self._leads_com_erro)

        nomes = ", ".join(f"{item['nome']} ({item['passo']})" for item in leads_com_erro) or "-"
        self._emitir_log(
            f"TERMINOU! Processados: {processados} | Colados: {colados} | "
            f"Já existiam: {ja_existiam} | Erros de fechamento: {erros}"
            + (f" (nomes: {nomes})" if erros else "")
        )
        if erros:
            for item in leads_com_erro:
                self._emitir_log(f"Erro parcial: {item['nome']} — passo {item['passo']}")
            self._emitir_log("Concluído com avisos: houve erros parciais durante o fechamento.")
        else:
            self._emitir_log("Concluído sem erros parciais.")

    def _broadcast_estado(self) -> None:
        self._broadcast({"tipo": "estado", **self.status()})

    def _broadcast(self, mensagem: dict[str, Any]) -> None:
        with self._lock:
            self._recent_messages.append(mensagem)
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            if loop and loop.is_running():
                loop.call_soon_threadsafe(queue.put_nowait, mensagem)
            else:
                with contextlib.suppress(Exception):
                    queue.put_nowait(mensagem)


runner = Runner()
