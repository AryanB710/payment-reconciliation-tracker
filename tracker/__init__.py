"""Flask application factory."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "recon.sqlite3")),
        SAMPLE_DATA_DIR=str(BASE_DIR / "sample_data"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5 MB per request
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])
    app.teardown_appcontext(db.close_db)

    from .routes import bp
    app.register_blueprint(bp)

    @app.template_filter("money")
    def money(value):
        if value in (None, ""):
            return "—"
        return f"{float(value):,.2f}"

    @app.template_filter("label")
    def label(value: str) -> str:
        return (value or "").replace("_", " ").title()

    return app
