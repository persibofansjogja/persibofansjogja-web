import os
import re
from datetime import datetime, timezone, timedelta
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
from flask import Flask, Response, render_template, request, jsonify, redirect, session, url_for

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "")
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(hours=8),
)

# Berita diambil langsung dari RSS penerbit dan arsip resmi; Google News tidak digunakan.
RSS_FEEDS = [
    {
        "name": "blokBojonegoro.com",
        "url": "https://blokbojonegoro.com/rss.xml",
    },
    {
        "name": "ANTARA Jawa Timur",
        "url": "https://jatim.antaranews.com/rss/terkini.xml",
    },
    {
        "name": "ANTARA Kabar Jatim",
        "url": "https://jatim.antaranews.com/rss/kabar-jatim.xml",
    },
    {
        "name": "ANTARA Sepak Bola Indonesia",
        "url": "https://www.antaranews.com/rss/sepakbola-liga-indonesia.xml",
    },
    {
        "name": "CNN Indonesia Olahraga",
        "url": "https://www.cnnindonesia.com/olahraga/rss",
    }
]


_MONTHS_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember"
]


def _parse_published_value(value):
    """Parse tanggal RSS/HTML (RFC 822, ISO-8601, atau format tanggal Indonesia)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Epoch seconds/milliseconds yang kadang dipakai metadata terstruktur.
    if re.fullmatch(r"\d{10,13}", raw):
        try:
            stamp = int(raw[:10])
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass

    # RFC 822/2822 (format tanggal RSS klasik).
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, OverflowError):
        pass

    # ISO-8601 termasuk akhiran Z dan offset zona waktu.
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, OverflowError):
        pass

    # Format Indonesia, misalnya 14 November 2026 atau 14 Nov 2026 19:30 WIB.
    month_map = {
        "jan": 1, "januari": 1, "feb": 2, "februari": 2,
        "mar": 3, "maret": 3, "apr": 4, "april": 4,
        "mei": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
        "agu": 8, "agustus": 8, "sep": 9, "september": 9,
        "okt": 10, "oktober": 10, "nov": 11, "november": 11,
        "des": 12, "desember": 12,
    }
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", raw, re.I)
    if match:
        month = month_map.get(match.group(2).lower())
        if month:
            try:
                return datetime(
                    int(match.group(3)), month, int(match.group(1)),
                    int(match.group(4) or 0), int(match.group(5) or 0),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass
    return None


def _format_published_date(dt):
    if not dt:
        return "Tanggal tidak tersedia"
    return f"{dt.day} {_MONTHS_ID[dt.month - 1]} {dt.year}"


def _entry_date(entry):
    """Format tanggal RSS ke bahasa Indonesia, termasuk RSS dengan tanggal ISO."""
    raw_date = entry.get("published") or entry.get("updated") or entry.get("created")
    dt = _parse_published_value(raw_date)
    if not dt:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                dt = None
    return _format_published_date(dt)


_ARTICLE_DATE_CACHE = {}
_ARTICLE_DATE_CACHE_TTL = 6 * 60 * 60


def _extract_article_published_date(article_url):
    """Baca tanggal publikasi dari metadata artikel (Open Graph, JSON-LD, atau time)."""
    now = time.time()
    cached = _ARTICLE_DATE_CACHE.get(article_url)
    if cached and now - cached[0] < _ARTICLE_DATE_CACHE_TTL:
        return cached[1], cached[2]

    current_url = article_url
    result = ("Tanggal tidak tersedia", None)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersiboFansJogjaNews/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    date_keys = (
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "date"),
        ("name", "parsely-pub-date"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateCreated"),
    )

    try:
        for _ in range(5):
            if not _is_public_http_url(current_url):
                break
            response = requests.get(
                current_url, headers=headers, timeout=(3, 6),
                allow_redirects=False, stream=True,
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
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 1_200_000:
                    break
                chunks.append(chunk)
            response.close()
            soup = BeautifulSoup(b"".join(chunks), "html.parser")

            candidates = []
            for attr, key in date_keys:
                tag = soup.find("meta", attrs={attr: key})
                if tag:
                    candidates.append(tag.get("content") or tag.get("datetime"))
            time_tag = soup.find("time", datetime=True)
            if time_tag:
                candidates.append(time_tag.get("datetime"))
            for tag in soup.find_all(attrs={"itemprop": "datePublished"}, limit=3):
                candidates.append(tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True))

            # JSON-LD umum dipakai iLeague dan portal berita modern.
            for script in soup.find_all("script", type="application/ld+json"):
                raw_json = script.string or script.get_text()
                try:
                    import json
                    data = json.loads(raw_json)
                    stack = data if isinstance(data, list) else [data]
                    while stack:
                        node = stack.pop()
                        if isinstance(node, dict):
                            for key in ("datePublished", "dateCreated", "dateModified"):
                                if node.get(key):
                                    candidates.append(node[key])
                            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                        elif isinstance(node, list):
                            stack.extend(node)
                except (ValueError, TypeError):
                    continue

            for candidate in candidates:
                dt = _parse_published_value(candidate)
                if dt:
                    result = (_format_published_date(dt), dt.timetuple())
                    break
            break
    except Exception:
        pass

    _ARTICLE_DATE_CACHE[article_url] = (now, result[0], result[1])
    if len(_ARTICLE_DATE_CACHE) > 500:
        for key in list(_ARTICLE_DATE_CACHE)[:100]:
            _ARTICLE_DATE_CACHE.pop(key, None)
    return result


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
_NEWS_CACHE_TTL = 300  # Simpan hasil scraping 5 menit untuk mempercepat kunjungan berikutnya.
_NEWS_CACHE_LOCK = Lock()
_MAX_OG_LOOKUPS = 6  # Batasi permintaan gambar eksternal agar halaman lebih cepat.  # Prioritaskan thumbnail WhatsApp untuk 12 berita terbaru.


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
    """Ambil gambar Open Graph yang biasanya dipakai sebagai pratinjau WhatsApp."""
    return _extract_article_thumbnail(article["link"])



def _is_persibo_relevant(entry):
    """Saring agar feed umum hanya menampilkan berita terkait Persibo."""
    from re import search
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or entry.get("description", "") or ""
    text = (title + " " + summary).lower()

    direct_terms = (
        "persibo",
        "laskar angling dharma",
        "angling dharma",
    )
    if any(term in text for term in direct_terms):
        return True

    # Artikel tentang Bojonegoro hanya diterima jika konteksnya sepak bola.
    has_bojonegoro = "bojonegoro" in text
    football_terms = (
        "sepak bola", "sepakbola", "liga nusantara", "liga 3",
        "liga 2", "klub", "suporter", "pemain", "pelatih",
        "stadion", "pertandingan", "kompetisi", "kesebelasan",
    )
    return has_bojonegoro and any(term in text for term in football_terms)



def _relative_inews_time(text):
    """Ubah label relatif pada indeks iNews (mis. '2 hari lalu') menjadi waktu perkiraan."""
    from re import search
    match = search(r"(\d+)\s*(menit|jam|hari|minggu|bulan)\s+lalu", text.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = {
        "menit": 60,
        "jam": 3600,
        "hari": 86400,
        "minggu": 7 * 86400,
        "bulan": 30 * 86400,
    }[unit]
    return time.gmtime(time.time() - amount * seconds)


def _fetch_inews_bojonegoro():
    """Ambil berita Persibo dari indeks iNews dan tautan AMP/READ, tanpa Google News."""
    from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode
    import html as _html

    host = "bojonegoro.inews.id"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    found = []
    seen_ids = set()

    def _article_id(url):
        match = re.search(r"/(?:amp|read)/(\d+)(?:/|$)", urlparse(url).path, re.I)
        return match.group(1) if match else None

    def _canonical_article_url(url):
        parsed = urlparse(url)
        path = re.sub(r"^/amp/", "/read/", parsed.path, flags=re.I)
        return urlunparse((parsed.scheme or "https", parsed.netloc or host, path, "", "", ""))

    def _append_article(article_id, title, url, published_parsed=None, published="Tanggal tidak tersedia"):
        if not article_id or article_id in seen_ids:
            return
        title = " ".join((title or "").split()).strip()
        if not title:
            return
        seen_ids.add(article_id)
        canonical = _canonical_article_url(url)
        try:
            image = _extract_article_thumbnail(canonical)
        except Exception:
            image = None
        article_date, article_date_parsed = _extract_article_published_date(canonical)
        if article_date != "Tanggal tidak tersedia":
            published = article_date
            published_parsed = article_date_parsed
        elif published_parsed:
            # Cadangan dari label waktu relatif yang disediakan indeks iNews.
            try:
                relative_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                published = _format_published_date(relative_dt)
            except (TypeError, ValueError, OverflowError):
                pass
        found.append({
            "title": title,
            "link": canonical,
            "source": "iNews Bojonegoro",
            "published": published,
            "summary": "",
            "image": image,
            "_published_parsed": published_parsed or time.gmtime(0),
        })

    # Indeks umum, kanal Bojonegoro, kanal Olahraga, dan beberapa bentuk pagination.
    pages = [
        "https://bojonegoro.inews.id/",
        "https://bojonegoro.inews.id/indeks/bojonegoro/all",
        "https://bojonegoro.inews.id/indeks/olahraga/all",
        "https://bojonegoro.inews.id/indeks/all/all",
    ]
    for base in list(pages[1:]):
        for page_no in range(2, 6):
            pages.append(base + "?page={}".format(page_no))

    for page_url in pages:
        try:
            response = requests.get(page_url, headers=headers, timeout=(4, 10))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception:
            continue

        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, _html.unescape(anchor.get("href", "").strip()))
            parsed = urlparse(href)
            if parsed.netloc.lower() not in (host, "www." + host):
                continue
            article_id = _article_id(href)
            if not article_id or article_id in seen_ids:
                continue

            listing_text = " ".join(anchor.get_text(" ", strip=True).split())
            title = re.sub(
                r"^(?:Olahraga|Bojonegoro|Jatim|Nasional|Ekbis|Lifestyle|Wisata dan Religi)\s+"
                r"\d+\s+(?:detik|menit|jam|hari|minggu|bulan)\s+lalu\s+",
                "", listing_text, flags=re.IGNORECASE,
            ).strip()

            # Jika teks anchor kosong atau hanya label, baca metadata halaman artikel.
            if len(title) < 12:
                try:
                    article_response = requests.get(
                        _canonical_article_url(href), headers=headers, timeout=(4, 8)
                    )
                    article_response.raise_for_status()
                    article_soup = BeautifulSoup(article_response.text, "html.parser")
                    meta = article_soup.find("meta", property="og:title")
                    title = (meta.get("content", "") if meta else "") or (
                        article_soup.title.get_text(" ", strip=True) if article_soup.title else ""
                    )
                    title = re.sub(r"\s*[|–-]\s*iNews.*$", "", title, flags=re.I).strip()
                except Exception:
                    title = ""

            # Gunakan judul dan konteks tautan; tidak hanya mengandalkan teks indeks.
            if not _is_persibo_relevant({"title": title, "summary": href}):
                continue

            published_parsed = _relative_inews_time(listing_text)
            _append_article(article_id, title, href, published_parsed)

    return found



def _fetch_ileague_persibo():
    """Telusuri kanal klub dan arsip berita iLeague, tanpa memakai Google News."""
    from urllib.parse import urljoin, urlparse
    from email.utils import parsedate_to_datetime
    import datetime as _datetime

    base_hosts = ("ileague.id", "www.ileague.id", "ligaindonesiabaru.com", "www.ligaindonesiabaru.com")
    club_pages = [
        "https://ileague.id/clubs/single/LIGA_NUSANTARA_2026-27/persibo_bojonegoro",
        "https://www.ileague.id/clubs/single/LIGA_NUSANTARA_2026-27/persibo_bojonegoro",
    ]
    # Halaman arsip utama ikut dipindai. Tautan pagination ditemukan dari HTML,
    # sehingga tidak bergantung pada tebakan nomor halaman tertentu.
    archive_pages = ["https://ileague.id/news", "https://www.ileague.id/news"]
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersiboFansJogjaNews/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    found, seen_articles, seen_pages = [], set(), set()
    seen_article_slugs = set()

    def _article_slug(url):
        path = urlparse(url).path.rstrip("/")
        return path.split("/")[-1].lower() if path else ""

    # Tidak ada artikel yang disisipkan manual; semua berita harus ditemukan dari halaman klub/arsip iLeague.

    # Antrean berisi halaman klub, arsip berita, dan pagination yang ditemukan.
    queue = club_pages + archive_pages
    max_pages = 8
    while queue and len(seen_pages) < max_pages and len(found) < 20:
        page_url = queue.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        try:
            response = requests.get(page_url, headers=headers, timeout=(3, 7))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception:
            continue

        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor.get("href", "").strip()).split("#", 1)[0]
            parsed = urlparse(href)
            host = (parsed.netloc or "").lower()
            if host not in base_hosts:
                continue
            path = parsed.path or ""

            # Ambil tautan artikel dari halaman klub maupun arsip berita.
            href_slug = _article_slug(href)
            if "/news/detail/" in path and href not in seen_articles and href_slug not in seen_article_slugs:
                title = " ".join(anchor.get_text(" ", strip=True).split())
                if not title:
                    title = (anchor.get("title") or "").strip()
                if not title or not _is_persibo_relevant({"title": title, "summary": ""}):
                    continue
                seen_articles.add(href)
                seen_article_slugs.add(href_slug)
                published, published_parsed = _extract_article_published_date(href)
                found.append({
                    "title": title, "link": href, "source": "iLeague",
                    "published": published, "summary": "",
                    "image": None,
                    "_published_parsed": published_parsed or time.gmtime(0),
                })
                if len(found) >= 20:
                    break

            # Ikuti tautan pagination/arsip yang masih berada di area /news.
            if path.startswith("/news") and "/news/detail/" not in path:
                if href not in seen_pages and href not in queue and len(seen_pages) + len(queue) < max_pages:
                    queue.append(href)

    # Lengkapi thumbnail untuk artikel hasil penelusuran; kegagalan gambar
    # tidak menggugurkan artikelnya.
    for article in found:
        if not article.get("image"):
            try:
                article["image"] = _extract_article_thumbnail(article["link"])
            except Exception:
                pass
    return found



def _fetch_berita_bojonegoro():
    """Ambil berita Persibo dari indeks BeritaBojonegoro secara paralel."""
    from urllib.parse import urljoin, urlparse
    import html as _html

    host = "beritabojonegoro.com"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    listing_pages = [
        "https://beritabojonegoro.com/category/olahraga",
        "https://beritabojonegoro.com/",
        "https://beritabojonegoro.com/category/olahraga?page=2",
    ]

    def fetch_listing(url):
        try:
            if not _is_public_http_url(url):
                return url, None
            response = requests.get(url, headers=headers, timeout=(2, 5))
            response.raise_for_status()
            return url, BeautifulSoup(response.text, "html.parser")
        except Exception:
            return url, None

    candidates = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch_listing, url) for url in listing_pages]
        for future in as_completed(futures):
            listing_url, soup = future.result()
            if soup is None:
                continue
            for anchor in soup.find_all("a", href=True):
                href = urljoin(listing_url, _html.unescape(anchor.get("href", "").strip()))
                parsed = urlparse(href)
                if parsed.netloc.lower() not in (host, "www." + host):
                    continue
                match = re.search(r"/read/(\d+)-", parsed.path, re.I)
                if not match:
                    continue
                article_id = match.group(1)
                listing_title = " ".join(anchor.get_text(" ", strip=True).split())
                # Ambil konteks kartu/area terdekat agar judul bisa dikenali
                # meskipun teks anchor hanya berupa gambar atau tautan "Baca".
                if len(listing_title) < 12:
                    parent = anchor
                    for _ in range(3):
                        if parent.parent:
                            parent = parent.parent
                    listing_title = " ".join(parent.get_text(" ", strip=True).split())[:500]
                candidates.setdefault(article_id, (href, listing_title))

    def fetch_article(article_id, article_url, listing_title):
        try:
            if not _is_public_http_url(article_url):
                return None
            response = requests.get(article_url, headers=headers, timeout=(2, 5))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception:
            return None

        meta_title = soup.find("meta", property="og:title")
        title = (meta_title.get("content", "").strip() if meta_title else "") or listing_title
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        title = re.sub(r"\s*[|–-]\s*BeritaBojonegoro.*$", "", title, flags=re.I).strip()

        # Gunakan hanya isi artikel, bukan keseluruhan halaman yang dapat
        # memuat ticker, sidebar, atau tautan berita rekomendasi.
        article_body = (
            soup.select_one(
                ".detail-content, .article-content, .news-detail, "
                ".detail-news, .content-detail, .post-content"
            )
            or soup.find("article")
        )
        if article_body:
            paragraphs = [
                " ".join(p.get_text(" ", strip=True).split())
                for p in article_body.find_all("p")
            ]
            paragraphs = [p for p in paragraphs if p]
            article_excerpt = " ".join(paragraphs[:3])[:1800]
        else:
            meta_description = (
                soup.find("meta", attrs={"name": "description"})
                or soup.find("meta", property="og:description")
            )
            article_excerpt = (
                meta_description.get("content", "").strip()
                if meta_description else ""
            )

        direct_terms = ("persibo", "laskar angling dharma", "angling dharma")
        football_terms = (
            "sepak bola", "sepakbola", "liga nusantara", "liga 3", "liga 2",
            "pemain", "pelatih", "stadion", "pertandingan",
            "kompetisi", "kesebelasan", "persipa",
        )
        title_lower = title.lower()
        excerpt_lower = article_excerpt.lower()
        title_mentions_persibo = any(term in title_lower for term in direct_terms)
        intro_mentions_persibo = any(term in excerpt_lower for term in direct_terms)
        intro_has_football = any(term in excerpt_lower for term in football_terms)

        # Persibo harus disebut pada judul, atau pada paragraf pembuka
        # bersama konteks sepak bola. Teks halaman di luar artikel diabaikan.
        if not (
            title_mentions_persibo
            or (intro_mentions_persibo and intro_has_football)
        ):
            return None

        summary = " ".join(article_excerpt.split())[:420]

        published, published_parsed = _extract_article_published_date(article_url)
        # Gambar diambil belakangan hanya untuk berita terpilih, bukan setiap artikel.
        return {
            "title": title,
            "link": article_url,
            "source": "BeritaBojonegoro.com",
            "published": published,
            "summary": _clean_summary(summary[:420]),
            "image": None,
            "_published_parsed": published_parsed or time.gmtime(0),
        }

    found = []
    # Prioritaskan artikel yang judul/kartu indeksnya jelas menyebut Persibo.
    # Tetap sisakan kandidat umum sebagai cadangan bila judul indeks tidak lengkap.
    def candidate_priority(candidate):
        _article_id, (_url, listing_title) = candidate
        text = (listing_title or "").lower()
        direct = any(term in text for term in (
            "persibo", "laskar angling dharma", "angling dharma"
        ))
        football = any(term in text for term in (
            "liga nusantara", "liga 2", "liga 3", "sepak bola",
            "sepakbola", "persipa", "pertandingan"
        ))
        return (0 if direct and football else 1 if direct else 2)

    ranked_candidates = sorted(candidates.items(), key=candidate_priority)
    # Periksa lebih banyak kandidat, tetapi prioritaskan berita Persibo
    # supaya berita yang jelas relevan tidak terpotong oleh artikel umum.
    selected_candidates = ranked_candidates[:40]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(fetch_article, article_id, url, title)
            for article_id, (url, title) in selected_candidates
        ]
        for future in as_completed(futures):
            try:
                article = future.result()
                if article:
                    found.append(article)
            except Exception:
                pass
    return found


def get_persibo_news(limit=40):
    """Ambil berita dari RSS penerbit secara paralel, ekstrak thumbnail, dan cache 30 detik."""
    global _NEWS_CACHE, _NEWS_CACHE_AT

    now = time.time()
    with _NEWS_CACHE_LOCK:
        if _NEWS_CACHE and now - _NEWS_CACHE_AT < _NEWS_CACHE_TTL:
            return _NEWS_CACHE[:limit]

    collected = []
    seen = set()

    # Jalankan RSS dan seluruh scraper media secara bersamaan agar waktu tunggu
    # tidak dijumlahkan satu per satu.
    source_results = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        rss_futures = {
            executor.submit(_fetch_one_feed, cfg): ("rss", cfg)
            for cfg in RSS_FEEDS
        }
        direct_futures = {
            executor.submit(_fetch_inews_bojonegoro): "inews",
            executor.submit(_fetch_ileague_persibo): "ileague",
            executor.submit(_fetch_berita_bojonegoro): "beritabojonegoro",
        }
        for future in as_completed(list(rss_futures) + list(direct_futures)):
            try:
                if future in rss_futures:
                    feed_config, entries = future.result()
                    for entry in entries:
                        link = (entry.get("link") or "").strip()
                        title = _clean_summary(entry.get("title", ""))
                        if not link or not title or link in seen:
                            continue
                        if not _is_persibo_relevant(entry):
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
                else:
                    source_name = direct_futures[future]
                    source_results[source_name] = future.result()
            except Exception:
                # Satu penerbit yang gagal tidak boleh menggagalkan sumber lainnya.
                continue

    # Gabungkan hasil media langsung dan deduplikasi URL.
    for source_name in ("inews", "ileague", "beritabojonegoro"):
        for article in source_results.get(source_name, []) or []:
            link = article.get("link")
            if link and link not in seen:
                seen.add(link)
                collected.append(article)

    # Dedup iLeague lintas domain/variasi URL berdasarkan slug dan judul.
    # Bila RSS sudah membawa artikel yang sama, buang duplikat iLeague.
    import unicodedata as _unicodedata
    from urllib.parse import urlparse as _urlparse

    def _article_key(item):
        title_key = re.sub(
            r"[^a-z0-9]+", " ",
            _unicodedata.normalize("NFKD", (item.get("title") or "").lower())
            .encode("ascii", "ignore").decode("ascii"),
        ).strip()
        path = _urlparse(item.get("link") or "").path.rstrip("/")
        slug = path.split("/")[-1].lower() if path else ""
        return slug or title_key

    deduped = []
    seen_article_keys = set()
    for item in collected:
        key = _article_key(item)
        if key and key in seen_article_keys:
            continue
        if key:
            seen_article_keys.add(key)
        deduped.append(item)
    collected = deduped

    # Urutkan terbaru lebih dulu bila tanggal RSS tersedia.
    collected.sort(
        key=lambda item: item.get("_published_parsed") or (0, 0, 0, 0, 0, 0),
        reverse=True,
    )
    selected = collected[:limit]

    # Prioritaskan og:image halaman artikel untuk berita terbaru.
    # WhatsApp umumnya menggunakan og:image sebagai gambar pratinjau tautan.
    # Jika metadata tidak tersedia, gambar dari RSS tetap dipakai sebagai cadangan.
    og_candidates = [item for item in selected if not item.get('image')][:_MAX_OG_LOOKUPS]
    if og_candidates:
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(_og_image_for_article, item): item
                for item in og_candidates
            }
            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    og_image = future.result()
                    if og_image:
                        item["image"] = og_image
                except Exception:
                    # Pertahankan gambar RSS jika pengambilan metadata gagal.
                    pass

    # Hilangkan field internal sebelum dikirim ke template.
    for item in selected:
        item.pop("_published_parsed", None)

    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE = selected
        _NEWS_CACHE_AT = time.time()

    return selected




ILEAGUE_SEASON_URL = "https://ileague.id/fixtures/index/LIGA_NUSANTARA_2026-27/3/0"
ILEAGUE_SUMMARY_URL = "https://ileague.id/matchweek_summary/index/LIGA_NUSANTARA_2026-27"
ILEAGUE_RESULT_PREFIX = "https://ileague.id/result/detail/"

def _ileague_get(url):
    response = requests.get(
        url, timeout=(5, 12),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://ileague.id/home/index/LIGA_NUSANTARA_2026-27",
        }
    )
    response.raise_for_status()
    return response

def _fixture_date_from_url(url):
    match = re.search(r"/(20\d{2}-\d{2}-\d{2})/", url or "")
    if not match:
        return "", ""
    raw = match.group(1)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
        days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
        months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
                  "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
        pretty = f"{days[dt.weekday()]}, {dt.day} {months[dt.month - 1]} {dt.year}"
        return raw, pretty
    except ValueError:
        return raw, raw

def _fixture_teams_from_url(url):
    # Bentuk tautan iLeague: .../tanggal/HOME_TEAM/AWAY_TEAM
    parts = (url or "").split("/")
    try:
        idx = parts.index("detail")
        home_slug, away_slug = parts[idx + 3], parts[idx + 4]
        home = home_slug.replace("_", " ")
        away = away_slug.replace("_", " ")
        return home, away
    except (ValueError, IndexError):
        return "", ""

def _get_persibo_fixtures():
    """Ambil fixture dari tautan detail pertandingan resmi iLeague."""
    result = {
        "matches": [], "source": ILEAGUE_SEASON_URL,
        "updated": datetime.now().strftime("%d-%m-%Y %H:%M WIB"),
        "error": None
    }
    try:
        response = _ileague_get(ILEAGUE_SEASON_URL)
        soup = BeautifulSoup(response.text, "html.parser")
        matches, seen = [], set()
        for link in soup.select('a[href*="/result/detail/"]'):
            href = urljoin(response.url, link.get("href", ""))
            if "PERSIBO_BOJONEGORO" not in href.upper():
                continue
            if href in seen:
                continue
            seen.add(href)

            home, away = _fixture_teams_from_url(href)
            date_iso, date_label = _fixture_date_from_url(href)

            # Ambil teks dari container terdekat yang mencakup kedua klub.
            context = link
            context_text = " ".join(link.stripped_strings)
            for parent in link.parents:
                if getattr(parent, "name", None) in ("body", "html"):
                    break
                candidate = " ".join(parent.stripped_strings)
                if len(candidate) > 1200:
                    continue
                if "PERSIBO" in candidate.upper() and (
                    (home and home.upper() in candidate.upper()) or
                    (away and away.upper() in candidate.upper())
                ):
                    context, context_text = parent, candidate
                    break

            # Kadang teks klub terletak di elemen saudara, sehingga fallback ke link.
            if not context_text:
                context_text = " ".join(link.stripped_strings)

            time_match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", context_text)
            venue = ""
            for node in context.find_all(["span", "small", "p", "div"]) if hasattr(context, "find_all") else []:
                value = " ".join(node.stripped_strings)
                if value and len(value) < 100 and any(
                    word in value.lower() for word in ("stadion", "gelora", "soedirman", "hang tuah", "wibawa", "kebogiro", "pasiran")
                ):
                    venue = value
                    break
            if not venue:
                venue_match = re.search(
                    r"(?:Letjen|Stadion|Gelora|Moch\.|Sultan|Wibawa|Kebogiro|Pasiran)[^|,]{2,90}",
                    context_text, re.I
                )
                venue = venue_match.group(0).strip() if venue_match else ""

            score_match = re.search(r"\b(\d{1,2})\s*[:\-]\s*(\d{1,2})\b", context_text)
            score = f"{score_match.group(1)} - {score_match.group(2)}" if score_match else ""
            matches.append({
                "raw": context_text or f"{home} vs {away}",
                "cells": [],
                "date": date_label,
                "date_iso": date_iso,
                "time": time_match.group(0) if time_match else "",
                "home": home,
                "away": away,
                "opponent": away if "PERSIBO" in home.upper() else home,
                "venue": venue,
                "score": score,
                "status": "Selesai" if score_match else "Terjadwal",
                "match_url": href
            })

        # Urutkan secara kronologis jika tanggal tersedia.
        matches.sort(key=lambda m: m.get("date_iso") or "9999-99-99")
        result["matches"] = matches
        if not matches:
            result["error"] = (
                "Belum ditemukan tautan detail pertandingan Persibo pada halaman fixtures iLeague."
            )
    except Exception as exc:
        result["error"] = "Halaman jadwal iLeague belum dapat dimuat."
        result["detail"] = str(exc)[:240]
    return result

def _ileague_fixtures_diagnostics():
    """Diagnostik struktur fixture beserta tautan detail Persibo."""
    output = {"fixtures_url": ILEAGUE_SEASON_URL, "summary_url": ILEAGUE_SUMMARY_URL}
    try:
        response = _ileague_get(ILEAGUE_SEASON_URL)
        soup = BeautifulSoup(response.text, "html.parser")
        links = []
        for link in soup.select('a[href*="/result/detail/"]'):
            href = urljoin(response.url, link.get("href", ""))
            if "PERSIBO_BOJONEGORO" in href.upper():
                links.append({
                    "text": " ".join(link.stripped_strings),
                    "href": href,
                    "context": " ".join(link.parent.stripped_strings)[:500]
                })
        output["fixtures"] = {
            "http_status": response.status_code,
            "final_url": response.url,
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "html_length": len(response.text),
            "persibo_match_links": links[:30]
        }
    except Exception as exc:
        output["fixtures"] = {"error": str(exc)[:300]}
    return output


ADMIN_HOST = "admin.persibofansjogja.web.id"


def _is_admin_host():
    """Batasi halaman dan API pengujian ke subdomain admin."""
    return (request.host or "").split(":", 1)[0].lower() == ADMIN_HOST


@app.route("/", methods=["GET", "POST"])
def home():
    """Halaman login admin; dashboard CRUD hanya tampil setelah login."""
    if not _is_admin_host():
        return render_template("index.html")

    if request.method == "POST":
        import hmac
        username = os.getenv("ADMIN_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")
        secret = os.getenv("FLASK_SECRET_KEY", "")
        supplied_user = request.form.get("username", "")
        supplied_password = request.form.get("password", "")
        if not username or not password or not secret:
            return render_template(
                "admin_login.html",
                error="Konfigurasi login belum lengkap. Hubungi administrator."
            ), 503
        if (hmac.compare_digest(supplied_user, username)
                and hmac.compare_digest(supplied_password, password)):
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            return redirect(url_for("home"))
        return render_template(
            "admin_login.html", error="Username atau password salah."
        ), 401

    if session.get("admin_authenticated"):
        return render_template("test_crud.html")
    return render_template("admin_login.html")


@app.post("/logout")
def admin_logout():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
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


