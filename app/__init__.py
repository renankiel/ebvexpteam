import os
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv

from app.blueprints.auth.routes import init_auth
from app.blueprints.board.routes import board_bp
from app.blueprints.actions.routes import actions_bp

load_dotenv()


def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static"
    )

    app.permanent_session_lifetime = timedelta(days=7)

    # Inicializa OAuth + registra o auth_bp
    init_auth(app)

    # Registra os demais blueprints mantendo 100% as mesmas URLs originais
    app.register_blueprint(board_bp)
    app.register_blueprint(actions_bp)

    return app
