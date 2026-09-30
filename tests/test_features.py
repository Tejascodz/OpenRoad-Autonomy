"""Map status, place search, simulation speed, fleet state and map download behaviour."""

import pytest

from app.services import geocode
from conftest import login, reset_fleet

TRIP = {"pickup_lat": 12.962, "pickup_lon": 77.585, "delivery_lat": 12.978, "delivery_lon": 77.600}


def test_map_status_and_geofence(client):
    login(client, "viewer")
    r = client.get("/api/v1/map/status")
    assert r.status_code == 200
    body = r.json()
    assert body["offline"] is True and "state" in body and body["geofence"]["radius_km"] > 0


def test_geocode_requires_operator_and_validates(client):
    login(client, "viewer")
    assert client.get("/api/v1/geocode?q=majestic").status_code == 403
    login(client, "operator")
    assert client.get("/api/v1/geocode?q=ab").status_code == 422
    assert client.get("/api/v1/geocode?q=" + "x" * 200).status_code == 422
    assert client.get("/api/v1/geocode?q=majestic").json() == []  # offline mode: no external calls


def test_geocode_filters_to_geofence_and_sanitises(monkeypatch):
    calls = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"lat": "12.9763", "lon": "77.5712", "display_name": "Majestic, Bengaluru"},
                    {"lat": "40.7", "lon": "-74.0", "display_name": "New York"},       # outside geofence
                    {"lat": "bad", "lon": "x", "display_name": "broken"}]

    def fake_get(url, params, headers, timeout):
        calls.update(params=params, headers=headers)
        return Resp()

    monkeypatch.setattr(geocode.requests, "get", fake_get)
    monkeypatch.setattr(geocode, "_last_call", 0.0)
    geocode._cache.clear()
    out = geocode.search("Maje\x00stic\n  station")
    assert [o["short"] for o in out] == ["Majestic"]
    assert calls["params"]["bounded"] == 1 and "\x00" not in calls["params"]["q"]
    assert "OpenRoad" in calls["headers"]["User-Agent"]
    assert geocode.search("Maje\x00stic\n  station") == out  # cached: no second call needed


