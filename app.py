import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urljoin, urlparse
import ipaddress
import socket
import time

import requests
from bs4 import BeautifulSoup

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
        mime = link.get("type", "")
        if link.get("rel") == "enclosure" or mime.startswith("image/"):
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
        finder.feed(entry.get("summary", "") or entry.get("description", "") or "")
        if finder.image:
            return finder.image
    except Exception:
        pass
    return None



# Cache sederhana agar halaman penerbit tidak diambil berulang kali setiap request.
_THUMBNAIL_CACHE = {}
_THUMBNAIL_CACHE_TTL = 6 * 60 * 60
_MAX_ARTICLE_LOOKUPS = 12


def _is_public_http_url(url):
    """Batasi URL ke HTTP(S) dan host publik; blokir localhost/IP privat."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in ("localhost",) or host.endswith((".localhost", ".local")):
            return False
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        for result in addresses:
            ip = ipaddress.ip_address(result[4][0])
            if not ip.is_global:
                return False
        return bool(addresses)
    except (ValueError, OSError):
        return False


def _extract_article_thumbnail(article_url):
    """Ambil og:image/twitter:image dari halaman artikel dengan redirect yang dibatasi."""
    cached = _THUMBNAIL_CACHE.get(article_url)
    now = time.time()
    if cached and now - cached[0] < _THUMBNAIL_CACHE_TTL:
        return cached[1]

    current_url = article_url
    image_url = None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersiboFansJogjaNews/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        # Periksa setiap tujuan redirect sebelum mengirim request berikutnya.
        for _ in range(4):
            if not _is_public_http_url(current_url):
                break
            response = requests.get(
                current_url,
                headers=headers,
                timeout=(3, 4),
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    break
                current_url = urljoin(current_url, location)
                continue

            content_type = response.headers.get("Content-Type", "").lower()
            if response.status_code != 200 or "html" not in content_type:
                response.close()
                break

            # Batasi ukuran HTML yang dibaca.
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 1_000_000:
                    break
                chunks.append(chunk)
            response.close()
            html = b"".join(chunks)
            soup = BeautifulSoup(html, "html.parser")

            for attrs in (
                {"property": "og:image"},
                {"property": "og:image:secure_url"},
                {"name": "twitter:image"},
                {"name": "twitter:image:src"},
            ):
                tag = soup.find("meta", attrs=attrs)
                if tag and tag.get("content"):
                    candidate = urljoin(current_url, tag["content"].strip())
                    if _safe_image_url(candidate):
                        image_url = candidate
                        break

            # Fallback: gambar pertama di area artikel utama.
            if not image_url:
                main = soup.find("article") or soup.find("main")
                if main:
                    img = main.find("img")
                    if img:
                        candidate = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                        if candidate:
                            candidate = urljoin(current_url, candidate.strip())
                            if _safe_image_url(candidate):
                                image_url = candidate
            break

    except requests.RequestException:
        image_url = None
    except Exception:
        image_url = None

    _THUMBNAIL_CACHE[article_url] = (now, image_url)
    # Jaga cache agar tidak terus membesar pada proses server yang panjang.
    if len(_THUMBNAIL_CACHE) > 500:
        oldest = sorted(_THUMBNAIL_CACHE.items(), key=lambda item: item[1][0])[:100]
        for key, _ in oldest:
            _THUMBNAIL_CACHE.pop(key, None)

    return image_url



# Cache berita singkat seperti mekanisme pada website referensi.
_NEWS_CACHE = []
_NEWS_CACHE_AT = 0.0
_NEWS_CACHE_TTL = 30
_NEWS_CACHE_LOCK = Lock()
_MAX_OG_LOOKUPS = 10


def _fetch_one_feed(feed_config):
    """Ambil dan parse satu RSS; dipanggil paralel oleh ThreadPoolExecutor."""
    try:
        response = requests.get(
            feed_config["url"],
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersiboFansJogja/1.0)"},
            timeout=(3, 6),
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        return feed_config, parsed.entries
    except requests.RequestException:
        return feed_config, []
    except Exception:
        return feed_config, []


def _og_image_for_article(article):
    """Fallback metadata gambar artikel jika RSS tidak membawa thumbnail."""
    return _extract_article_thumbnail(article["link"])


def get_persibo_news(limit=40):
    """Ambil berita dari RSS secara paralel, ekstrak thumbnail, dan cache 30 detik."""
    global _NEWS_CACHE, _NEWS_CACHE_AT

    now = time.time()
    with _NEWS_CACHE_LOCK:
        if _NEWS_CACHE and now - _NEWS_CACHE_AT < _NEWS_CACHE_TTL:
            return _NEWS_CACHE[:limit]

    collected = []
    seen = set()

    # Kedua feed Google News diproses bersamaan.
    with ThreadPoolExecutor(max_workers=min(4, len(RSS_FEEDS) or 1)) as executor:
        futures = [executor.submit(_fetch_one_feed, cfg) for cfg in RSS_FEEDS]
        for future in as_completed(futures):
            feed_config, entries = future.result()
            for entry in entries:
                link = (entry.get("link") or "").strip()
                title = _clean_summary(entry.get("title", ""))
                if not link or not title or link in seen:
                    continue
                seen.add(link)

                source = entry.get("source", {}).get("title") or feed_config["name"]
                collected.append({
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": _entry_date(entry),
                    "summary": _clean_summary(entry.get("summary", "")),
                    "image": _entry_image(entry),
                    "_published_parsed": entry.get("published_parsed") or entry.get("updated_parsed"),
                })

    # Urutkan terbaru lebih dulu bila tanggal RSS tersedia.
    collected.sort(
        key=lambda item: item.get("_published_parsed") or (0, 0, 0, 0, 0, 0),
        reverse=True,
    )
    selected = collected[:limit]

    # RSS kadang tidak menyertakan gambar. Coba metadata artikel untuk beberapa
    # berita teratas yang belum memiliki gambar, dengan jumlah request dibatasi.
    missing = [item for item in selected if not item.get("image")][:_MAX_OG_LOOKUPS]
    if missing:
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(_og_image_for_article, item): item
                for item in missing
            }
            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    item["image"] = future.result()
                except Exception:
                    item["image"] = None

    # Hilangkan field internal sebelum dikirim ke template.
    for item in selected:
        item.pop("_published_parsed", None)

    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE = selected
        _NEWS_CACHE_AT = time.time()

    return selected


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
