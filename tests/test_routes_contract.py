"""Route + panel-HTML contract tests.

These freeze the *external surface* of the app so the refactor (which splits
control_panel.py and extracts the embedded panel frontend to a file) cannot
change any route or a single byte of the panel HTML.

`tests/baseline/` was captured from the pre-refactor server.
"""
import json
import os

from fastapi.testclient import TestClient

import backend.main as main

_BASELINE = os.path.join(os.path.dirname(__file__), "baseline")


def test_route_surface_unchanged():
    spec = main.app.openapi()
    current = sorted(
        f"{m.upper()} {p}"
        for p, item in spec["paths"].items()
        for m in item.keys()
    )
    expected = json.load(open(os.path.join(_BASELINE, "routes.json"), encoding="utf-8"))
    assert current == expected


def test_panel_html_byte_identical():
    client = TestClient(main.app)
    body = client.get("/panel").content
    expected = open(os.path.join(_BASELINE, "panel.html"), "rb").read()
    assert body == expected
