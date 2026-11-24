"""Admin-only routes for login, manual fixture sync, and archive editing."""
import hmac
import logging
import os
import re
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, session

from ..services.fixtures import (
    _load_saved_fixtures, _supabase_config, _upsert_admin_fixture,
    sync_persibo_fixtures, get_fixture_auto_sync_state,
    fixture_auto_sync_due, record_fixture_sync_state,
)
from ..services.supabase_crud import handle_test_crud
from ..utils.security import csrf_token, valid_csrf_token

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)
ADMIN_HOST = "admin.persibofansjogja.web.id"
FIXTURE_FIELDS = {"date", "date_iso", "time", "home", "away", "venue", "score", "status", "matchweek"}


def _is_admin_host():
    return (request.host or "").split(":", 1)[0].lower() == ADMIN_HOST


def _authorized_api():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not session.get("admin_authenticated"):
        return jsonify(error="Sesi admin berakhir. Silakan login kembali."), 401
    if not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    return None


def admin_home():
    """Render existing admin login or the fixture archive dashboard."""
    if request.method == "POST":
        if not valid_csrf_token():
            return render_template("admin_login.html", error="Sesi formulir tidak valid. Muat ulang halaman lalu coba kembali.", csrf_token=csrf_token()), 400
        username, password, secret = os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", ""), os.getenv("FLASK_SECRET_KEY", "")
        supplied_user, supplied_password = request.form.get("username", ""), request.form.get("password", "")
        if not username or not password or not secret:
            return render_template("admin_login.html", error="Konfigurasi login belum lengkap. Hubungi administrator.", csrf_token=csrf_token()), 503
        if hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password):
            session.clear()
            session["admin_authenticated"] = True
            session.permanent = True
            csrf_token()
            return redirect("/")
        return render_template("admin_login.html", error="Username atau password salah.", csrf_token=csrf_token()), 401
    if session.get("admin_authenticated"):
        return render_template("fixtures_admin.html", csrf_token=csrf_token())
    return render_template("admin_login.html", csrf_token=csrf_token())


@admin_bp.post("/logout")
def admin_logout():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    session.clear()
    return redirect("/")


@admin_bp.route("/fixtures-admin/api", methods=["GET", "POST", "PATCH"])
def fixtures_admin_api():
    """Read, add, and edit archived fixtures. Delete is intentionally unavailable."""
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not session.get("admin_authenticated"):
        return jsonify(error="Sesi admin berakhir. Silakan login kembali."), 401
    if request.method != "GET" and not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    if not _supabase_config():
        return jsonify(error="Konfigurasi Supabase arsip belum lengkap."), 503
    if request.method == "GET":
        try:
            data = _load_saved_fixtures() or {"matches": []}
            return jsonify(
                matches=data.get("matches", []), updated=data.get("updated"),
                auto_sync=get_fixture_auto_sync_state(),
            )
        except Exception as exc:
            return jsonify(error="Arsip Supabase tidak dapat dibaca.", details=str(exc)[:200]), 502

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error="Format data tidak valid."), 400
    values = payload.get("data", payload)
    if not isinstance(values, dict):
        return jsonify(error="Format data pertandingan tidak valid."), 400
    match_url = str(payload.get("match_url") or values.get("match_url") or "").strip()
    if request.method == "POST" and not match_url:
        match_url = "manual:" + str(uuid.uuid4())
    if not match_url or len(match_url) > 1000:
        return jsonify(error="Identitas pertandingan tidak valid."), 400

    cleaned = {}
    for field in FIXTURE_FIELDS:
        if field in values:
            value = values[field]
            if value is None:
                value = ""
            if field == "matchweek" and value == "":
                continue
            if field == "matchweek" and value != "":
                try:
                    value = int(value)
                    if value < 1 or value > 99:
                        raise ValueError()
                except (TypeError, ValueError):
                    return jsonify(error="Pekan pertandingan harus berupa angka 1–99."), 400
            else:
                value = str(value).strip()
                if len(value) > 500:
                    return jsonify(error=f"Kolom {field} terlalu panjang."), 400
            cleaned[field] = value
    if request.method == "POST":
        for required in ("home", "away", "date_iso"):
            if not cleaned.get(required):
                return jsonify(error=f"Kolom {required} wajib diisi."), 400
    if "date_iso" in cleaned and cleaned["date_iso"]:
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", cleaned["date_iso"]):
            return jsonify(error="Format tanggal harus YYYY-MM-DD."), 400
        try:
            datetime.strptime(cleaned["date_iso"], "%Y-%m-%d")
        except ValueError:
            return jsonify(error="Tanggal pertandingan tidak valid."), 400
    if "status" in cleaned and cleaned["status"] not in {"Terjadwal", "Selesai", "Ditunda", "Dibatalkan"}:
        return jsonify(error="Status pertandingan tidak dikenal."), 400
    try:
        _upsert_admin_fixture(match_url, cleaned)
        return jsonify(ok=True, message="Data pertandingan berhasil disimpan sebagai arsip manual.")
    except Exception as exc:
        return jsonify(error="Data pertandingan gagal disimpan.", details=str(exc)[:200]), 502


