from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources

import pytest
from fastapi.testclient import TestClient

from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.state import Broker
from argus_iris.server.app import create_app

CONFIG = {"nodes": {"morpheus": {"agent_url": "http://morpheus:9110"}}}


def test_dashboard_is_served() -> None:
    with TestClient(create_app(broker=Broker(BrokerConfig.model_validate(CONFIG)), poll=False)) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/ui/"
        page = client.get("/ui/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        # Everything the page references is packaged.
        for asset in ("app.js", "app.css", "vendor/dh/dh-tokens.css", "vendor/dh/dh-theme.js"):
            assert f'"{asset}"' in page.text
            assert client.get(f"/ui/{asset}").status_code == 200, asset
        # The API routes are not shadowed by the mount.
        assert client.get("/health").json()["mode"] == "observe-only"


def _js_function(src: str, start: str) -> str:
    """The statement in app.js that begins with *start*, up to its closing line."""
    i = src.index(start)
    end = src.index("\n}\n", i) + 3 if start.startswith("function") else src.index("\n", i) + 1
    return src[i:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js's colour code")
def test_no_named_service_shares_the_other_colour() -> None:
    src = (resources.files("argus_iris") / "ui" / "app.js").read_text()
    script = "\n".join(
        [
            _js_function(src, "const SLOTS"),
            "const colours = new Map();",
            _js_function(src, "function slotFor"),
            _js_function(src, "const colourVar"),
            "const out = [];",
            "for (let i = 1; i <= 10; i++) { const s = slotFor('n', 's' + i); out.push([s, colourVar(s)]); }",
            "out.push([0, colourVar(0)]);",
            "console.log(JSON.stringify(out));",
        ]
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    other = out.pop()[1]
    named = [colour for slot, colour in out if slot]
    assert len(named) >= 7 and len(set(named)) == len(named)  # every named service has a colour of its own
    assert other not in named  # and none looks like the folded "other" services
