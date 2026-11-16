from __future__ import annotations

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
