"""应用装配：启动期初始化知识库与 PowerContext 子进程，挂载 /api 路由与页面。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api, audit
from .api import QA_LOG_FILE, AppContext
from .config import Settings, get_settings
from .kb import KnowledgeBase
from .team import PowerContextClient, PowerContextError, PowerContextService, TeamStore

logger = logging.getLogger("saitong")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_context(settings: Settings | None = None, *, start_service: bool | None = None) -> AppContext:
    cfg = settings or get_settings()
    kb = KnowledgeBase(settings=cfg)
    service: PowerContextService | None = None
    autostart = cfg.powercontext_autostart if start_service is None else start_service
    if autostart:
        service = PowerContextService(settings=cfg, ready_timeout=cfg.powercontext_ready_timeout)
        if not service.start():
            logger.warning("PowerContext 未就绪，队伍档案相关接口会返回 503；日志见 %s",
                           service.log_path)
        else:
            logger.info("PowerContext 已就绪：%s", service.base_url)
    client = (service.client() if service is not None else PowerContextClient(settings=cfg))
    teams = TeamStore(client, settings=cfg)
    return AppContext(settings=cfg, kb=kb, teams=teams, service=service,
                      qa=api.QaLog(cfg.data_dir / QA_LOG_FILE))


def create_app(settings: Settings | None = None, *, context: AppContext | None = None,
               start_service: bool | None = None) -> FastAPI:
    cfg = settings or (context.settings if context is not None else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        ctx = context or build_context(cfg, start_service=start_service)
        app.state.ctx = ctx
        logger.info("知识库：collection=%s 块数=%s", ctx.kb.collection_name, ctx.kb.health()["chunks"])
        try:
            yield
        finally:
            ctx.teams.client.close()
            if ctx.service is not None:
                ctx.service.stop()

    app = FastAPI(title="赛题通 SaiTong", version="0.1.0", lifespan=lifespan)
    if context is not None:  # 测试与嵌入场景直接可用
        app.state.ctx = context

    app.include_router(api.router)

    @app.exception_handler(PowerContextError)
    async def _powercontext_error(request: Request, exc: PowerContextError) -> JSONResponse:
        return JSONResponse(503, content={"detail": {**exc.as_dict(), "service": "powercontext"}})

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        return api.health(request)

    if STATIC_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")

        @app.get("/stats", include_in_schema=False)
        def stats_page() -> FileResponse:
            page = STATIC_DIR / "stats.html"
            if not page.is_file():
                raise HTTPException(404, detail={"error": "stats_page_missing"})
            return FileResponse(str(page))

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        if STATIC_DIR.is_dir():
            return RedirectResponse("/ui/")
        return RedirectResponse("/docs")

    return app


app = create_app()
