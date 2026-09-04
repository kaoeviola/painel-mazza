import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import BASE_DIR


NOVNC_WEB_DIRS = (
    Path("/usr/share/novnc"),
    Path("/usr/share/noVNC"),
    Path("/usr/local/share/novnc"),
)


@dataclass
class DisplaySession:
    atendente_id: str
    servico: str
    display: str
    novnc_port: int
    vnc_password: str
    processes: list[subprocess.Popen] = field(default_factory=list)


class DisplayManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str], DisplaySession] = {}

    def modo(self) -> str:
        return os.getenv("MODO", "local").strip().lower() or "local"

    def em_vps(self) -> bool:
        return self.modo() == "vps"

    def garantir_display(self, atendente: dict, servico: str = "painel") -> DisplaySession | None:
        if not self.em_vps():
            return None

        self._validar_servico(servico)
        atendente_id = atendente["id"]
        display = self._display_config(atendente, servico)
        novnc_port = self._novnc_port_config(atendente, servico)
        vnc_password = atendente.get("vnc_senha") or ""
        if not display or not novnc_port or not vnc_password:
            raise RuntimeError(f"Display/noVNC de {servico} nao configurado para o atendente {atendente_id}.")

        with self._lock:
            chave = (atendente_id, servico)
            session = self._sessions.get(chave)
            if session and self._session_ativa(session):
                self.aguardar_websockify(session.novnc_port, timeout=15.0)
                return session

            session = DisplaySession(
                atendente_id=atendente_id,
                servico=servico,
                display=display,
                novnc_port=novnc_port,
                vnc_password=vnc_password,
            )
            session.processes.extend(self._subir_processos(session))
            self.aguardar_websockify(session.novnc_port, timeout=15.0)
            self._sessions[chave] = session
            return session

    def aguardar_websockify(self, novnc_port: int, timeout: float = 15.0) -> None:
        prazo = time.monotonic() + timeout
        ultimo_erro: OSError | None = None
        while time.monotonic() < prazo:
            try:
                with socket.create_connection(("127.0.0.1", int(novnc_port)), timeout=1.0):
                    return
            except OSError as exc:
                ultimo_erro = exc
                time.sleep(0.3)
        raise RuntimeError(f"websockify nao ficou pronto na porta {novnc_port} em {timeout:.0f}s: {ultimo_erro}")

    def status(self, atendente: dict, servico: str | None = None) -> dict:
        if servico:
            return self._status_servico(atendente, servico)

        servicos = {
            nome: self._status_servico(atendente, nome)
            for nome in ("painel", "whatsapp")
        }
        return {
            "ativo": any(item["ativo"] for item in servicos.values()),
            "modo": self.modo(),
            "servicos": servicos,
        }

    def _status_servico(self, atendente: dict, servico: str) -> dict:
        self._validar_servico(servico)
        atendente_id = atendente["id"]
        if not self.em_vps():
            return {"ativo": False, "modo": self.modo(), "url": None, "servico": servico}

        with self._lock:
            session = self._sessions.get((atendente_id, servico))
            ativo = bool(session and self._session_ativa(session))

        return {
            "ativo": ativo,
            "modo": self.modo(),
            "servico": servico,
            "display": self._display_config(atendente, servico),
            "vnc_port": self._novnc_port_config(atendente, servico),
            "url": self.novnc_url(atendente, servico) if ativo else None,
        }

    def novnc_url(self, atendente: dict, servico: str = "painel") -> str:
        self._validar_servico(servico)
        atendente_id = atendente["id"]
        password = atendente.get("vnc_senha") or ""
        return f"/vnc/{atendente_id}/{servico}/vnc.html?resize=remote&autoconnect=true&path=/vnc/{atendente_id}/{servico}/websockify&password={password}"

    def proxy_base_url(self, atendente: dict, servico: str = "painel") -> str:
        return f"http://127.0.0.1:{self._novnc_port_config(atendente, servico)}"

    def playwright_env(self, atendente: dict, servico: str = "painel") -> dict[str, str]:
        env = dict(os.environ)
        if self.em_vps():
            display = self._display_config(atendente, servico)
            if display:
                env["DISPLAY"] = display
        return env

    def _subir_processos(self, session: DisplaySession) -> list[subprocess.Popen]:
        self._validar_binarios()
        log_dir = BASE_DIR / "logs" / "displays"
        log_dir.mkdir(parents=True, exist_ok=True)
        vnc_port = self._vnc_port(session.novnc_port)
        novnc_web = self._novnc_web_dir()
        env = dict(os.environ)
        env["DISPLAY"] = session.display

        processos: list[subprocess.Popen] = []
        processos.append(
            self._popen(
                [
                    "Xvfb",
                    session.display,
                    "-screen",
                    "0",
                    "1440x900x24",
                    "-ac",
                    "+extension",
                    "RANDR",
                ],
                log_dir / f"{session.atendente_id}_{session.servico}_xvfb.log",
            )
        )
        self._desativar_blank_x(session.display, log_dir / f"{session.atendente_id}_{session.servico}_xset.log")
        processos.append(
            self._popen(
                ["openbox"],
                log_dir / f"{session.atendente_id}_{session.servico}_openbox.log",
                env=env,
            )
        )
        processos.append(
            self._popen(
                [
                    "x11vnc",
                    "-display",
                    session.display,
                    "-rfbport",
                    str(vnc_port),
                    "-passwd",
                    session.vnc_password,
                    "-forever",
                    "-shared",
                    "-noxdamage",
                    "-repeat",
                ],
                log_dir / f"{session.atendente_id}_{session.servico}_x11vnc.log",
                env=env,
            )
        )
        processos.append(
            self._popen(
                [
                    "websockify",
                    "--web",
                    str(novnc_web),
                    str(session.novnc_port),
                    f"127.0.0.1:{vnc_port}",
                ],
                log_dir / f"{session.atendente_id}_{session.servico}_websockify.log",
            )
        )
        return processos

    def _desativar_blank_x(self, display: str, log_path: Path) -> None:
        if shutil.which("xset") is None:
            return
        env = dict(os.environ)
        env["DISPLAY"] = display
        for _ in range(20):
            proc = self._popen(["xset", "s", "off", "-dpms", "s", "noblank"], log_path, env=env)
            try:
                if proc.wait(timeout=2) == 0:
                    return
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(0.2)

    def _popen(self, args: list[str], log_path: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
        log_file = log_path.open("ab")
        return subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )

    def _session_ativa(self, session: DisplaySession) -> bool:
        return all(processo.poll() is None for processo in session.processes)

    def _validar_binarios(self) -> None:
        faltando = [cmd for cmd in ("Xvfb", "x11vnc", "websockify", "openbox") if shutil.which(cmd) is None]
        if faltando:
            raise RuntimeError(f"Binarios ausentes para noVNC/Xvfb: {', '.join(faltando)}.")

    def _novnc_web_dir(self) -> Path:
        for path in NOVNC_WEB_DIRS:
            if (path / "vnc.html").exists():
                return path
        raise RuntimeError("Diretorio web do noVNC nao encontrado.")

    def _vnc_port(self, novnc_port: int) -> int:
        return novnc_port - 1000

    def _display_config(self, atendente: dict, servico: str) -> str:
        if servico == "painel":
            return atendente.get("display_painel") or atendente.get("display") or ""
        return atendente.get("display_whatsapp") or ""

    def _novnc_port_config(self, atendente: dict, servico: str) -> int:
        if servico == "painel":
            return int(atendente.get("vnc_port_painel") or atendente.get("vnc_port") or 0)
        return int(atendente.get("vnc_port_whatsapp") or 0)

    def _validar_servico(self, servico: str) -> None:
        if servico not in {"painel", "whatsapp"}:
            raise ValueError("Servico invalido. Use 'painel' ou 'whatsapp'.")


display_manager = DisplayManager()
