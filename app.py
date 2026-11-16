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
from flask import Flask, Response, render_template

app = Flask(__name__)

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
_NEWS_CACHE_TTL = 30
_NEWS_CACHE_LOCK = Lock()
_MAX_OG_LOOKUPS = 12  # Prioritaskan thumbnail WhatsApp untuk 12 berita terbaru.


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
    """Ambil berita Persibo secara otomatis dari kategori olahraga dan beranda BeritaBojonegoro.com."""
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
    ]
    for page_no in range(2, 6):
        listing_pages.append(
            "https://beritabojonegoro.com/category/olahraga?page={}".format(page_no)
        )

    found = []
    seen_ids = set()

    def _article_id(url):
        match = re.search(r"/read/(\d+)-", urlparse(url).path, re.I)
        return match.group(1) if match else None

    for listing_url in listing_pages:
        try:
            if not _is_public_http_url(listing_url):
                continue
            response = requests.get(listing_url, headers=headers, timeout=(4, 9))
            response.raise_for_status()
            listing_soup = BeautifulSoup(response.text, "html.parser")
        except Exception:
            continue

        candidates = {}
        for anchor in listing_soup.find_all("a", href=True):
            href = urljoin(listing_url, _html.unescape(anchor.get("href", "").strip()))
            parsed = urlparse(href)
            if parsed.netloc.lower() not in (host, "www." + host):
                continue
            article_id = _article_id(href)
            if not article_id or article_id in seen_ids:
                continue
            title = " ".join(anchor.get_text(" ", strip=True).split())
            candidates.setdefault(article_id, (href, title))

        for article_id, (article_url, listing_title) in candidates.items():
            try:
                if not _is_public_http_url(article_url):
                    continue
                article_response = requests.get(
                    article_url, headers=headers, timeout=(4, 9)
                )
                article_response.raise_for_status()
                article_soup = BeautifulSoup(article_response.text, "html.parser")
            except Exception:
                continue

            meta_title = article_soup.find("meta", property="og:title")
            title = (
                (meta_title.get("content", "").strip() if meta_title else "")
                or listing_title
            )
            if not title and article_soup.title:
                title = article_soup.title.get_text(" ", strip=True)
            title = re.sub(r"\s*[|–-]\s*BeritaBojonegoro.*$", "", title, flags=re.I).strip()

            main = article_soup.find("article") or article_soup.find("main") or article_soup
            summary = " ".join(main.get_text(" ", strip=True).split())[:5000]
            if not _is_persibo_relevant({"title": title, "summary": summary}):
                continue

            seen_ids.add(article_id)
            published, published_parsed = _extract_article_published_date(article_url)
            image = None
            try:
                image = _extract_article_thumbnail(article_url)
            except Exception:
                pass

            found.append({
                "title": title,
                "link": article_url,
                "source": "BeritaBojonegoro.com",
                "published": published,
                "summary": _clean_summary(summary[:420]),
                "image": image,
                "_published_parsed": published_parsed or time.gmtime(0),
            })

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

    # RSS penerbit diproses paralel; iNews dan iLeague juga dipindai sebagai sumber langsung.
    with ThreadPoolExecutor(max_workers=min(4, len(RSS_FEEDS) or 1)) as executor:
        futures = [executor.submit(_fetch_one_feed, cfg) for cfg in RSS_FEEDS]
        for future in as_completed(futures):
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

    # Tambahkan berita iNews Bojonegoro dari indeks kanal Olahraga dan indeks umum.
    # Sumber ini tidak memakai Google News dan difilter khusus untuk Persibo/Bojonegoro.
    try:
        inews_articles = _fetch_inews_bojonegoro()
        for article in inews_articles:
            if article["link"] not in seen:
                seen.add(article["link"])
                collected.append(article)
    except Exception:
        pass

    # Tambahkan berita Persibo dari BeritaBojonegoro.com melalui indeks kategori/beranda.
    # Tidak ada artikel yang ditanam manual; seluruh item harus ditemukan dari situs penerbit.
    try:
        bojonegoro_articles = _fetch_berita_bojonegoro()
        for article in bojonegoro_articles:
            if article["link"] not in seen:
                seen.add(article["link"])
                collected.append(article)
    except Exception:
        pass

    # Tambahkan berita resmi dari iLeague. Dedup tidak hanya berdasarkan URL,
    # karena satu artikel kadang muncul dengan host/URL berbeda (ileague.id
    # dan ligaindonesiabaru.com) atau variasi garis miring.
    try:
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

        existing_ileague_keys = {
            _article_key(item) for item in collected
            if (item.get("source") or "").lower() == "ileague"
        }
        ileague_articles = _fetch_ileague_persibo()
        for article in ileague_articles:
            key = _article_key(article)
            if article.get("link") not in seen and key not in existing_ileague_keys:
                seen.add(article["link"])
                existing_ileague_keys.add(key)
                collected.append(article)
    except Exception:
        pass

    # Urutkan terbaru lebih dulu bila tanggal RSS tersedia.
    collected.sort(
        key=lambda item: item.get("_published_parsed") or (0, 0, 0, 0, 0, 0),
        reverse=True,
    )
    selected = collected[:limit]

    # Prioritaskan og:image halaman artikel untuk berita terbaru.
    # WhatsApp umumnya menggunakan og:image sebagai gambar pratinjau tautan.
    # Jika metadata tidak tersedia, gambar dari RSS tetap dipakai sebagai cadangan.
    og_candidates = selected[:_MAX_OG_LOOKUPS]
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
