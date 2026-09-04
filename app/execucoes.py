import json
import threading
from pathlib import Path
from typing import Any

from app.config import BASE_DIR


EXECUCOES_PATH = BASE_DIR / "logs" / "execucoes.jsonl"
_lock = threading.RLock()


def registrar_execucao(registro: dict[str, Any]) -> None:
    EXECUCOES_PATH.parent.mkdir(parents=True, exist_ok=True)
    linha = json.dumps(registro, ensure_ascii=False)
    with _lock:
        with EXECUCOES_PATH.open("a", encoding="utf-8") as arquivo:
            arquivo.write(linha + "\n")


def listar_execucoes(atendente_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if not EXECUCOES_PATH.exists():
        return []

    registros: list[dict[str, Any]] = []
    with _lock:
        linhas = EXECUCOES_PATH.read_text(encoding="utf-8").splitlines()

    for linha in reversed(linhas):
        if not linha.strip():
            continue
        try:
            registro = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if atendente_id and registro.get("atendente") != atendente_id:
            continue
        registros.append(registro)
        if len(registros) >= limit:
            break
    return registros
