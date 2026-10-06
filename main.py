# main.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
from contextlib import asynccontextmanager
from db.manager import init_db, close_db

# Убедитесь, что в api/main.py переменная называется router.
from api.main import router

BASE_DIR = Path(__file__).parent

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    close_db()

app = FastAPI(
    title="Offline Server",
    description="API для работы с меню, операторами и транзакциями",
    lifespan=lifespan
)

# ВАЖНО: Все действия с app идут строго ПОСЛЕ его создания
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(router, prefix="/api")

@app.get("/")
def index():
    tpl_path = BASE_DIR / "templates" / "default.html"
    if tpl_path.exists():
        with open(tpl_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return {"status": "server running"}
	
from fastapi.responses import Response
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)