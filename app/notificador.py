import os
from pathlib import Path
from typing import Any

import httpx

from app.config import CONFIG_DIR


CONFIG_PATH = CONFIG_DIR / "whatsapp_notify.json"
GRAPH_VERSION = "v20.0"


def enviar_resumo(atendente: dict[str, Any], registro_execucao: dict[str, Any]) -> tuple[bool, str | None]:
    destino = atendente.get("notificar_whatsapp")
    if not destino:
        return False, None

    config = _carregar_config()
    phone_number_id = config.get("phone_number_id")
    token_env = config.get("token_env") or "WHATSAPP_TOKEN"
    token = os.getenv(token_env, "")
    if not phone_number_id:
        return False, "Notificação WhatsApp não enviada: phone_number_id não configurado em config/whatsapp_notify.json."
    if not token:
        return False, f"Notificação WhatsApp não enviada: variável de ambiente {token_env} não configurada."

    payload = {
        "messaging_product": "whatsapp",
        "to": destino,
        "type": "text",
        "text": {"preview_url": False, "body": _mensagem(registro_execucao)},
    }
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"

    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20.0,
        )
    except Exception as exc:
        return False, f"Falha ao enviar notificação WhatsApp: {exc}"

    if response.is_success:
        return True, "Notificação WhatsApp enviada."

    erro = _extrair_erro(response)
    if erro.get("code") == 131047:
        return False, "Notificação não entregue — envie um oi para o número da Mazza para abrir a janela"
    return False, f"Falha ao enviar notificação WhatsApp: {erro.get('message') or response.text}"


def _carregar_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    import json

    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _mensagem(registro: dict[str, Any]) -> str:
    status = registro.get("status_final")
    icone = "✅"
    if status == "erro":
        icone = "❌"
    elif status in {"concluido_com_avisos", "cancelado"}:
        icone = "⚠️"

    contadores = registro.get("contadores") or {}
    duracao = _formatar_duracao(int(registro.get("duracao_seg") or 0))
    return (
        f"{icone} Extração concluída — {registro.get('modo', '-')}\n"
        f"Processados: {contadores.get('extraidos', 0)} | Colados: {contadores.get('colados', 0)}\n"
        f"Já existiam: {contadores.get('ja_existiam', 0)} | Erros: {contadores.get('erro_fechamento', 0)}\n"
        f"Duração: {duracao}"
    )


def _formatar_duracao(segundos: int) -> str:
    minutos, resto = divmod(max(0, segundos), 60)
    if minutos:
        return f"{minutos}min {resto}s"
    return f"{resto}s"


def _extrair_erro(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"message": response.text}
    erro = data.get("error") or {}
    return erro if isinstance(erro, dict) else {"message": str(erro)}
