"""iLeague fixture retrieval and normalization services."""

import re
import time
from copy import deepcopy
from datetime import datetime
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


ILEAGUE_SEASON_URL = "https://ileague.id/fixtures/index/LIGA_NUSANTARA_2026-27/3/0"
ILEAGUE_SUMMARY_URL = "https://ileague.id/matchweek_summary/index/LIGA_NUSANTARA_2026-27"
ILEAGUE_RESULT_PREFIX = "https://ileague.id/result/detail/"

# Cache proses lokal: membantu request berulang pada instance Vercel yang sama.
# Cache ini bersifat best-effort; serverless cold start dapat memulai cache baru.
_FIXTURES_CACHE_TTL = 300
_FIXTURES_CACHE = None
_FIXTURES_CACHE_AT = 0.0
_FIXTURES_CACHE_LOCK = Lock()


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

def _fetch_persibo_fixtures():
    """Ambil seluruh jadwal Persibo dari semua pekan Liga Nusantara."""
    base = "https://ileague.id/fixtures/index/LIGA_NUSANTARA_2026-27"
    result = {
        "matches": [], "source": base,
        "updated": datetime.now().strftime("%d-%m-%Y %H:%M WIB"),
        "error": None
    }
    try:
        matches, seen = [], set()

        # Grup Persibo berisi 8 klub: format kandang-tandang menghasilkan 14 pekan.
        # Jangan hanya membaca pekan aktif (sebelumnya hanya /3/0).
        for week in range(1, 15):
            page_url = f"{base}/{week}/0"
            try:
                response = _ileague_get(page_url)
                soup = BeautifulSoup(response.text, "html.parser")
            except Exception:
                continue

            for link in soup.select('a[href*="/result/detail/"]'):
                href = urljoin(response.url, link.get("href", ""))
                if "PERSIBO_BOJONEGORO" not in href.upper() or href in seen:
                    continue

                home, away = _fixture_teams_from_url(href)
                home = (home or "").strip()
                away = (away or "").strip()
                date_iso, date_label = _fixture_date_from_url(href)
                if not home or not away:
                    continue
                seen.add(href)

                # Ambil konteks terdekat yang memuat kedua nama klub.
                context_text = " ".join(link.stripped_strings)
                best_length = None
                for parent in link.parents:
                    if getattr(parent, "name", None) in ("body", "html"):
                        break
                    candidate = " ".join(parent.stripped_strings)
                    upper = candidate.upper()
                    if home.upper() in upper and away.upper() in upper:
                        if best_length is None or len(candidate) < best_length:
                            context_text = candidate
                            best_length = len(candidate)
                    if best_length is not None and len(candidate) > best_length * 2:
                        break

                # Jangan mengambil skor dari teks fixture umum karena satu blok
                # HTML dapat memuat skor/angka pertandingan lain. Verifikasi hasil
                # hanya dari halaman detail pertandingan yang bersangkutan.
                score = ""
                finished = False
                kickoff = ""
                venue = ""
                detail_text = ""
                try:
                    detail_response = _ileague_get(href)
                    detail_soup = BeautifulSoup(detail_response.text, "html.parser")
                    detail_text = " ".join(detail_soup.stripped_strings)

                    # Di halaman detail iLeague skor akhir ditampilkan setelah label FT,
                    # misalnya: FT 2 : 1. Ini menghindari salah baca 2-2 dari halaman jadwal.
                    detail_ft = re.search(
                        r"\bFT\s*(\d{1,2})\s*:\s*(\d{1,2})\b",
                        detail_text, re.I
                    )
                    if detail_ft:
                        score = f"{detail_ft.group(1)} - {detail_ft.group(2)}"
                        finished = True

                    # Waktu kick-off di halaman detail ditampilkan sebagai
                    # "Kick Off 15:30 WIB". Longgarkan spasi/line break HTML.
                    kickoff_detail = re.search(
                        r"Kick\s*Off\s*[:：]?\s*(\d{1,2})\s*:\s*(\d{2})",
                        detail_text, re.I
                    )
                    if not kickoff_detail:
                        kickoff_detail = re.search(
                            r"(\d{1,2})\s*:\s*(\d{2})\s*WIB",
                            detail_text, re.I
                        )
                    if kickoff_detail:
                        kickoff = (
                            f"{kickoff_detail.group(1).zfill(2)}:"
                            f"{kickoff_detail.group(2)}"
                        )

                    # Stadion berada tepat di bawah tanggal pada halaman detail,
                    # contohnya "Stadion Letjen H. Soedirman , Bojonegoro".
                    venue_detail = re.search(
                        r"\b(Stadion\s+[^,|]+)",
                        detail_text, re.I
                    )
                    if venue_detail:
                        venue = re.sub(
                            r"\s+", " ", venue_detail.group(1)
                        ).strip(" ,")
                except Exception:
                    # Jika halaman detail gagal diakses, jangan menebak skor.
                    pass

                # Waktu dari kartu fixture hanya dipakai bila detail tidak
                # menyediakan Kick Off.
                if not kickoff:
                    time_match = re.search(
                        r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)",
                        context_text
                    )
                    if time_match:
                        kickoff = (
                            f"{time_match.group(1).zfill(2)}:"
                            f"{time_match.group(2)}"
                        )

                # Gunakan venue hasil halaman detail. Tambahkan fallback hanya
                # untuk pertandingan yang sudah terkonfirmasi sebelumnya.
                if not venue and (home.upper(), away.upper(), date_iso) == (
                    "PERSIBO BOJONEGORO", "GRESIK UNITED FC", "2026-11-22"
                ):
                    venue = "Stadion Letjen H. Soedirman"
                if not venue and (home.upper(), away.upper(), date_iso) == (
                    "PERSIBO BOJONEGORO", "PERSIPA", "2026-11-13"
                ):
                    venue = "Stadion Letjen H. Soedirman"

                # Normalisasi aman: hilangkan awalan "Stadion" yang terduplikasi
                # dan jangan tampilkan 00:00 sebagai jam kick-off jika sumber
                # tidak menyediakan waktu pertandingan.
                venue = re.sub(r"^(?:Stadion\\s+){2,}", "Stadion ", venue, flags=re.I)
                venue = re.sub(r"\\s+", " ", venue).strip(" ,") if venue else ""
                if kickoff == "00:00":
                    kickoff = ""

                matches.append({
                    "raw": f"{home} vs {away}",
                    "cells": [],
                    "date": date_label,
                    "date_iso": date_iso,
                    "time": kickoff,
                    "home": home,
                    "away": away,
                    "opponent": away if "PERSIBO" in home.upper() else home,
                    "venue": venue,
                    "score": score,
                    "status": "Selesai" if finished else "Terjadwal",
                    "match_url": href,
                    "matchweek": week
                })

        matches.sort(key=lambda m: (m.get("date_iso") or "9999-99-99", m.get("matchweek", 99)))
        result["matches"] = matches
        result["pages_checked"] = 14
        if not matches:
            result["error"] = "Belum ditemukan tautan detail pertandingan Persibo pada 14 pekan jadwal iLeague."
    except Exception as exc:
        result["error"] = "Halaman jadwal iLeague belum dapat dimuat."
        result["detail"] = str(exc)[:240]
    return result


def _get_persibo_fixtures():
    """Ambil jadwal dari cache 5 menit sebelum melakukan scraping ulang."""
    global _FIXTURES_CACHE, _FIXTURES_CACHE_AT

    now = time.monotonic()
    with _FIXTURES_CACHE_LOCK:
        if (
            _FIXTURES_CACHE is not None
            and now - _FIXTURES_CACHE_AT < _FIXTURES_CACHE_TTL
        ):
            return deepcopy(_FIXTURES_CACHE)

        result = _fetch_persibo_fixtures()

        # Jangan cache respons kosong/error agar kegagalan sumber tidak menetap.
        if result.get("matches") and not result.get("error"):
            _FIXTURES_CACHE = deepcopy(result)
            _FIXTURES_CACHE_AT = time.monotonic()

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
