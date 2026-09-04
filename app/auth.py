import os
from urllib.parse import urlencode

import bcrypt
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app.config import buscar_atendente


COOKIE_NAME = "mazza_session"
SESSION_MAX_AGE = 60 * 60 * 8
PUBLIC_PATHS = {"/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)


def _serializer() -> URLSafeTimedSerializer:
    secret_key = os.getenv("APP_SECRET_KEY", "troque-esta-chave-em-producao")
    return URLSafeTimedSerializer(secret_key=secret_key, salt="mazza-session")


def verificar_senha(senha: str, senha_hash: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode("utf-8"), senha_hash.encode("utf-8"))
    except ValueError:
        return False


def autenticar(atendente_id: str, senha: str) -> dict | None:
    atendente = buscar_atendente(atendente_id)
    if not atendente:
        return None

    if not verificar_senha(senha, atendente.get("senha_hash", "")):
        return None

    return atendente


def criar_cookie_sessao(response: Response, atendente_id: str) -> None:
    token = _serializer().dumps({"atendente_id": atendente_id})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def remover_cookie_sessao(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def atendente_da_requisicao(request: Request) -> dict | None:
    return atendente_por_token(request.cookies.get(COOKIE_NAME))


def atendente_por_token(token: str | None) -> dict | None:
    if not token:
        return None

    try:
        dados = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None

    return buscar_atendente(dados.get("atendente_id", ""))


def redirect_login(next_path: str = "/") -> RedirectResponse:
    query = urlencode({"next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        atendente = atendente_da_requisicao(request)
        if atendente:
            request.state.atendente = atendente
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Nao autenticado"}, status_code=401)

        return redirect_login(path)
