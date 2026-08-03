import os
import warnings
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import src.endpoints.examples
import src.endpoints.functions
import src.endpoints.health
import src.endpoints.recipes
from src.utils import RootLogger

load_dotenv(verbose=True)

logger = RootLogger.get_root_logger()

Main_Server_Host_Public = os.getenv("MAIN_SERVER_HOST_PUBLIC", "localhost")
Main_Server_Host_Public = f"http://{Main_Server_Host_Public}"

current_Version = "1.0.0"

swagger_Ui_Description = f"""
# Welcome to the Knowledge Store

This API allows you to apply feature engineering, analytical, and forecasting functions directly to data files.

#### In this page you can find all the endpoints exposed, and try them directly.

**Build Date:** {datetime.today().strftime('%Y/%m/%d %H:%M:%S')}
"""

app = FastAPI(
    title="Knowledge Store",
    docs_url="/docs",
    redoc_url=None,
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    description=swagger_Ui_Description,
    openapi_tags=[
        {
            "name": "Functions",
            "description": "List available functions (feature, analytical, forecasting) and apply them to an uploaded data file.",
        },
        {
            "name": "Recipes",
            "description": "List available recipes and apply them to an uploaded data file.",
        },
        {
            "name": "Examples",
            "description": "Retrieve example datasets that can be used with the functions and recipes.",
        },
    ],
    summary="Stateless Feature Engineering API",
    version=current_Version,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(
    src.endpoints.health.router,
    tags=["Health"],
    include_in_schema=False,
)

app.include_router(
    src.endpoints.functions.router,
    prefix="/functions",
    tags=["Functions"],
)

app.include_router(
    src.endpoints.recipes.router,
    prefix="/recipes",
    tags=["Recipes"],
)

app.include_router(
    src.endpoints.examples.router,
    prefix="/examples",
    tags=["Examples"],
)

# --- AI-Effect Control Interface ---
try:
    from common import create_control_router, knowledge_store_handlers

    app.include_router(
        create_control_router(knowledge_store_handlers),
        prefix="/control",
    )
except ImportError as e:
    import logging as _logging
    _logging.warning(f"AI-Effect control interface not available: {e}")


if __name__ == "__main__":
    main_server_reload_str = os.getenv("MAIN_SERVER_RELOAD", "False")
    Main_Server_Reload = "true" in main_server_reload_str.lower()

    scriptName = os.path.basename(__file__).replace(".py", "")

    Main_Server_Port = int(os.getenv("MAIN_SERVER_PORT", "8000"))
    Main_Server_Host = os.getenv("MAIN_SERVER_HOST", "0.0.0.0")

    logger.info(
        f"Starting {scriptName} on {Main_Server_Host}:{Main_Server_Port} with reload={Main_Server_Reload}"
    )
    if Main_Server_Reload:
        warnings.warn(
            "MAIN_SERVER_RELOAD env var set to 'True'. Server will restart on code change."
        )

    uvicorn.run(
        "main:app",
        host=Main_Server_Host,
        port=Main_Server_Port,
        reload=Main_Server_Reload,
    )
