import json
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = BASE_DIR / "static"
ATENDENTES_PATH = CONFIG_DIR / "atendentes.json"
MODOS_PATH = CONFIG_DIR / "modos.json"
TIMING_PATH = CONFIG_DIR / "timing.json"


@lru_cache
def carregar_atendentes() -> dict:
    with ATENDENTES_PATH.open("r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def listar_atendentes() -> list[dict]:
    return carregar_atendentes().get("atendentes", [])


def buscar_atendente(atendente_id: str) -> dict | None:
    for atendente in listar_atendentes():
        if atendente.get("id") == atendente_id and atendente.get("ativo", False):
            return atendente
    return None


@lru_cache
def carregar_modos() -> dict:
    with MODOS_PATH.open("r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def status_do_modo(modo: str) -> list[str]:
    config = config_do_modo(modo)
    status = config.get("status_marcar", [])
    if not status:
        raise ValueError(f"Modo {modo!r} nao possui status_marcar em config/modos.json.")
    return status


def config_do_modo(modo: str) -> dict:
    config = carregar_modos().get(modo)
    if not config:
        raise ValueError(f"Modo {modo!r} nao encontrado em config/modos.json.")
    return config


@lru_cache
def carregar_timings() -> dict:
    with TIMING_PATH.open("r", encoding="utf-8") as arquivo:
        return json.load(arquivo)
