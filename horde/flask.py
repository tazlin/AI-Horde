# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import socket

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from horde.logger import logger
from horde.redis_ctrl import ger_cache_url, is_redis_up

db = SQLAlchemy()
cache = Cache()
SQLITE_MODE = os.getenv("USE_SQLITE", "0") == "1"

_app_instance = None


def get_app():
    """Return the app instance for background threads that need app_context()."""
    if _app_instance is None:
        raise RuntimeError("App not created yet — call create_app() first")
    return _app_instance


def create_app(config=None):
    global _app_instance

    app = Flask(__name__)
    app.config.SWAGGER_UI_DOC_EXPANSION = "list"
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    if config:
        app.config.update(config)

    if "SQLALCHEMY_DATABASE_URI" not in app.config:
        if SQLITE_MODE:
            logger.warning("Using SQLite for database")
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
        else:
            app.config["SQLALCHEMY_DATABASE_URI"] = (
                f"postgresql://{os.getenv('POSTGRES_USER', 'postgres')}:" f"{os.getenv('POSTGRES_PASS')}@{os.getenv('POSTGRES_URL')}"
            )
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
                "pool_size": 50,
                "max_overflow": -1,
            }
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    db.init_app(app)

    if not SQLITE_MODE and not app.config.get("TESTING"):
        with app.app_context():
            logger.warning(f"pool size = {db.engine.pool.size()}")
    logger.init_ok("Horde Database", status="Started")

    logger.init("Flask Cache", status="Connecting")
    if is_redis_up() and not app.config.get("TESTING"):
        try:
            app.config.update(
                {
                    "CACHE_TYPE": "RedisCache",
                    "CACHE_REDIS_URL": ger_cache_url(),
                    "CACHE_DEFAULT_TIMEOUT": 300,
                }
            )
            cache.init_app(app)
            logger.init_ok("Flask Cache", status="Connected")
        except Exception as e:
            logger.error(f"Flask Cache Failed: {e}")
            app.config.update({"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})
            cache.init_app(app)
            logger.init_warn("Flask Cache", status="SimpleCache")
    else:
        app.config.update({"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})
        cache.init_app(app)
        logger.init_warn("Flask Cache", status="SimpleCache")

    from horde.limiter import init_limiter

    init_limiter(app)

    if not app.config.get("TESTING"):
        from horde.horde_redis import horde_redis

        horde_redis.connect()

    if not app.config.get("TESTING"):
        from horde.countermeasures import init_countermeasures

        init_countermeasures()

    from horde.apis import apiv2
    from horde.routes import routes_bp

    app.register_blueprint(apiv2)
    app.register_blueprint(routes_bp)

    _register_oauth(app)

    from horde.argparser import args
    from horde.consts import HORDE_VERSION

    @app.after_request
    def after_request(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE, PATCH"
        response.headers["Access-Control-Allow-Headers"] = (
            "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, apikey, "
            "Client-Agent, X-Fields, X-Forwarded-For, Proxied-For, Proxy-Authorization"
        )
        response.headers["Horde-Node"] = f"{socket.gethostname()}:{args.port}:{HORDE_VERSION}"
        return response

    if not app.config.get("TESTING"):
        from horde.classes import init_db

        init_db(app)

    if not app.config.get("TESTING"):
        from horde.database import start_background_threads

        start_background_threads()

    _app_instance = app
    return app


def _register_oauth(app):
    from flask_dance.contrib.discord import make_discord_blueprint
    from flask_dance.contrib.github import make_github_blueprint
    from flask_dance.contrib.google import make_google_blueprint

    app.secret_key = os.getenv("secret_key")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

    google_blueprint = make_google_blueprint(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GLOOGLE_CLIENT_SECRET"),
        reprompt_consent=True,
        redirect_url="/register",
        scope=["email"],
    )
    app.register_blueprint(google_blueprint, url_prefix="/google")

    discord_blueprint = make_discord_blueprint(
        client_id=os.getenv("DISCORD_CLIENT_ID"),
        client_secret=os.getenv("DISCORD_CLIENT_SECRET"),
        scope=["identify"],
        redirect_url="/finish_dance",
    )
    app.register_blueprint(discord_blueprint, url_prefix="/discord")

    github_blueprint = make_github_blueprint(
        client_id=os.getenv("GITHUB_CLIENT_ID"),
        client_secret=os.getenv("GITHUB_CLIENT_SECRET"),
        scope=["identify"],
        redirect_url="/finish_dance",
    )
    app.register_blueprint(github_blueprint, url_prefix="/github")
