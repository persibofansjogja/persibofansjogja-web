"""Protected scheduled synchronization endpoint for fixture archiving."""
import hmac
import os
from flask import Blueprint, jsonify, request
from ..services.fixtures import sync_persibo_fixtures

sync_bp = Blueprint("fixtures_sync", __name__)


@sync_bp.post("/api/cron/sync-fixtures")
def sync_fixtures_cron():
    expected = os.getenv("CRON_SECRET", "").strip()
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return jsonify(error="Unauthorized"), 401
    result = sync_persibo_fixtures()
    if not result.get("matches"):
        return jsonify(
            ok=False,
            message="Tidak ada data sumber yang valid; arsip lama tidak dihapus.",
            source_error=result.get("error"),
            storage_error=result.get("storage_error"),
        ), 502
    return jsonify(
        ok=True,
        found=len(result["matches"]),
        saved=result.get("saved_count", 0),
        storage=result.get("storage", "unknown"),
        storage_error=result.get("storage_error"),
        updated=result.get("updated"),
    )
