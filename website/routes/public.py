"""Public-facing pages and read-only endpoints."""
from flask import Blueprint, Response, jsonify, render_template, request
from ..services.news import get_persibo_news
from ..services.fixtures import _get_persibo_fixtures

public_bp = Blueprint("public", __name__)


@public_bp.route("/", methods=["GET", "POST"])
def home():
    # Admin login/dashboard shares the root path on its dedicated hostname.
    if (request.host or "").split(":", 1)[0].lower() == "admin.persibofansjogja.web.id":
        from .admin import admin_home
        return admin_home()
    return render_template("index.html")


@public_bp.get("/jadwal")
def jadwal():
    return render_template("jadwal.html")


@public_bp.get("/api/jadwal-persibo")
def api_jadwal_persibo():
    return jsonify(_get_persibo_fixtures())


@public_bp.get("/berita")
def berita():
    return render_template("berita.html", articles=get_persibo_news())


@public_bp.get("/robots.txt")
def robots():
    body = """User-agent: *\nAllow: /\n\nSitemap: https://www.persibofansjogja.web.id/sitemap.xml\n"""
    return Response(body, mimetype="text/plain")


@public_bp.get("/sitemap.xml")
def sitemap():
    body = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.persibofansjogja.web.id/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>
  <url><loc>https://www.persibofansjogja.web.id/berita</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>
  <url><loc>https://www.persibofansjogja.web.id/jadwal</loc><changefreq>daily</changefreq><priority>0.8</priority></url>
</urlset>
"""
    return Response(body, mimetype="application/xml")


@public_bp.get("/health")
def health():
    return {"status": "ok", "app": "Persibo Fans Jogja"}