@admin_bp.post("/fixtures-admin/sync")
def fixtures_admin_sync():
    """Fetch current iLeague data only when an authenticated admin requests it."""
    denied = _authorized_api()
    if denied:
        return denied
    if not _supabase_config():
        return jsonify(error="Konfigurasi Supabase arsip belum lengkap."), 503
    result = sync_persibo_fixtures()
    if result.get("error") or not result.get("matches") or result.get("storage_error"):
        message = result.get("storage_error") or result.get("error") or "iLeague tidak mengembalikan data pertandingan."
        try:
            record_fixture_sync_state("failed", message, source="manual")
        except Exception:
            pass
        return jsonify(error=message, archived_data_preserved=True), 502
    try:
        record_fixture_sync_state(
            "success", "Sinkronisasi manual berhasil.", source="manual",
            found=len(result["matches"]), saved=result.get("saved_count", 0),
            skipped_manual=result.get("skipped_manual", 0),
        )
    except Exception:
        pass
    return jsonify(
        ok=True, found=len(result["matches"]), saved=result.get("saved_count", 0),
        skipped_manual=result.get("skipped_manual", 0), updated=result.get("updated"),
        message="Sinkronisasi selesai. Arsip lama yang tidak ditemukan di sumber tetap dipertahankan.",
    )


@admin_bp.post("/api/fixtures-auto-sync")
def fixtures_auto_sync():
    """Authenticated machine endpoint called by GitHub Actions."""
    expected = os.getenv("GITHUB_SYNC_TOKEN", "").strip()
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return jsonify(error="Unauthorized"), 401
    if not _supabase_config():
        return jsonify(error="Konfigurasi Supabase arsip belum lengkap."), 503
    try:
        due, interval, state = fixture_auto_sync_due()
        if not due:
            return jsonify(
                ok=True, skipped=True, reason="Belum waktunya sinkronisasi adaptif.",
                interval_minutes=interval,
                next_run_at=(state or {}).get("next_run_at"),
            )
        result = sync_persibo_fixtures()
        if result.get("error") or not result.get("matches") or result.get("storage_error"):
            message = result.get("storage_error") or result.get("error") or "iLeague tidak mengembalikan data."
            record_fixture_sync_state("failed", message, source="github_actions",
                                      interval_minutes=interval)
            return jsonify(error=message, archived_data_preserved=True), 502
        state_row = record_fixture_sync_state(
            "success", "Sinkronisasi otomatis berhasil.", source="github_actions",
            interval_minutes=interval, found=len(result["matches"]),
            saved=result.get("saved_count", 0), skipped_manual=result.get("skipped_manual", 0),
        )
        return jsonify(
            ok=True, skipped=False, found=len(result["matches"]),
            saved=result.get("saved_count", 0),
            skipped_manual=result.get("skipped_manual", 0),
            interval_minutes=interval, next_run_at=state_row.get("next_run_at"),
        )
    except Exception as exc:
        try:
            record_fixture_sync_state("failed", str(exc)[:500], source="github_actions")
        except Exception:
            pass
        return jsonify(error="Sinkronisasi otomatis gagal.", details=str(exc)[:500],
                       archived_data_preserved=True), 502

@admin_bp.get("/test-crud")
def test_crud_page():
    if _is_admin_host():
        return redirect("/", code=301)
    return jsonify(error="Not found"), 404


@admin_bp.route("/test-crud/api", methods=["GET", "POST", "PATCH", "DELETE"])
def test_crud_api():
    if not _is_admin_host():
        return jsonify(error="Not found"), 404
    if not session.get("admin_authenticated"):
        return jsonify(error="Sesi admin berakhir. Silakan login kembali."), 401
    if request.method in {"POST", "PATCH", "DELETE"} and not valid_csrf_token():
        return jsonify(error="Token CSRF tidak valid."), 400
    return handle_test_crud()
