"""Smoke tests for public routes and local static asset references."""

import re
from pathlib import Path

from website.routes import public


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_home_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Persibo" in response.data


def test_news_page_renders_without_external_feed(client, monkeypatch):
    monkeypatch.setattr(public, "get_persibo_news", lambda: [])
    response = client.get("/berita")
    assert response.status_code == 200
    assert b"BERITA" in response.data


def test_fixtures_page_renders(client):
    response = client.get("/jadwal")
    assert response.status_code == 200
    assert b"fixture-list" in response.data


def test_robots_and_sitemap(client):
    robots = client.get("/robots.txt")
    sitemap = client.get("/sitemap.xml")
    assert robots.status_code == 200
    assert "Sitemap:" in robots.get_data(as_text=True)
    assert sitemap.status_code == 200
    assert "urlset" in sitemap.get_data(as_text=True)


def test_template_javascript_references_exist():
    templates = REPO_ROOT / "website" / "templates"
    static = REPO_ROOT / "website" / "static"
    pattern = re.compile(r"filename=['\"]js/([^'\"]+)['\"]")
    references = set()
    for template in templates.rglob("*.html"):
        references.update(pattern.findall(template.read_text(encoding="utf-8")))
    assert references, "Tidak ditemukan referensi JavaScript pada template"
    missing = sorted(name for name in references if not (static / "js" / name).is_file())
    assert not missing, f"File JavaScript yang direferensikan tidak ditemukan: {missing}"


def test_security_headers_are_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in response.headers["Permissions-Policy"]
