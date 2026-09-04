from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.auth import AuthMiddleware
from app.config import STATIC_DIR
from app.routes import api, pages, vnc


app = FastAPI(title="Painel Mazza Broker")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(pages.router)
app.include_router(api.router)
app.include_router(api.ws_router)
app.include_router(vnc.router)
