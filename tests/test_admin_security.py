"""Admin authentication, CSRF, and CRUD boundary tests."""

import os
import re

ADMIN_HOST = "admin.persibofansjogja.web.id"


def _csrf_from(response):
    html = response.get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "Token CSRF tidak ditemukan pada halaman login"
    return match.group(1)


def _login(client, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "pytest-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "pytest-password")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-only-secret-key-not-for-production")
    page = client.get("/", headers={"Host": ADMIN_HOST})
    token = _csrf_from(page)
    return client.post(
        "/",
        headers={"Host": ADMIN_HOST},
        data={
            "username": "pytest-admin",
            "password": "pytest-password",
            "csrf_token": token,
        },
        follow_redirects=False,
    )


def test_admin_login_page_is_only_on_admin_host(client):
    admin_page = client.get("/", headers={"Host": ADMIN_HOST})
    public_page = client.get("/", headers={"Host": "www.persibofansjogja.web.id"})
    assert admin_page.status_code == 200
    assert b"Login Admin" in admin_page.data
    assert public_page.status_code == 200
    assert b"Login Admin" not in public_page.data


def test_admin_login_rejects_missing_csrf(client, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "pytest-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "pytest-password")
    response = client.post(
        "/", headers={"Host": ADMIN_HOST},
        data={"username": "pytest-admin", "password": "pytest-password"},
    )
    assert response.status_code == 400


def test_admin_login_accepts_valid_credentials(client, monkeypatch):
    response = _login(client, monkeypatch)
    assert response.status_code == 302
    dashboard = client.get("/", headers={"Host": ADMIN_HOST})
    assert dashboard.status_code == 200
    assert b"Arsip Jadwal & Hasil" in dashboard.data


def test_crud_api_requires_admin_session(client):
    response = client.get("/test-crud/api", headers={"Host": ADMIN_HOST})
    assert response.status_code == 401


def test_crud_api_rejects_missing_csrf(client, monkeypatch):
    _login(client, monkeypatch)
    response = client.post(
        "/test-crud/api",
        headers={"Host": ADMIN_HOST, "Content-Type": "application/json"},
        json={"name": "blocked"},
    )
    assert response.status_code == 400


def test_crud_api_reports_missing_supabase_configuration(client, monkeypatch):
    _login(client, monkeypatch)
    for key in (
        "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_TEST_TABLE",
    ):
        monkeypatch.delenv(key, raising=False)
    response = client.get("/test-crud/api", headers={"Host": ADMIN_HOST})
    assert response.status_code == 503
    assert "Konfigurasi Supabase" in response.get_json()["error"]


def test_crud_api_rejects_invalid_table_name_before_network(client, monkeypatch):
    _login(client, monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_TEST_TABLE", "table;drop table users")
    with client.session_transaction(base_url=f"http://{ADMIN_HOST}") as session:
        csrf = session["csrf_token"]
    response = client.post(
        "/test-crud/api",
        headers={
            "Host": ADMIN_HOST,
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
        },
        json={"name": "test"},
    )
    assert response.status_code == 400


def test_crud_api_is_not_exposed_on_public_host(client):
    response = client.get(
        "/test-crud/api",
        headers={"Host": "www.persibofansjogja.web.id"},
    )
    assert response.status_code == 404


def test_admin_logout_requires_csrf_and_clears_session(client, monkeypatch):
    _login(client, monkeypatch)
    with client.session_transaction(base_url=f"http://{ADMIN_HOST}") as session:
        csrf = session["csrf_token"]
    blocked = client.post("/logout", headers={"Host": ADMIN_HOST})
    assert blocked.status_code == 400
    logged_out = client.post(
        "/logout",
        headers={"Host": ADMIN_HOST, "X-CSRF-Token": csrf},
    )
    assert logged_out.status_code == 302
    login_page = client.get("/", headers={"Host": ADMIN_HOST})
    assert b"Login Admin" in login_page.data


def test_fixture_admin_api_requires_admin_session(client):
    response = client.get("/fixtures-admin/api", headers={"Host": ADMIN_HOST})
    assert response.status_code == 401

def test_fixture_manual_sync_requires_csrf(client, monkeypatch):
    _login(client, monkeypatch)
    response = client.post("/fixtures-admin/sync", headers={"Host": ADMIN_HOST})
    assert response.status_code == 400

def test_fixture_admin_api_is_not_exposed_on_public_host(client):
    response = client.get(
        "/fixtures-admin/api",
        headers={"Host": "www.persibofansjogja.web.id"},
    )
    assert response.status_code == 404


def test_admin_login_is_rate_limited_after_five_failed_attempts(client, monkeypatch):
    from website.utils import security

    security._MEMORY_BUCKETS.clear()
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "pytest-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "pytest-password")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-only-secret-key-not-for-production")

    for _ in range(4):
        page = client.get("/", headers={"Host": ADMIN_HOST})
        response = client.post(
            "/", headers={"Host": ADMIN_HOST},
            data={"username": "pytest-admin", "password": "wrong-password",
                  "csrf_token": _csrf_from(page)},
        )
        assert response.status_code == 401

    page = client.get("/", headers={"Host": ADMIN_HOST})
    fifth = client.post(
        "/", headers={"Host": ADMIN_HOST},
        data={"username": "pytest-admin", "password": "wrong-password",
              "csrf_token": _csrf_from(page)},
    )
    assert fifth.status_code == 429
    assert int(fifth.headers["Retry-After"]) > 0
