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


def test_fixtures_page_renders_server_side_and_seo(client, monkeypatch):
    monkeypatch.setattr(
        public,
        "_get_persibo_fixtures",
        lambda: {
            "matches": [{
                "date": "Jumat, 27 November 2026",
                "date_iso": "2026-11-27",
                "time": "15:00",
                "home": "PERSIBO BOJONEGORO",
                "away": "GRESIK UNITED FC",
                "raw": "PERSIBO BOJONEGORO vs GRESIK UNITED FC",
                "venue": "Stadion Letjen H. Soedirman",
                "score": "2 - 1",
                "status": "Selesai",
                "match_url": "https://ileague.id/result/detail/example",
            }],
            "updated": "2026-11-27T10:00:00+07:00",
            "error": None,
        },
    )
    response = client.get("/jadwal")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "PERSIBO BOJONEGORO vs GRESIK UNITED FC" in html
    assert 'rel="canonical"' in html
    assert 'name="robots" content="index, follow' in html
    assert 'property="og:title"' in html
    assert 'application/ld+json' in html
    assert "SportsEvent" in html
    assert "loading-spinner" not in html


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
