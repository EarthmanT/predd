"""Tests for app.py — only basic smoke tests, coverage is incomplete (see DEMO-12)."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_create_item(client):
    resp = client.post("/items", json={"name": "widget"})
    assert resp.status_code == 201
    assert resp.get_json()["name"] == "widget"


def test_get_item(client):
    client.post("/items", json={"name": "gadget"})
    resp = client.get("/items/1")
    assert resp.status_code == 200
