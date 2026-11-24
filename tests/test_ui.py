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
        for asset in (
            "app.js",
            "app.css",
            "vendor/dh/dh-tokens.css",
            "vendor/dh/dh-theme.js",
            "brand/icon.svg",
            "brand/favicon.ico",
            "brand/apple-icon.png",
            "brand/argus-iris-lockup-light.svg",
            "brand/argus-iris-lockup-dark.svg",
        ):
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


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js's event stream code")
def test_a_closed_event_stream_is_reopened_with_backoff() -> None:
    src = (resources.files("argus_iris") / "ui" / "app.js").read_text()
    script = "\n".join(
        [
            "const api = (p) => p;",
            "const streams = [], timers = [], labels = [];",
            "class EventSource {",
            "  static CLOSED = 2;",
            "  constructor(url) { this.url = url; this.readyState = 0; this.on = {}; this.closed = false; streams.push(this); }",
            "  addEventListener(n, fn) { (this.on[n] ??= []).push(fn); }",
            "  fire(n) { for (const fn of this.on[n] ?? []) fn({ data: '{}' }); }",
            "  close() { this.closed = true; this.readyState = 2; }",
            "}",
            "const setTimeout = (fn, ms) => timers.push([fn, ms]);",
            _js_function(src, "function connectEvents"),
            "const setLive = (state, text) => labels.push(state + ' ' + text);",
            "const seen = [];",
            "connectEvents(setLive, { node: () => seen.push(streams.length) });",
            "streams[0].fire('error');",  # the browser is retrying by itself
            "const out = { retrying: [streams.length, timers.length] };",
            "const drop = (es) => { es.readyState = 2; es.fire('error'); timers.shift()[0](); };",
            "drop(streams[0]); drop(streams[1]);",  # given up twice: reopened each time, waiting longer
            "streams[2].fire('open'); drop(streams[2]);",  # an open resets the backoff
            "streams[3].fire('node');",
            "Object.assign(out, { streams: streams.length, closed: streams.map((s) => s.closed),",
            "  urls: [...new Set(streams.map((s) => s.url))], labels, seen });",
            "console.log(JSON.stringify(out));",
        ]
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == {
        "retrying": [1, 0],
        "streams": 4,
        "closed": [True, True, True, False],
        "urls": ["events"],
        "labels": [
            "warn reconnecting",
            "bad disconnected: retrying in 1s",
            "bad disconnected: retrying in 2s",
            "bad disconnected: retrying in 1s",
        ],
        "seen": [4],  # the reopened stream has the page's handlers
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js's registry reads")
def test_a_stale_registry_read_neither_drops_a_new_node_nor_overwrites_newer_data() -> None:
    src = (resources.files("argus_iris") / "ui" / "app.js").read_text()
    body = src[src.index("let registryTimer") : src.index("function scheduleRegistry")]
    handler = src[src.index("    node: (e) => {") : src.index("  });\n}\nboot();")]
    script = "\n".join(
        [
            "const api = (p) => p, pending = [], $ = () => null, fill = () => {}, noRows = () => {};",
            "let services = null, registry = null, directory = null;",
            "const nodes = new Map(), order = [], document = { getElementById: () => null };",
            "const renderRegistry = () => {}, renderSuite = renderRegistry, renderAgents = renderRegistry;",
            "const renderNode = renderRegistry, renderSummary = renderRegistry, scheduleRegistry = () => {};",
            "const fetch = (p) => new Promise((ok) => pending.push((names) => ok({ ok: true,",
            "  json: async () => (p === 'registry' ? { nodes: names.map((name) => ({ name })) } : names) })));",
            body,
            "const handlers = {",
            handler,
            "};",
            "const event = (name) => handlers.node({ data: JSON.stringify({ name, services: [] }) });",
            "const answer = (fs, names) => fs.forEach((f) => f(names));",
            "(async () => {",
            "  event('a'); event('gone');",
            "  const first = loadRegistry(); event('new');",  # announced after the read began
            "  answer(pending.splice(0), ['a']); await first;",
            "  const out = { first: [...nodes.keys()], order: [...order] };",
            "  const older = loadRegistry(), o = pending.splice(0);",
            "  const newer = loadRegistry(), n = pending.splice(0);",
            "  answer(n, ['a', 'new']); await newer; answer(o, ['a']); await older;",
            "  out.last = registry.nodes.map((x) => x.name); out.services = services; out.nodes = [...nodes.keys()];",
            "  console.log(JSON.stringify(out));",
            "})();",
        ]
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["first"] == ["a", "new"] and out["order"] == ["a", "new"]  # "gone" is still pruned
    assert out["last"] == out["services"] == out["nodes"] == ["a", "new"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js's snippets")
@pytest.mark.parametrize(
    ("page", "broker"),
    [
        ("http://192.168.1.39:8120/ui/?x=1#connect", "http://192.168.1.39:8120"),
        ("https://lan.example/iris/ui/", "https://lan.example/iris"),
    ],
)
def test_connect_snippets_keep_the_brokers_path_prefix(page: str, broker: str) -> None:
    src = (resources.files("argus_iris") / "ui" / "app.js").read_text()
    script = "\n".join(
        [
            f"const location = {{ href: {json.dumps(page)} }};",
            _js_function(src, "const api ="),
            "const els = {}, $ = (s) => (els[s] ??= {});",
            _js_function(src, "function renderSnippets"),
            "renderSnippets();",
            "console.log(JSON.stringify(Object.values(els).map((e) => e.textContent)));",
        ]
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    env, agent = json.loads(res.stdout)
    assert f"ARGUS_GPU_BROKER_URL={broker}" in env.splitlines()
    assert f"IRIS_BROKER_URL={broker}" in agent.splitlines()
