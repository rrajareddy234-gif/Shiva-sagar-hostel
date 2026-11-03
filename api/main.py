"""Application entry point.

Run locally with:
    uvicorn api.main:app --host 0.0.0.0 --port 12000
"""

from __future__ import annotations

import logging
import os

from api.webhook import create_app
from core.config import Settings, load_dotenv

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _app():
    # Direct runs read .env; Docker already injects it, and this will not override.
    load_dotenv()
    return create_app(Settings.from_env())


app = _app()
