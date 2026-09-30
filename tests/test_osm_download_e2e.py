"""End-to-end: OSMnx graph download through the bounded client against a local fake Overpass server
that first answers 'busy' (504), then an incomplete answer, then real data."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

CENTER = (12.9716, 77.5946)


def _osm_json():
    nodes, ways, nid, wid = [], [], 1, 1
    step = 0.002
    ids = {}
    for i in range(-3, 4):
        for j in range(-3, 4):
            ids[(i, j)] = nid
            nodes.append({"type": "node", "id": nid, "lat": CENTER[0] + i * step, "lon": CENTER[1] + j * step})
            nid += 1
    for i in range(-3, 4):
        ways.append({"type": "way", "id": wid, "nodes": [ids[(i, j)] for j in range(-3, 4)],
                     "tags": {"highway": "residential", "name": f"Row {i}"}}); wid += 1
        ways.append({"type": "way", "id": wid, "nodes": [ids[(j, i)] for j in range(-3, 4)],
                     "tags": {"highway": "primary" if i == 0 else "residential", "maxspeed": "40"}}); wid += 1
    return {"version": 0.6, "elements": nodes + ways}


class FakeOverpass(BaseHTTPRequestHandler):
    script = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # probe + /status
        if self.path.endswith("/status"):
            return self._send(200, b"Connected as: 1\nRate limit: 2\n2 slots available now.\n")
        return self._send(200, {"elements": []})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        code, body = FakeOverpass.script.pop(0) if FakeOverpass.script else (200, _osm_json())
        self._send(code, body if body is not None else b"<html>busy</html>")


@pytest.fixture
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), FakeOverpass)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api"
    srv.shutdown()
    srv.server_close()


def test_real_osmnx_pipeline_survives_busy_server(fake_server, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import map_service as ms
    from app.services import overpass_client

    monkeypatch.setattr(settings, "OFFLINE_MAPS", False)
    monkeypatch.setattr(settings, "OVERPASS_URLS", [fake_server])
    monkeypatch.setattr(settings, "OSM_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(overpass_client, "RETRY_WAITS_S", (0, 0))
    FakeOverpass.script = [(504, None), (200, {"remark": "runtime error: Query timed out", "elements": []})]
    svc = ms.MapService()
    region = svc._download(CENTER, 800)
    G = region.planner.G
    assert G.number_of_nodes() > 10 and svc.status()["state"] == "ready"
    s, _ = region.planner.snap(CENTER[0] - 0.005, CENTER[1] - 0.005)
    t, _ = region.planner.snap(CENTER[0] + 0.005, CENTER[1] + 0.005)
    assert region.planner.plan(s, t, "alt", "fastest").distance_m > 1000
    # second download of the same area is served from the on-disk cache (no server answers left)
    FakeOverpass.script = [(504, None)] * 10
    svc2 = ms.MapService()
    assert svc2._download(CENTER, 800).planner.G.number_of_nodes() == G.number_of_nodes()
