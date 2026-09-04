import asyncio

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response

from app.auth import COOKIE_NAME, atendente_por_token
from app.config import buscar_atendente
from app.displays import display_manager


router = APIRouter()


@router.get("/vnc/{atendente_id}")
async def vnc_root(atendente_id: str, request: Request):
    atendente = _atendente_http(request, atendente_id)
    return RedirectResponse(url=display_manager.novnc_url(atendente, "painel"), status_code=303)


@router.get("/vnc/{atendente_id}/{servico}")
async def vnc_service_root(atendente_id: str, servico: str, request: Request):
    atendente = _atendente_http(request, atendente_id)
    if servico == "vnc.html":
        return RedirectResponse(url=display_manager.novnc_url(atendente, "painel"), status_code=303)
    _validar_servico(servico)
    return RedirectResponse(url=display_manager.novnc_url(atendente, servico), status_code=303)


@router.get("/vnc/{atendente_id}/vnc.html")
async def vnc_legacy_http(atendente_id: str, request: Request):
    atendente = _atendente_http(request, atendente_id)
    return RedirectResponse(url=display_manager.novnc_url(atendente, "painel"), status_code=303)


@router.get("/vnc/{atendente_id}/{servico}/{path:path}")
async def vnc_proxy_http(atendente_id: str, servico: str, path: str, request: Request):
    atendente = _atendente_http(request, atendente_id)
    _validar_servico(servico)
    status = display_manager.status(atendente, servico)
    if not status["ativo"]:
        raise HTTPException(status_code=404, detail="Display VNC inativo.")

    query = str(request.url.query)
    target_url = f"{display_manager.proxy_base_url(atendente, servico)}/{path}"
    if query:
        target_url = f"{target_url}?{query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "connection", "content-length", "accept-encoding"}
    }
    try:
        upstream = await _request_com_retry(request.method, target_url, headers=headers)
    except httpx.ConnectError:
        return _tela_iniciando()

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _request_com_retry(method: str, target_url: str, headers: dict[str, str]) -> httpx.Response:
    prazo = asyncio.get_running_loop().time() + 5.0
    ultimo_erro: httpx.ConnectError | None = None
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        while True:
            try:
                return await client.request(method, target_url, headers=headers)
            except httpx.ConnectError as exc:
                ultimo_erro = exc
                if asyncio.get_running_loop().time() >= prazo:
                    raise ultimo_erro
                await asyncio.sleep(0.4)


def _tela_iniciando() -> Response:
    html = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="2">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tela iniciando</title>
  <style>
    body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#c9d1d9;font:14px Inter,system-ui,sans-serif}
    main{display:grid;gap:10px;justify-items:center}
    span{width:34px;height:34px;border:3px solid rgba(255,255,255,.12);border-top-color:#10b981;border-radius:999px;animation:spin 1s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
  </style>
</head>
<body><main><span></span><strong>Tela iniciando, aguarde...</strong></main></body>
</html>"""
    return Response(content=html, status_code=503, media_type="text/html")


@router.websocket("/vnc/{atendente_id}/{servico}/websockify")
async def vnc_proxy_ws(atendente_id: str, servico: str, websocket: WebSocket):
    atendente = atendente_por_token(websocket.cookies.get(COOKIE_NAME))
    if not atendente or atendente.get("id") != atendente_id or servico not in {"painel", "whatsapp"}:
        await websocket.close(code=1008)
        return

    status = display_manager.status(atendente, servico)
    if not status["ativo"]:
        await websocket.close(code=1011)
        return

    query = websocket.url.query
    target = f"ws://127.0.0.1:{status['vnc_port']}/websockify"
    if query:
        target = f"{target}?{query}"

    await websocket.accept()
    try:
        async with websockets.connect(target, max_size=None) as upstream:
            await _pipe_websockets(websocket, upstream)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close(code=1011)


async def _pipe_websockets(client: WebSocket, upstream) -> None:
    import asyncio

    async def client_to_upstream():
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close()
                return
            if "bytes" in message and message["bytes"] is not None:
                await upstream.send(message["bytes"])
            elif "text" in message and message["text"] is not None:
                await upstream.send(message["text"])

    async def upstream_to_client():
        async for message in upstream:
            if isinstance(message, bytes):
                await client.send_bytes(message)
            else:
                await client.send_text(message)

    done, pending = await asyncio.wait(
        {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def _atendente_http(request: Request, atendente_id: str) -> dict:
    atendente = getattr(request.state, "atendente", None)
    if not atendente:
        raise HTTPException(status_code=401, detail="Nao autenticado.")
    if atendente.get("id") != atendente_id:
        raise HTTPException(status_code=403, detail="Acesso negado ao display de outro atendente.")
    if not buscar_atendente(atendente_id):
        raise HTTPException(status_code=404, detail="Atendente nao encontrado.")
    return atendente


def _validar_servico(servico: str) -> None:
    if servico not in {"painel", "whatsapp"}:
        raise HTTPException(status_code=404, detail="Servico VNC nao encontrado.")
