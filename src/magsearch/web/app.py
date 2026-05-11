from fastapi import FastAPI

from magsearch.web.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Magazine Search")
    app.include_router(router)
    return app


app = create_app()
