"""Application configuration loaded from environment variables."""

import os
import secrets
from datetime import timedelta


def configure_app(app):
    """Apply secure defaults and environment-specific Flask settings.

    A random key is intentionally used only for local development. Production
    deployments must provide a stable FLASK_SECRET_KEY so sessions survive
    across serverless invocations.
    """
    is_vercel = os.getenv("VERCEL") == "1"
    is_production = is_vercel or os.getenv("FLASK_ENV", "").lower() == "production"
    secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()

    if is_production and not secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY wajib diatur pada environment production."
        )

    app.config.update(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SESSION_COOKIE_SECURE=is_production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(hours=1),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )

    @app.after_request
    def add_security_headers(response):
        """Add low-risk browser security headers to every application response."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        if is_production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )
        return response

    return app
