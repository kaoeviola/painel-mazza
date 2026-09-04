import json
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.auth import atendente_por_token, COOKIE_NAME
from app.config import buscar_atendente
from app.conexoes import verificar_conexoes
from app.displays import display_manager
from app.execucoes import listar_execucoes
from app.planilha import PlanilhaErro, atualizar_daily_cap, ler_daily_cap
from app.runner import runner
from app.setup_conexoes import setup_conexoes

router = APIRouter(prefix="/api")
ws_router = APIRouter()


class IniciarExtracaoPayload(BaseModel):
    tipo: str = Field(pattern="^(prospectar|retrabalho)$")
    quantidade: int = Field(ge=1, le=500)


class ConectarPayload(BaseModel):
    servico: str = Field(pattern="^(painel|whatsapp)$")


class DailyCapPayload(BaseModel):
    daily_cap: Any = None


@router.get("/status")
async def status(request: Request):
    atendente = request.state.atendente
    return {
        "logado": True,
        "atendente": atendente["nome"],
        "execucao": runner.status(atendente["id"]),
    }


@router.get("/status/conexoes")
async def status_conexoes(request: Request):
    atendente = request.state.atendente
    conexoes = await asyncio.to_thread(verificar_conexoes, atendente)
    return conexoes


@router.get("/config/daily-cap")
async def obter_daily_cap(request: Request):
    planilha_id = _planilha_disparo_mazza_id()
    try:
        daily_cap = await asyncio.to_thread(ler_daily_cap, planilha_id)
    except PlanilhaErro as exc:
        raise HTTPException(status_code=502, detail=exc.motivo) from exc
    return {"daily_cap": daily_cap}


@router.post("/config/daily-cap")
async def salvar_daily_cap(payload: DailyCapPayload, request: Request):
    if request.state.atendente["id"] != "kaoe":
        raise HTTPException(status_code=403, detail="Apenas o atendente kaoe pode alterar o limite diario.")

    daily_cap = payload.daily_cap
    if type(daily_cap) is not int or not 1 <= daily_cap <= 500:
        raise HTTPException(status_code=400, detail="daily_cap deve ser um inteiro entre 1 e 500.")

    planilha_id = _planilha_disparo_mazza_id()
    try:
        await asyncio.to_thread(atualizar_daily_cap, planilha_id, daily_cap)
    except PlanilhaErro as exc:
        raise HTTPException(status_code=502, detail=exc.motivo) from exc
    return {"daily_cap": daily_cap}


@router.get("/vnc/status")
async def vnc_status(request: Request):
    atendente = request.state.atendente
    status = display_manager.status(atendente)
    setup = setup_conexoes.status(atendente)
    execucao = runner.status(atendente["id"])
    extracao_com_janelas = bool(
        execucao.get("atendente_dono") == atendente["id"]
        and execucao.get("janelas_ativas")
    )
    for servico, item in status.get("servicos", {}).items():
        janela_ativa = bool(extracao_com_janelas or (setup.get("ativo") and setup.get("servico") == servico))
        item["janela_ativa"] = janela_ativa
        if not janela_ativa:
            item["url"] = None
    status["setup"] = setup
    status["janela_ativa"] = any(item.get("janela_ativa") for item in status.get("servicos", {}).values())
    return status


@router.get("/execucoes")
async def execucoes(request: Request, limit: int = 50, atendente_id: str | None = None):
    atendente = request.state.atendente
    limite = max(1, min(int(limit or 50), 200))
    filtro_atendente = atendente_id if atendente.get("admin") else atendente["id"]
    return {"execucoes": listar_execucoes(filtro_atendente, limite)}


@router.post("/conexoes/conectar")
async def conectar_servico(payload: ConectarPayload, request: Request):
    atendente = request.state.atendente
    _bloquear_se_extracao_ativa(atendente["id"])
    try:
        resultado = setup_conexoes.iniciar(atendente, payload.servico, logger=runner._emitir_log)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **resultado}


@router.post("/conexoes/finalizar")
async def finalizar_conexao(payload: ConectarPayload, request: Request):
    atendente = request.state.atendente
    resultado = setup_conexoes.finalizar(atendente, payload.servico, logger=runner._emitir_log)
    if resultado["status"] == "sem_setup":
        raise HTTPException(status_code=409, detail="Nao ha setup de conexao aberto para este atendente.")
    if resultado["status"] == "servico_diferente":
        raise HTTPException(status_code=409, detail=f"Setup ativo e de {resultado['servico']}.")

    conexoes = await asyncio.to_thread(verificar_conexoes, atendente)
    return {"ok": True, **resultado, "conexoes": conexoes}


@router.post("/extracao/iniciar")
async def iniciar_extracao(payload: IniciarExtracaoPayload, request: Request):
    atendente = request.state.atendente
    resultado = runner.iniciar(
        atendente_id=atendente["id"],
        tipo=payload.tipo,
        quantidade=payload.quantidade,
    )
    return {"ok": True, **resultado, "execucao": runner.status(atendente["id"])}


def _bloquear_se_extracao_ativa(atendente_id: str) -> None:
    status = runner.status(atendente_id)
    if status["estado"] in {"RODANDO", "PAUSADO", "AGUARDANDO_CONFIRMACAO"} or status.get("atendente_dono"):
        raise HTTPException(status_code=409, detail="Existe uma extracao em andamento. Finalize ou cancele antes de conectar.")


def _planilha_disparo_mazza_id() -> str:
    atendente_kaoe = buscar_atendente("kaoe")
    planilha_id = atendente_kaoe.get("planilha_id", "") if atendente_kaoe else ""
    if not planilha_id or planilha_id.startswith("PLACEHOLDER"):
        raise HTTPException(status_code=503, detail="A planilha Disparo Mazza nao esta configurada.")
    return planilha_id


@router.post("/extracao/parar")
async def parar_extracao(request: Request):
    atendente = request.state.atendente
    resultado = runner.parar(atendente["id"])
    if resultado["status"] == "sem_execucao":
        raise HTTPException(status_code=409, detail="Nao ha execucao sua em andamento ou na fila.")
    return {"ok": True, **resultado, "execucao": runner.status(atendente["id"])}


@router.post("/extracao/continuar")
async def continuar_extracao(request: Request):
    atendente = request.state.atendente
    resultado = runner.continuar(atendente["id"])
    if resultado["status"] == "sem_execucao":
        raise HTTPException(status_code=409, detail="Nao ha execucao sua aguardando confirmacao.")
    if resultado["status"] == "estado_invalido":
        raise HTTPException(status_code=409, detail=f"Execucao nao esta aguardando confirmacao: {resultado['estado']}.")
    return {"ok": True, **resultado, "execucao": runner.status(atendente["id"])}


@ws_router.websocket("/ws/logs")
async def logs_ws(websocket: WebSocket):
    atendente = atendente_por_token(websocket.cookies.get(COOKIE_NAME))
    if not atendente:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = await runner.subscribe()
    try:
        while True:
            mensagem = await queue.get()
            await websocket.send_text(json.dumps(mensagem, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        runner.unsubscribe(queue)
