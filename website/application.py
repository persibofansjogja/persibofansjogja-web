"""Flask application factory and WSGI application instance."""
import os
from flask import Flask
from .config import configure_app
from .routes import register_routes


def create_app():
    app = configure_app(Flask(__name__))
    register_routes(app)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