def test_sim_speed_bounds_and_roles(client):
    csrf = login(client, "viewer")
    assert client.post("/api/v1/fleet/sim_speed", json={"scale": 5}, headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    assert client.post("/api/v1/fleet/sim_speed", json={"scale": 100}, headers=h).status_code == 422
    assert client.post("/api/v1/fleet/sim_speed", json={"scale": 10}, headers=h).json()["time_scale"] == 10
    st = client.get("/api/v1/fleet").json()
    assert st["time_scale"] == 10 and st["simulated"] is True
    assert any("Simulation speed" in e["message"] for e in st["events"])


def test_fleet_state_has_eta_and_only_simulated_fields(client):
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    reset_fleet(client, csrf)
    r = client.post("/api/v1/start_delivery", json=TRIP, headers=h)
    assert r.status_code == 200, r.text
    rid = r.json()["robot_id"]
    st = client.get("/api/v1/fleet").json()
    robot = next(x for x in st["robots"] if x["id"] == rid)
    assert robot["eta_s"] > 0 and robot["remaining_m"] > 0 and robot["delivery_id"] == r.json()["delivery_id"]
    assert set(robot) == {  # exactly the values the simulation computes, nothing else
        "id", "name", "mode", "stopped", "lat", "lon", "speed_kmh", "battery_pct", "range_km", "delivery_id",
        "leg", "leg_progress", "route_point", "remaining_m", "eta_s", "route_version", "home", "at_base",
        "odometer_km", "error"}
    assert any("assigned to" in e["message"] for e in st["events"])
    reset_fleet(client, csrf)


def test_validation_message_is_clean(client):
    csrf = login(client, "operator")
    far = {**TRIP, "delivery_lat": 13.2}
    r = client.post("/api/v1/plan", json=far, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422 and not r.json()["detail"][0]["msg"].startswith("Value error")


def test_offline_toggle_requires_operator_and_works(client):
    csrf = login(client, "viewer")
    assert client.post("/api/v1/map/offline", json={"enabled": True}, headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    reset_fleet(client, csrf)
    r = client.post("/api/v1/map/offline", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "offline"


def test_overpass_fallback_tries_next_server(monkeypatch):
    from app.config import settings
    from app.services import map_service as ms

    tried = []
    monkeypatch.setattr(ms, "probe_overpass", lambda base, timeout=None: (base.endswith("b"), "x"))

    class FakeOx:
        class settings:
            overpass_url = ""

        @staticmethod
        def graph_from_point(*a, **k):
            tried.append(FakeOx.settings.overpass_url)
            raise ConnectionError("down")

    monkeypatch.setattr(settings, "OVERPASS_URLS", ["https://a", "https://b"])
    svc = ms.MapService.__new__(ms.MapService)
    svc._status = {}
    svc._ox = FakeOx
    with pytest.raises(ms.MapDataUnavailable):
        svc._download((12.97, 77.59), 1000)
    assert tried == ["https://b"]  # unreachable server skipped, reachable one used
    assert svc._status["state"] == "error" and "offline demo map" in svc._status["message"]


def test_graph_disk_cache_roundtrip(tmp_path):
    from app.services.map_service import load_graph, save_graph
    from app.services.planning import RoutePlanner, VehicleLimits
    from app.services.planning.graph import synthetic_grid_graph

    G = synthetic_grid_graph(12.97, 77.59, size=9, spacing_m=100)
    path = tmp_path / "g.json.gz"
    save_graph(G, path)
    H = load_graph(path)
    assert H.number_of_nodes() == G.number_of_nodes() and H.number_of_edges() == G.number_of_edges()
    u, v = next(iter(G.edges))
    assert H[u][v]["speed_kmh"] == G[u][v]["speed_kmh"] and len(H[u][v]["geometry"]) == len(G[u][v]["geometry"])
    s, _ = RoutePlanner(H, VehicleLimits()).snap(12.965, 77.585)
    t, _ = RoutePlanner(H, VehicleLimits()).snap(12.975, 77.595)
    assert RoutePlanner(H, VehicleLimits()).plan(s, t, "alt", "fastest").distance_m > 0


def test_busy_servers_are_retried_then_succeed(monkeypatch):
    from app.config import settings
    from app.services import map_service as ms
    from app.services.planning.graph import synthetic_grid_graph

    calls = {"probe": 0, "sleep": []}

    def probe(base, timeout=None):
        calls["probe"] += 1
        return calls["probe"] > 2, "HTTP 504"   # busy for the whole first round

    grid = synthetic_grid_graph(12.97, 77.59, size=7, spacing_m=100)

    class FakeOx:
        class settings:
            overpass_url = ""

        @staticmethod
        def graph_from_point(*a, **k):
            import networkx as nx
            M = nx.MultiDiGraph()
            for n, d in grid.nodes(data=True):
                M.add_node(n, y=d["y"], x=d["x"])
            for u, v, d in grid.edges(data=True):
                M.add_edge(u, v, length=d["length"], highway=d["highway"])
            return M

    monkeypatch.setattr(ms, "probe_overpass", probe)
    monkeypatch.setattr(ms.time, "sleep", lambda s: calls["sleep"].append(s))
    monkeypatch.setattr(settings, "OVERPASS_URLS", ["https://a", "https://b"])
    monkeypatch.setattr(settings, "OFFLINE_MAPS", False)
    svc = ms.MapService.__new__(ms.MapService)
    svc._status = {}
    svc._ox = FakeOx
    region = svc._download((12.97, 77.59), 500, rounds=3, background=True)
    assert region.planner.G.number_of_nodes() == grid.number_of_nodes()
    assert calls["sleep"] == [ms.RETRY_BACKOFF_S[0]] and svc._status["state"] == "ready"


def test_requests_fail_fast_while_warmup_downloads(monkeypatch):
    import threading

    from app.config import settings
    from app.services import map_service as ms

    monkeypatch.setattr(settings, "OFFLINE_MAPS", False)
    svc = ms.MapService.__new__(ms.MapService)
    svc._status, svc._ox, svc._regions, svc._lock = {}, object(), {}, threading.Lock()
    stop = threading.Event()
    svc._warm_thread = threading.Thread(target=stop.wait, daemon=True)
    svc._warm_thread.start()
    try:
        c = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
        with pytest.raises(ms.MapDataUnavailable, match="still downloading"):
            svc.planner_for([c, (c[0] + 0.005, c[1])])
    finally:
        stop.set()


# ------------------------------------------------------------------ bounded Overpass client
class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Http:
    def __init__(self, cached=None):
        self.cached, self.saved = cached, []

    def _retrieve_from_cache(self, url):
        return self.cached

    def _save_to_cache(self, url, body, ok):
        self.saved.append(body)

    def _get_http_headers(self):
        return {}


class _OxSettings:
    overpass_url = "https://mirror.example/api"
    requests_timeout = 5


def test_overpass_busy_is_bounded_not_endless():
    from app.services.overpass_client import RETRY_WAITS_S, OverpassBusy, make_bounded_request
    calls, sleeps = [], []
    fn = make_bounded_request(_OxSettings, _Http(), sleep=sleeps.append,
                              post=lambda *a, **k: calls.append(1) or _Resp(504))
    with pytest.raises(OverpassBusy):
        fn({"data": "x"})
    assert len(calls) == len(RETRY_WAITS_S) + 1 and sleeps == list(RETRY_WAITS_S)


def test_overpass_incomplete_answer_not_cached_then_success():
    from app.services.overpass_client import make_bounded_request
    answers = [_Resp(200, {"remark": "runtime error: Query timed out in \"query\"", "elements": []}),
               _Resp(429), _Resp(200, {"elements": [{"type": "node"}]})]
    http = _Http()
    fn = make_bounded_request(_OxSettings, http, sleep=lambda s: None, post=lambda *a, **k: answers.pop(0))
    body = fn({"data": "x"})
    assert body["elements"] and http.saved == [body]  # only the complete answer is cached


def test_overpass_cache_hit_skips_network():
    from app.services.overpass_client import make_bounded_request
    fn = make_bounded_request(_OxSettings, _Http(cached={"elements": [1]}), sleep=lambda s: None,
                              post=lambda *a, **k: pytest.fail("network used"))
    assert fn({"data": "x"}) == {"elements": [1]}


def test_install_patches_osmnx_internal():
    import osmnx as ox
    from osmnx import _overpass

    from app.services.overpass_client import install
    install(ox)
    assert getattr(_overpass._overpass_request, "_openroad", False)
    assert ox.settings.overpass_rate_limit is False


def test_slot_wait_parsing(monkeypatch):
    from app.services import overpass_client as oc

    class R:
        status_code = 200
        text = "Rate limit: 2\n0 slots available now.\nSlot available after: 2026-10-01T00:00:10Z, in 12 seconds.\n"
    monkeypatch.setattr(oc.requests, "get", lambda *a, **k: R())
    assert oc.slot_wait_seconds("https://overpass-api.de/api") == 12
    R.text = "Rate limit: 2\n2 slots available now.\n"
    assert oc.slot_wait_seconds("https://overpass-api.de/api") == 0
