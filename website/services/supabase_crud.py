"""HTTP adapter for the temporary admin CRUD test table."""
import os
import re
import requests
from flask import jsonify, request


def handle_test_crud():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY") or "").strip()
    table = os.getenv("SUPABASE_TEST_TABLE", "").strip()
    id_column = os.getenv("SUPABASE_TEST_ID_COLUMN", "id").strip()
    missing = [name for name, value in (("SUPABASE_URL", url), ("SUPABASE_KEY atau SUPABASE_PUBLISHABLE_KEY", key), ("SUPABASE_TEST_TABLE", table)) if not value]
    if missing:
        return jsonify(error="Konfigurasi Supabase belum lengkap.", missing=missing), 503
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", id_column):
        return jsonify(error="Nama tabel atau kolom ID tidak valid."), 400
    if not url.startswith("https://") or not url.endswith(".supabase.co"):
        return jsonify(error="SUPABASE_URL tidak valid."), 400

    endpoint = f"{url}/rest/v1/{table}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"}
    try:
        if request.method == "GET":
            response = requests.get(endpoint, headers=headers, params={"select": "*", "limit": "100", "order": f"{id_column}.desc"}, timeout=12)
        elif request.method == "POST":
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or not payload:
                return jsonify(error="Isi data harus berupa objek JSON."), 400
            response = requests.post(endpoint, headers={**headers, "Prefer": "return=representation"}, json=payload, timeout=12)
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
                response = requests.patch(endpoint, headers={**headers, "Prefer": "return=representation"}, params=params, json=changes, timeout=12)
            else:
                response = requests.delete(endpoint, headers={**headers, "Prefer": "return=representation"}, params=params, timeout=12)
        if not response.ok:
            return jsonify(error="Supabase menolak permintaan. Periksa nama tabel, kolom ID, struktur data, dan kebijakan RLS.", supabase_status=response.status_code, details=response.text[:1200]), 502
        try:
            return jsonify(records=response.json())
        except ValueError:
            return jsonify(records=[])
    except requests.RequestException:
        return jsonify(error="Tidak dapat menghubungi Supabase."), 502
