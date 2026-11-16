import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
from flask import Flask, Response, render_template

app = Flask(__name__)

# Google News RSS: artikel yang relevan dengan Persibo Bojonegoro.
RSS_FEEDS = [
    {
        "name": "Google News",
        "url": "https://news.google.com/rss/search?q=Persibo&hl=id&gl=ID&ceid=ID:id",
    },
    {
        "name": "Google News",
        "url": "https://news.google.com/rss/search?q=%22Laskar+Angling+Dharma%22&hl=id&gl=ID&ceid=ID:id",
    }
]


def _entry_date(entry):
    """Format tanggal RSS ke format Indonesia; kosong jika tanggal tidak tersedia."""
    raw_date = entry.get("published") or entry.get("updated")
    if not raw_date:
        return "Tanggal tidak tersedia"
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        months = [
            "Januari", "Februari", "Maret", "April", "Mei", "Juni",
            "Juli", "Agustus", "September", "Oktober", "November", "Desember"
        ]
        return f"{dt.day} {months[dt.month - 1]} {dt.year}"
    except (TypeError, ValueError, OverflowError):
        return raw_date


def _clean_summary(value):
    """Buang markup HTML sederhana dari ringkasan RSS."""
    from html import unescape
    from re import sub
    text = sub(r"<[^>]+>", " ", value or "")
    return " ".join(unescape(text).split())



def _safe_image_url(url):
    """Terima URL gambar HTTP(S) saja."""
    from urllib.parse import urlparse
    if not url:
        return None
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate
    return None


def _entry_image(entry):
    """Cari gambar dari media:thumbnail, media:content, enclosure, atau HTML ringkasan."""
    from html.parser import HTMLParser

    for field in ("media_thumbnail", "media_content"):
        media_items = entry.get(field, []) or []
        for media in media_items:
            image_url = _safe_image_url(media.get("url"))
            if image_url:
                return image_url

    for enclosure in entry.get("enclosures", []) or []:
        mime = enclosure.get("type", "")
        if mime.startswith("image/") or not mime:
            image_url = _safe_image_url(enclosure.get("href") or enclosure.get("url"))
            if image_url:
                return image_url

    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure":
            mime = link.get("type", "")
            if mime.startswith("image/") or not mime:
                image_url = _safe_image_url(link.get("href"))
                if image_url:
                    return image_url

    class ImageFinder(HTMLParser):
        def __init__(self):
            super().__init__()
            self.image = None

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "img" and not self.image:
                attrs_dict = dict(attrs)
                self.image = _safe_image_url(attrs_dict.get("src") or attrs_dict.get("data-src"))

    finder = ImageFinder()
    try:
        finder.feed(entry.get("summary", "") or "")
        if finder.image:
            return finder.image
    except Exception:
        pass
    return None


def get_persibo_news(limit=40):
    """Ambil berita Persibo dari RSS; satu feed gagal tidak menggagalkan halaman."""
    items = []
    seen = set()

    for feed_config in RSS_FEEDS:
        try:
            parsed = feedparser.parse(
                feed_config["url"],
                request_headers={"User-Agent": "PersiboFansJogja/1.0 (+https://www.persibofansjogja.web.id)"}
            )
            for entry in parsed.entries:
                link = entry.get("link", "").strip()
                title = _clean_summary(entry.get("title", ""))
                if not link or not title or link in seen:
                    continue
                seen.add(link)

                source = entry.get("source", {}).get("title") or feed_config["name"]
                items.append({
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": _entry_date(entry),
                    "summary": _clean_summary(entry.get("summary", "")),
                    "image": _entry_image(entry),
                })
        except Exception:
            # RSS bisa sesaat tidak tersedia; tampilkan status kosong di halaman.
            continue

    return items[:limit]


@app.get("/")
def home():
    """Halaman utama Persibo Fans Jogja."""
    return render_template("index.html")


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
</urlset>
"""
    return Response(body, mimetype="application/xml")


@app.get("/health")
def health():
    """Endpoint sederhana untuk pemeriksaan status aplikasi."""
    return {"status": "ok", "app": "Persibo Fans Jogja"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1"
    )
