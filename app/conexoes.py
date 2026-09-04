from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.planilha import verificar_acesso


PERFIS_DIR = BASE_DIR / "perfis"


def verificar_conexoes(atendente: dict[str, Any]) -> dict[str, Any]:
    atendente_id = atendente["id"]
    perfil = PERFIS_DIR / atendente_id
    sessao_painel = perfil / "sessao_painel"
    sessao_whatsapp = perfil / "sessao_whatsapp"

    return {
        "painel_corretor": _status_sessao(sessao_painel),
        "whatsapp": _status_sessao(sessao_whatsapp),
        "planilha": _status_planilha(atendente.get("planilha_id", "")),
    }


def _status_sessao(caminho: Path) -> dict[str, Any]:
    if _tem_dados_sessao(caminho):
        return {"status": "conectado"}
    return {"status": "nao_configurado"}


def _tem_dados_sessao(caminho: Path) -> bool:
    if not caminho.exists() or not caminho.is_dir():
        return False

    if (caminho / ".setup_concluido").exists():
        return True

    for item in caminho.rglob("*"):
        if not item.is_file() or item.name in {".migrado_kaoe", ".setup_concluido", "LOCK"}:
            continue
        if item.name.startswith("Singleton"):
            continue
        if _parece_dado_de_sessao(item, caminho):
            return True
    return False


def _parece_dado_de_sessao(item: Path, raiz: Path) -> bool:
    nome = item.name.lower()
    if nome in {"cookies", "cookies-journal", "login data", "login data-journal"}:
        return True

    partes = {parte.lower() for parte in item.relative_to(raiz).parts}
    diretorios_sessao = {
        "local storage",
        "session storage",
        "indexeddb",
        "service worker",
        "databases",
    }
    return bool(partes & diretorios_sessao) and item.stat().st_size > 0


def _status_planilha(planilha_id: str) -> dict[str, Any]:
    ok, titulo, motivo, client_email = verificar_acesso(planilha_id)
    if ok:
        return {"status": "conectado", "titulo": titulo, "client_email": client_email}
    return {"status": motivo or "erro_api", "titulo": None, "client_email": client_email}
