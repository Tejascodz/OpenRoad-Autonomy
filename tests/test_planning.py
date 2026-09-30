import math
import random

import networkx as nx
import numpy as np
import pytest

from app.services.planning import CostModel, NoRouteError, RoutePlanner, VehicleLimits
from app.services.planning.dstar_lite import DStarLite
from app.services.planning.graph import index_nodes, parse_maxspeed_kmh, synthetic_grid_graph
from app.services.planning.search import Landmarks, astar, haversine_heuristic


@pytest.fixture(scope="module")
def graph():
    G = synthetic_grid_graph(12.97, 77.59, size=25, spacing_m=110)
    rng = random.Random(42)
    for e in rng.sample(list(G.edges), 150):  # make it irregular
        G.remove_edge(*e)
    index_nodes(G)
    return G


def _ref(G, s, t, model):
    return nx.dijkstra_path_length(G, s, t, weight=lambda u, v, d: None if model(u, v, d) == math.inf else model(u, v, d))


@pytest.mark.parametrize("profile", ["shortest", "fastest", "energy", "safest"])
def test_astar_and_alt_are_optimal(graph, profile):
    rng = random.Random(profile)
    m = CostModel(profile)
    lm = Landmarks(graph, m, k=6)
    nodes = list(graph.nodes)
    checked = 0
    while checked < 40:
        s, t = rng.sample(nodes, 2)
        try:
            ref = _ref(graph, s, t, m)
        except nx.NetworkXNoPath:
            continue
        for h in (None, haversine_heuristic(graph, t, m), lm.heuristic(t)):
            r = astar(graph, s, t, m, h)
            assert r.path[0] == s and r.path[-1] == t
            assert r.cost == pytest.approx(ref, rel=1e-9)
        checked += 1


def test_alt_expands_fewer_nodes_than_dijkstra(graph):
    m = CostModel("fastest")
    lm = Landmarks(graph, m, k=8)
    rng = random.Random(1)
    nodes = list(graph.nodes)
    dij = alt = 0
    for _ in range(30):
        s, t = rng.sample(nodes, 2)
        try:
            dij += astar(graph, s, t, m).expanded
            alt += astar(graph, s, t, m, lm.heuristic(t)).expanded
        except NoRouteError:
            pass
    assert alt < dij * 0.6


def test_dstar_lite_matches_dijkstra_through_repeated_replans(graph):
    rng = random.Random(7)
    nodes = list(graph.nodes)
    k = CostModel("shortest").lower_bound_per_m * 0.999
    lat = {n: (graph.nodes[n]["y"], graph.nodes[n]["x"]) for n in nodes}
    from app.services.planning.geo import haversine
    h = lambda a, b: haversine(lat[a], lat[b]) * k  # noqa: E731
    for _ in range(10):
        s, t = rng.sample(nodes, 2)
        model = CostModel("shortest")
        try:
            _ref(graph, s, t, model)
        except nx.NetworkXNoPath:
            continue
        ds = DStarLite(graph, s, t, model, h)
        path, _, _ = ds.plan()
        pos = s
        for _step in range(5):
            if pos == t:
                break
            path = ds.path()
            pos = path[min(2, len(path) - 1)]
            ds.move_to(pos)
            rest = ds.path() if pos != t else [t]
            changed = list(zip(rest, rest[1:]))[:1] + rng.sample(list(graph.edges), 5)
            model.blocked.update(changed)
            ds.notify_edge_changes(changed)
            try:
                ref = _ref(graph, pos, t, model)
            except nx.NetworkXNoPath:
                with pytest.raises(NoRouteError):
                    ds.path()
                break
            p2 = ds.path()
            cost = sum(model(u, v, graph[u][v]) for u, v in zip(p2, p2[1:]))
            assert p2[0] == pos and p2[-1] == t
            assert cost == pytest.approx(ref, rel=1e-9)
            assert ds.path_cost() == pytest.approx(ref, rel=1e-9)


def test_alternatives_are_distinct_and_bounded(graph):
    P = RoutePlanner(graph, VehicleLimits())
    s, _ = P.snap(12.955, 77.575)
    t, _ = P.snap(12.985, 77.605)
    plan = P.plan(s, t, "alt", "shortest", alternatives=2)
    assert plan.alternatives
    for alt in plan.alternatives:
        assert 1.0 <= alt["cost_ratio"] <= 1.35 + 1e-9


def test_velocity_profile_respects_limits(graph):
    L = VehicleLimits(a_lat=1.5, a_acc=1.0, a_dec=2.0)
    P = RoutePlanner(graph, L)
    s, _ = P.snap(12.955, 77.575)
    t, _ = P.snap(12.985, 77.605)
    traj = P.plan(s, t, "astar", "fastest").trajectory
    v, k, lim, sarr = traj.v_ref, traj.curvature, traj.v_limit, traj.s
    assert v[0] == 0 and v[-1] == 0
    assert np.all(v <= lim + 1e-9)
    curved = k > 1e-6
    assert np.all(v[curved] <= np.sqrt(L.a_lat / k[curved]) + 1e-9)
    ds = np.diff(sarr)
    assert np.all(v[1:] ** 2 - v[:-1] ** 2 <= 2 * L.a_acc * ds + 1e-6)
    assert np.all(v[:-1] ** 2 - v[1:] ** 2 <= 2 * L.a_dec * ds + 1e-6)


def test_maxspeed_parsing():
    assert parse_maxspeed_kmh("50") == 50
    assert parse_maxspeed_kmh("30 mph") == pytest.approx(48.28, abs=0.01)
    assert parse_maxspeed_kmh(["40", "60"]) == 40
    assert parse_maxspeed_kmh("walk") == 6
    assert parse_maxspeed_kmh("IN:urban") is None
    assert parse_maxspeed_kmh(None) is None


def test_build_planning_graph_from_osmnx_style_multidigraph():
    from shapely.geometry import LineString

    from app.services.planning.graph import build_planning_graph

    G = nx.MultiDiGraph()
    G.add_node(1, y=12.0, x=77.0)
    G.add_node(2, y=12.0, x=77.01)
    G.add_node(3, y=12.01, x=77.01)
    curve = LineString([(77.0, 12.0), (77.005, 12.002), (77.01, 12.0)])  # (x=lon, y=lat) like OSMnx
    G.add_edge(1, 2, 0, length=1150.0, highway=["primary", "secondary"], maxspeed="40", geometry=curve)
    G.add_edge(1, 2, 1, length=1090.0, highway="residential")                      # shorter parallel edge wins
    G.add_edge(2, 1, 0, length=1150.0, highway="primary", maxspeed="30 mph",
               geometry=LineString(list(curve.coords)))                            # geometry stored u->v reversed
    G.add_edge(2, 3, 0, length=1100.0, highway="service")
    G.add_edge(3, 3, 0, length=10.0, highway="service")                            # self-loop dropped
    D = build_planning_graph(G, robot_max_kmh=25.0)
    assert not D.has_edge(3, 3)
    assert D[1][2]["length"] == 1090.0 and D[1][2]["highway"] == "residential"
    assert D[2][1]["speed_kmh"] == 25.0            # 30 mph capped at robot max
    geom = D[2][1]["geometry"]
    assert geom[0] == (12.0, 77.01) and geom[-1] == (12.0, 77.0)  # oriented u -> v
    assert D[2][3]["speed_kmh"] == 8.0
    P = RoutePlanner(D, VehicleLimits())
    plan = P.plan(1, 3, "alt", "fastest")
    assert plan.nodes == [1, 2, 3]
