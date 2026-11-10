"""The small HTML test frontend served by this backend: login, Brand Seeder, add creators. Profile, matches and pitch are not served here at all."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import api.app as api_app

ROOT = Path(__file__).resolve().parent.parent


def test_only_the_three_pages_are_left():
    assert sorted(p.name for p in (ROOT / "frontend").glob("*.html")) == ["add-creators.html", "index.html", "login.html"]


def test_every_page_the_server_hands_out_exists():
    source = (ROOT / "api" / "app.py").read_text()
    for name in set(re.findall(r'FileResponse\("frontend/([\w.-]+)"', source)):
        assert (ROOT / "frontend" / name).is_file(), name


def test_the_creator_page_routes_are_gone_and_do_not_redirect_to_the_app():
    client = TestClient(api_app.app, follow_redirects=False)
    for path in ("/creator-profile", "/matches", "/pitch"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "location" not in response.headers, path


def test_the_headers_and_the_login_page_no_longer_point_to_those_pages():
    for name in ("index.html", "add-creators.html", "login.html"):
        html = (ROOT / "frontend" / name).read_text()
        for gone in ('href="/creator-profile"', 'href="/matches"', 'href="/pitch"', "'/creator-profile'", "app.mavlit.com"):
            assert gone not in html, (name, gone)
    assert 'href="/add-creators"' in (ROOT / "frontend" / "index.html").read_text()   # the tabs that remain are still there
    assert 'href="/"' in (ROOT / "frontend" / "add-creators.html").read_text()
