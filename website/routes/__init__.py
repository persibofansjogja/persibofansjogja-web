"""Route blueprints for the website."""
from .public import public_bp
from .admin import admin_bp
from .fixtures_sync import sync_bp


def register_routes(app):
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(sync_bp)
