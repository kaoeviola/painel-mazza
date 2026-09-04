import contextlib
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from playwright.sync_api import sync_playwright

from app.config import BASE_DIR
from app.displays import display_manager


PERFIS_DIR = BASE_DIR / "perfis"
SETUP_TIMEOUT_SEGUNDOS = 10 * 60
SERVICOS = {"painel", "whatsapp"}


@dataclass
class SetupSession:
    atendente_id: str
    servico: str
    iniciado_em: float
    done: threading.Event
    context: Any | None = None
    thread: threading.Thread | None = None


class SetupConexoesManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SetupSession] = {}

    def iniciar(self, atendente: dict, servico: str, logger=None) -> dict[str, Any]:
        self._validar_servico(servico)
        if servico == "painel" and not atendente.get("painel_hash"):
            raise RuntimeError(f"painel_hash nao configurado para o atendente {atendente['id']}.")

        atendente_id = atendente["id"]
        with self._lock:
            existente = self._sessions.get(atendente_id)
            if existente and self._ativa(existente):
                return {
                    "status": "ja_ativo",
                    "servico": existente.servico,
                    "vnc": display_manager.status(atendente),
                }
            if existente:
                self._sessions.pop(atendente_id, None)

            self._preparar_perfil(atendente_id)
            display_manager.garantir_display(atendente, servico)
            session = SetupSession(
                atendente_id=atendente_id,
                servico=servico,
                iniciado_em=time.time(),
                done=threading.Event(),
            )
            thread = threading.Thread(
                target=self._worker,
                args=(session, dict(atendente), logger),
                daemon=True,
            )
            session.thread = thread
            self._sessions[atendente_id] = session
            thread.start()

        return {"status": "iniciado", "servico": servico, "vnc": display_manager.status(atendente)}

    def finalizar(self, atendente: dict, servico: str, logger=None) -> dict[str, Any]:
        self._validar_servico(servico)
        atendente_id = atendente["id"]
        with self._lock:
            session = self._sessions.get(atendente_id)
            if not session or not self._ativa(session):
                return {"status": "sem_setup"}
            if session.servico != servico:
                return {"status": "servico_diferente", "servico": session.servico}
            self._finalizar_session(session)
            self._marcar_conectado(atendente_id, servico)
            self._sessions.pop(atendente_id, None)

        self._log(logger, f"Setup de {servico} concluido para {atendente_id}.")
        return {"status": "finalizado", "servico": servico, "vnc": display_manager.status(atendente)}

    def status(self, atendente: dict) -> dict[str, Any]:
        atendente_id = atendente["id"]
        with self._lock:
            session = self._sessions.get(atendente_id)
            if not session or not self._ativa(session):
                return {"ativo": False, "servico": None}
            restante = max(0, int(SETUP_TIMEOUT_SEGUNDOS - (time.time() - session.iniciado_em)))
            return {"ativo": True, "servico": session.servico, "timeout_restante": restante}

    def _worker(self, session: SetupSession, atendente: dict, logger) -> None:
        url = self._url_servico(atendente, session.servico)
        perfil = self._perfil_servico(session.atendente_id, session.servico)
        launch_options = self._launch_options(atendente, session.servico)
        self._log(
            logger,
            f"Setup de {session.servico} aberto. Faca login na tela ao vivo e clique em Concluir conexao.",
        )

        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(perfil),
                    **launch_options,
                    viewport={"width": 1400, "height": 900},
                )
                with self._lock:
                    session.context = context
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if not session.done.wait(SETUP_TIMEOUT_SEGUNDOS):
                    self._log(logger, f"Setup de {session.servico} expirou apos 10 minutos. Navegador fechado.")
                with contextlib.suppress(Exception):
                    context.close()
        except Exception as exc:
            self._log(logger, f"Erro no setup de {session.servico}: {exc}")
        finally:
            with self._lock:
                if self._sessions.get(session.atendente_id) is session:
                    self._sessions.pop(session.atendente_id, None)

    def _finalizar_session(self, session: SetupSession) -> None:
        session.done.set()
        if session.context:
            with contextlib.suppress(Exception):
                session.context.close()

    def _launch_options(self, atendente: dict, servico: str) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": False,
            "env": display_manager.playwright_env(atendente, servico),
            "args": ["--window-size=1400,900"],
        }
        if not display_manager.em_vps():
            options["channel"] = "chrome"
        return options

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
                    shutil.copytree(
                        origem,
                        destino,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("LOCK", "Singleton*", "*.tmp"),
                    )
                    marcador.write_text(datetime.now().isoformat(), encoding="utf-8")

    def _perfil_servico(self, atendente_id: str, servico: str):
        nome = "sessao_painel" if servico == "painel" else "sessao_whatsapp"
        caminho = PERFIS_DIR / atendente_id / nome
        caminho.mkdir(parents=True, exist_ok=True)
        return caminho

    def _marcar_conectado(self, atendente_id: str, servico: str) -> None:
        marcador = self._perfil_servico(atendente_id, servico) / ".setup_concluido"
        marcador.write_text(datetime.now().isoformat(), encoding="utf-8")

    def _url_servico(self, atendente: dict, servico: str) -> str:
        if servico == "painel":
            return f"https://app.paineldocorretor.com.br/?hash={atendente.get('painel_hash', '')}"
        return "https://web.whatsapp.com"

    def _ativa(self, session: SetupSession) -> bool:
        return bool(session.thread and session.thread.is_alive() and not session.done.is_set())

    def _validar_servico(self, servico: str) -> None:
        if servico not in SERVICOS:
            raise ValueError("Servico invalido. Use 'painel' ou 'whatsapp'.")

    def _log(self, logger, linha: str) -> None:
        if logger:
            logger(linha)


setup_conexoes = SetupConexoesManager()
