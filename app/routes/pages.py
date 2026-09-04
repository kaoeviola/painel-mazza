from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import autenticar, criar_cookie_sessao, remover_cookie_sessao
from app.config import STATIC_DIR


router = APIRouter()
templates = Jinja2Templates(directory=str(STATIC_DIR))


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    atendente = getattr(request.state, "atendente", None)
    if atendente:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {"erro": request.query_params.get("erro")},
    )


@router.post("/login")
async def entrar(request: Request):
    corpo = (await request.body()).decode("utf-8")
    dados = parse_qs(corpo)
    usuario = dados.get("usuario", [""])[0].strip()
    senha = dados.get("senha", [""])[0]

    atendente = autenticar(usuario, senha)
    if not atendente:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"erro": "Usuário ou senha inválidos."},
            status_code=401,
        )

    destino = request.query_params.get("next") or "/"
    response = RedirectResponse(url=destino, status_code=303)
    criar_cookie_sessao(response, atendente["id"])
    return response


@router.get("/", response_class=HTMLResponse)
async def painel(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"atendente": request.state.atendente},
    )


@router.post("/logout")
async def sair():
    response = RedirectResponse(url="/login", status_code=303)
    remover_cookie_sessao(response)
    return response
