"""Flask routes and application wiring for Persibo Fans Jogja."""

import os
import re
import secrets
import hmac

import requests
from flask import (
    Flask, Response, render_template, request, jsonify, redirect, session, url_for
)

from .config import configure_app
from .services.news import get_persibo_news
from .services.fixtures import _get_persibo_fixtures

app = configure_app(Flask(__name__))


ADMIN_HOST = "admin.persibofansjogja.web.id"


def _is_admin_host():
    """Batasi halaman dan API pengujian ke subdomain admin."""
    return (request.host or "").split(":", 1)[0].lower() == ADMIN_HOST


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf_token():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


@app.route("/", methods=["GET", "POST"])
def home():
    """Halaman login admin; dashboard CRUD hanya tampil setelah login."""
    if not _is_admin_host():
        return render_template("index.html")

    if request.method == "POST":
        if not _valid_csrf_token():
            return render_template("admin_login.html", error="Sesi formulir tidak valid. Muat ulang halaman lalu coba kembali."), 400
        username = os.getenv("ADMIN_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")
        secret = os.getenv("FLASK_SECRET_KEY", "")
        supplied_user = request.form.get("username", "")
        supplied_password = request.form.get("password", "")
        if not username or not password or not secret:
            return render_template(
                "admin_login.html",
                error="Konfigurasi login belum lengkap. Hubungi administrator.",
                csrf_token=_csrf_token(),
            ), 503
        if (hmac.compare_digest(supplied_user, username)
                and hmac.compare_digest(supplied_password, password)):
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            _csrf_token()
            return redirect(url_for("home"))
        return render_template(
            "admin_login.html", error="Username atau password salah.", csrf_token=_csrf_token()
        ), 401

    if session.get("admin_authenticated"):
        return render_template("test_crud.html", csrf_token=_csrf_token())
    return render_template("admin_login.html", csrf_token=_csrf_token())


@app.post("/logout")
def admin_logout():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not _valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    session.clear()
    return redirect(url_for("home"))



@app.get("/jadwal")
def jadwal():
    """Jadwal dan hasil pertandingan Persibo pada Liga Nusantara 2026/27."""
    return render_template("jadwal.html")

@app.get("/api/jadwal-persibo")
def api_jadwal_persibo():
    return jsonify(_get_persibo_fixtures())

@app.get("/berita")
def berita():
    """Halaman berita Persibo dari RSS Feed."""
    articles = get_persibo_news()
    return render_template("berita.html", articles=articles)


@app.get("/robots.txt")
def robots():
    """Instruksi crawler mesin pencari."""
    body = """User-agent: *
Allow: /

Sitemap: https://www.persibofansjogja.web.id/sitemap.xml
"""
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Sitemap XML untuk halaman publik."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.persibofansjogja.web.id/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://www.persibofansjogja.web.id/berita</loc>
    <changefreq>hourly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://www.persibofansjogja.web.id/jadwal</loc>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
"""
    return Response(body, mimetype="application/xml")


@app.get("/health")
def health():
    """Endpoint sederhana untuk pemeriksaan status aplikasi."""
    return {"status": "ok", "app": "Persibo Fans Jogja"}


@app.get("/test-crud")
def test_crud_page():
    """Arahkan URL lama ke root subdomain admin; jangan tampilkan di domain publik."""
    if _is_admin_host():
        return redirect("/", code=301)
    return jsonify(error="Not found"), 404


@app.route("/test-crud/api", methods=["GET", "POST", "PATCH", "DELETE"])
def test_crud_api():
    """API CRUD sementara yang hanya tersedia pada subdomain admin."""
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not session.get("admin_authenticated"):
        return jsonify(error="Sesi admin berakhir. Silakan login kembali."), 401
    if request.method in {"POST", "PATCH", "DELETE"} and not _valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400

    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY") or "").strip()
    table = os.getenv("SUPABASE_TEST_TABLE", "").strip()
    id_column = os.getenv("SUPABASE_TEST_ID_COLUMN", "id").strip()
    missing = [name for name, value in (
        ("SUPABASE_URL", url),
        ("SUPABASE_KEY atau SUPABASE_PUBLISHABLE_KEY", key),
        ("SUPABASE_TEST_TABLE", table),
    ) if not value]
    if missing:
        return jsonify(error="Konfigurasi Supabase belum lengkap.", missing=missing), 503

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", id_column):
        return jsonify(error="Nama tabel atau kolom ID tidak valid."), 400
    if not url.startswith("https://") or not url.endswith(".supabase.co"):
        return jsonify(error="SUPABASE_URL tidak valid."), 400

    endpoint = f"{url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        if request.method == "GET":
            response = requests.get(
                endpoint, headers=headers,
                params={"select": "*", "limit": "100", "order": f"{id_column}.desc"},
                timeout=12,
            )
        elif request.method == "POST":
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or not payload:
                return jsonify(error="Isi data harus berupa objek JSON."), 400
            response = requests.post(
                endpoint, headers={**headers, "Prefer": "return=representation"},
                json=payload, timeout=12,
            )
        else:
            payload = request.get_json(silent=True) or {}
            record_id = payload.get("id")
            if record_id is None or str(record_id).strip() == "":
                return jsonify(error="ID data wajib diisi."), 400
            params = {id_column: f"eq.{record_id}"}
            if request.method == "PATCH":
                changes = payload.get("data")
                if not isinstance(changes, dict) or not changes:
                    return jsonify(error="Data perubahan harus berupa objek JSON."), 400
                response = requests.patch(
                    endpoint, headers={**headers, "Prefer": "return=representation"},
                    params=params, json=changes, timeout=12,
                )
            else:
                response = requests.delete(
                    endpoint, headers={**headers, "Prefer": "return=representation"},
                    params=params, timeout=12,
                )

        if not response.ok:
            return jsonify(
                error="Supabase menolak permintaan. Periksa nama tabel, kolom ID, struktur data, dan kebijakan RLS.",
                supabase_status=response.status_code,
                details=response.text[:1200],
            ), 502

        if request.method == "GET":
            return jsonify(records=response.json())
        try:
            return jsonify(records=response.json())
        except ValueError:
            return jsonify(records=[])
    except requests.RequestException:
        return jsonify(error="Tidak dapat menghubungi Supabase."), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1"
    )


