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


def _build_fixture_schema(fixtures):
    """Build conservative SportsEvent JSON-LD from verified archived fields."""
    events = []
    for match in (fixtures or {}).get("matches", []):
        date_iso = (match.get("date_iso") or "").strip()
        home = (match.get("home") or "").strip()
        away = (match.get("away") or "").strip()
        if not date_iso or not home or not away:
            continue

        # date_iso comes from the iLeague archive (YYYY-MM-DD). Do not invent
        # a kick-off time when the source does not provide one.
        start_date = date_iso
        kickoff = (match.get("time") or "").strip()
        if kickoff and len(kickoff) == 5 and kickoff[2] == ":":
            start_date = f"{date_iso}T{kickoff}:00+07:00"

        status = (match.get("status") or "").strip().lower()
        event = {
            "@type": "SportsEvent",
            "name": f"{home} vs {away}",
            "startDate": start_date,
            "eventStatus": (
                "https://schema.org/EventCompleted"
                if status == "selesai"
                else "https://schema.org/EventScheduled"
            ),
            "homeTeam": {"@type": "SportsTeam", "name": home},
            "awayTeam": {"@type": "SportsTeam", "name": away},
            "sport": "Soccer",
            "description": "Pertandingan Persibo Bojonegoro di Liga Nusantara."
        }

        venue = (match.get("venue") or "").strip()
        if venue:
            event["location"] = {
                "@type": "Place",
                "name": venue,
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Bojonegoro",
                    "addressRegion": "Jawa Timur",
                    "addressCountry": "ID"
                }
            }

        score = (match.get("score") or "").strip()
        if score and status == "selesai":
            event["description"] += f" Hasil akhir: {score}."

        match_url = (match.get("match_url") or "").strip()
        if match_url.startswith("https://") or match_url.startswith("http://"):
            event["url"] = match_url
        else:
            event["url"] = "https://persibofansjogja.web.id/jadwal"

        events.append(event)

    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Jadwal dan Hasil Pertandingan Persibo Bojonegoro",
        "description": "Jadwal dan hasil pertandingan Persibo Bojonegoro di Liga Nusantara.",
        "url": "https://persibofansjogja.web.id/jadwal",
        "inLanguage": "id-ID",
        "mainEntity": {
            "@type": "ItemList",
            "name": "Jadwal dan Hasil Pertandingan Persibo Bojonegoro",
            "itemListOrder": "https://schema.org/ItemListOrderAscending",
            "numberOfItems": len(events),
            "itemListElement": [
                {"@type": "ListItem", "position": index, "item": event}
                for index, event in enumerate(events, start=1)
            ]
        }
    }


@public_bp.get("/jadwal")
def jadwal():
    # Read the stored archive once and include it in the initial HTML response.
    fixtures = _get_persibo_fixtures()
    fixture_matches = fixtures.get("matches", []) if isinstance(fixtures, dict) else []
    fixture_schema = _build_fixture_schema(fixtures)
    return render_template(
        "jadwal.html",
        fixtures=fixtures,
        fixture_matches=fixture_matches,
        fixture_schema=fixture_schema,
    )


@public_bp.get("/api/jadwal-persibo")
def api_jadwal_persibo():
    return jsonify(_get_persibo_fixtures())


@public_bp.get("/berita")
def berita():
    return render_template("berita.html", articles=get_persibo_news())


@public_bp.get("/robots.txt")
def robots():
    body = """User-agent: *\nAllow: /\n\nSitemap: https://persibofansjogja.web.id/sitemap.xml\n"""
    return Response(body, mimetype="text/plain")


@public_bp.get("/sitemap.xml")
def sitemap():
    body = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://persibofansjogja.web.id/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>
  <url><loc>https://persibofansjogja.web.id/berita</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>
  <url><loc>https://persibofansjogja.web.id/jadwal</loc><changefreq>daily</changefreq><priority>0.8</priority></url>
</urlset>
"""
    return Response(body, mimetype="application/xml")


@public_bp.get("/health")
def health():
    return {"status": "ok", "app": "Persibo Fans Jogja"}
