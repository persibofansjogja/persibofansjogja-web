"""Admin-only routes, guarded by host, session and CSRF checks."""
import hmac
import os
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from ..services.supabase_crud import handle_test_crud
from ..utils.security import csrf_token, valid_csrf_token

admin_bp = Blueprint("admin", __name__)
ADMIN_HOST = "admin.persibofansjogja.web.id"


def _is_admin_host():
    return (request.host or "").split(":", 1)[0].lower() == ADMIN_HOST


def admin_home():
    """Render admin login or CRUD dashboard on the admin hostname."""
    if request.method == "POST":
        if not valid_csrf_token():
            return render_template("admin_login.html", error="Sesi formulir tidak valid. Muat ulang halaman lalu coba kembali.", csrf_token=csrf_token()), 400
        username, password, secret = os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", ""), os.getenv("FLASK_SECRET_KEY", "")
        supplied_user, supplied_password = request.form.get("username", ""), request.form.get("password", "")
        if not username or not password or not secret:
            return render_template("admin_login.html", error="Konfigurasi login belum lengkap. Hubungi administrator.", csrf_token=csrf_token()), 503
        if hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password):
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            csrf_token()
            return redirect("/")
        return render_template("admin_login.html", error="Username atau password salah.", csrf_token=csrf_token()), 401
    if session.get("admin_authenticated"):
        return render_template("test_crud.html", csrf_token=csrf_token())
    return render_template("admin_login.html", csrf_token=csrf_token())


@admin_bp.post("/logout")
def admin_logout():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    session.clear()
    return redirect("/")


@admin_bp.get("/test-crud")
def test_crud_page():
    if _is_admin_host():
        return redirect("/", code=301)
    return jsonify(error="Not found"), 404


@admin_bp.route("/test-crud/api", methods=["GET", "POST", "PATCH", "DELETE"])
def test_crud_api():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not session.get("admin_authenticated"):
        return jsonify(error="Sesi admin berakhir. Silakan login kembali."), 401
    if request.method in {"POST", "PATCH", "DELETE"} and not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    return handle_test_crud()
