'use strict';
/* global L, Api */
// OpenRoad Fleet Control.
// XSS policy: server data is only written with textContent / createElement (never parsed as
// HTML) and the CSP forbids inline scripts and styles.
// Every number shown comes from the server. The fleet is simulated (no physical robot),
// which the top bar says.

const $ = (id) => document.getElementById(id);
const COLORS = ['#22d3ee', '#a78bfa', '#f59e0b', '#34d399', '#f472b6', '#60a5fa', '#facc15', '#fb7185', '#4ade80', '#c084fc'];
const MODE_TEXT = {
    idle: 'Idle', to_pickup: 'To pickup', loading: 'Loading', to_dropoff: 'Delivering',
    unloading: 'Unloading', returning: 'Returning', charging: 'Charging',
};
const LEG_TEXT = { to_pickup: 'to pickup', to_dropoff: 'to destination', to_base: 'to base' };

const S = {
    me: null, map: null, dark: true, follow: false, pick: null,
    places: { pickup: null, dropoff: null }, markers: {},
    algo: 'alt', profile: 'fastest', preview: null, previewLines: [],
    fleet: null, selected: null, bots: {}, area: null, areaCircle: null, historyLayer: null,
    lastEventId: 0, mapState: 'idle', busy: false, hintSticky: false, mapTimer: null, switchedOffline: false,
};

// ------------------------------------------------------------------ small helpers
function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
}
const fmt = (v, d = 0) => (Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—');
const fmtKm = (m) => (Number(m) >= 1000 ? `${fmt(m / 1000, 2)} km` : `${fmt(m, 0)} m`);
function fmtDuration(sec) {
    sec = Number(sec);
    if (!Number.isFinite(sec) || sec < 0) return '—';
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : m > 0 ? `${m} min ${s}s` : `${s}s`;
}
const numOf = (rid) => parseInt(String(rid).replace(/\D/g, ''), 10) || 0;
const colorOf = (rid) => COLORS[(numOf(rid) - 1 + COLORS.length) % COLORS.length];
const safeClass = (v) => String(v || '').replace(/[^a-z_]/g, '');
const robotById = (rid) => (S.fleet ? S.fleet.robots.find((r) => r.id === rid) : null);
const isOperator = () => S.me && S.me.role !== 'viewer';

function toast(msg, type = 'info', ms = 4000) {
    const t = el('div', msg, 'toast ' + (['success', 'warning', 'error', 'critical', 'info'].includes(type) ? type : 'info'));
    $('toasts').appendChild(t);
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 260); }, ms);
    while ($('toasts').children.length > 5) $('toasts').firstChild.remove();
}
function setHint(text, kind, sticky) {
    S.hintSticky = !!sticky;
    const h = $('planner-hint');
    h.replaceChildren();
    h.className = 'hint' + (kind === 'error' ? ' error' : '');
    if (kind === 'busy') h.appendChild(el('span', null, 'spinner'));
    if (text) h.appendChild(el('span', text));
}
function busyButton(btn, busy, label) {
    if (busy) {
        btn.dataset.label = btn.textContent;
        btn.replaceChildren(el('span', null, 'spinner'), el('span', label || 'Working…'));
        btn.disabled = true;
    } else {
        btn.textContent = btn.dataset.label || btn.textContent;
        btn.disabled = false;
    }
}
function setPill(id, textId, cls, text) {
    $(id).className = 'pill ' + (cls || '');
    $(textId).textContent = text;
}
function store(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } }
function load(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }

// ------------------------------------------------------------------ map
function pinIcon(kind) {
    const p = el('div', null, 'pin ' + kind);
    p.appendChild(el('span', kind === 'pickup' ? 'A' : 'B'));
    return L.divIcon({ className: '', html: p, iconSize: [30, 30], iconAnchor: [15, 30] });
}

function botIcon(r, selected) {
    const d = el('div', String(numOf(r.id)), 'bot-marker' + (r.stopped ? ' stopped' : '') + (selected ? ' selected' : ''));
    d.style.background = colorOf(r.id);
    return L.divIcon({ className: '', html: d, iconSize: [30, 30], iconAnchor: [15, 15] });
}

function baseIcon(rid) {
    const d = el('div', null, 'base-marker');
    d.style.background = colorOf(rid);
    return L.divIcon({ className: '', html: d, iconSize: [12, 12], iconAnchor: [6, 6] });
}

function initMap(center) {
    S.map = L.map('map', { center, zoom: 14, zoomControl: true });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors', maxZoom: 19,
    }).addTo(S.map);
    S.dark = load('or-theme') !== 'light';
    document.body.classList.toggle('map-dark', S.dark);
    S.historyLayer = L.layerGroup().addTo(S.map);
    S.map.on('dragstart', () => setFollow(false));
    S.map.on('click', (e) => {
        if (!S.pick) return;
        setPlace(S.pick, e.latlng.lat, e.latlng.lng, `Pin ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`);
        setPickMode(null);
    });
}

function drawArea(area) {
    const key = area ? `${area.lat},${area.lon},${area.radius_m}` : '';
    if (key === (S.area || '')) return;
    S.area = key;
    if (S.areaCircle) { S.map.removeLayer(S.areaCircle); S.areaCircle = null; }
    if (!area) return;
    S.areaCircle = L.circle([area.lat, area.lon], {
        radius: area.radius_m, color: '#818cf8', weight: 1.5, opacity: 0.6, fill: true, fillOpacity: 0.03,
        dashArray: '6 8', interactive: false,
    }).addTo(S.map);
    S.areaCircle.bindTooltip(`Operating area · ${fmt(area.radius_m / 1000, 1)} km radius (loaded road map)`, { sticky: true });
}

function fitTo(coords) {
    if (coords && coords.length > 1) S.map.fitBounds(L.latLngBounds(coords), { paddingTopLeft: [390, 40], paddingBottomRight: [390, 60], maxZoom: 17 });
}

function setFollow(on) {
    S.follow = !!on;
    $('btn-follow').classList.toggle('primary', S.follow);
    if (S.follow) {
        const r = robotById(S.selected);
        if (r) S.map.panTo([r.lat, r.lon]);
        else toast('Select a robot to follow', 'info');
    }
}

// ------------------------------------------------------------------ places / search
function setPlace(kind, lat, lon, label) {
    S.places[kind] = { lat: Number(lat), lon: Number(lon), label };
    $(`${kind}-input`).value = label;
    if (S.markers[kind]) S.map.removeLayer(S.markers[kind]);
    const m = L.marker([lat, lon], { icon: pinIcon(kind), draggable: true, zIndexOffset: 800 }).addTo(S.map);
    m.bindTooltip(kind === 'pickup' ? 'Pickup' : 'Destination', { direction: 'top', offset: [0, -28] });
    m.on('dragend', () => {
        const p = m.getLatLng();
        setPlace(kind, p.lat, p.lng, `Pin ${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}`);
    });
    S.markers[kind] = m;
    hideSuggest(kind);
    clearPreview();
    S.hintSticky = false;
    updatePlanner();
}

function clearPlace(kind) {
    if (S.markers[kind]) S.map.removeLayer(S.markers[kind]);
    S.markers[kind] = null;
    S.places[kind] = null;
    $(`${kind}-input`).value = '';
}

function setPickMode(kind) {
    S.pick = kind;
    document.body.classList.toggle('picking', !!kind);
    ['pickup', 'dropoff'].forEach((k) => $(`${k}-pick`).classList.toggle('primary', k === kind));
    if (kind) setHint(`Click on the map (inside the dashed circle) to set the ${kind === 'pickup' ? 'pickup' : 'destination'}`);
    else if (!S.busy) { S.hintSticky = false; updatePlanner(); }
}

function parseCoords(text) {
    const m = String(text).match(/^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/);
    if (!m) return null;
    const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
    return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : null;
}

function hideSuggest(kind) { $(`${kind}-suggest`).classList.add('hidden'); }

function showSuggest(kind, items, emptyText) {
    const box = $(`${kind}-suggest`);
    box.replaceChildren();
    if (!items.length) box.appendChild(el('div', emptyText || 'No places found inside the operating area', 's-empty'));
    items.forEach((it, i) => {
        const b = el('button', null, i === 0 ? 'active' : '');
        b.type = 'button';
        b.appendChild(el('div', it.short, 's-title'));
        b.appendChild(el('div', it.name, 's-sub'));
        b.addEventListener('mousedown', (e) => { e.preventDefault(); setPlace(kind, it.lat, it.lon, it.short); });
        box.appendChild(b);
    });
    box._items = items;
    box.classList.remove('hidden');
}

function wireSearch(kind) {
    const input = $(`${kind}-input`);
    let timer = null, seq = 0;
    input.addEventListener('input', () => {
        clearTimeout(timer);
        const q = input.value.trim();
        if (parseCoords(q)) { showSuggest(kind, [], 'Press Enter to use these coordinates'); return; }
        if (q.length < 3) { hideSuggest(kind); return; }
        timer = setTimeout(async () => {
            const my = ++seq;
            showSuggest(kind, [], 'Searching…');
            try {
                const res = await Api.get(`/api/v1/geocode?q=${encodeURIComponent(q)}`);
                if (my === seq) showSuggest(kind, res);
            } catch (e) {
                if (my === seq) showSuggest(kind, [], e.message);
            }
        }, 450);
    });
    input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const c = parseCoords(input.value);
        if (c) { setPlace(kind, c[0], c[1], `${c[0].toFixed(5)}, ${c[1].toFixed(5)}`); return; }
        const items = $(`${kind}-suggest`)._items || [];
        if (items.length) setPlace(kind, items[0].lat, items[0].lon, items[0].short);
    });
    input.addEventListener('blur', () => setTimeout(() => hideSuggest(kind), 150));
    $(`${kind}-pick`).addEventListener('click', () => setPickMode(S.pick === kind ? null : kind));
}

function availableRobots() {
    return S.fleet ? S.fleet.robots.filter((r) => !r.stopped && !r.delivery_id && (r.mode === 'idle' || r.mode === 'charging')) : [];
}

function updatePlanner() {
    const ready = S.places.pickup && S.places.dropoff;
    $('preview-btn').disabled = !ready || S.busy;
    $('dispatch-btn').disabled = !ready || S.busy;
    if (S.busy || S.pick || S.hintSticky) return;
    const free = availableRobots().length;
    if (!S.places.pickup) setHint('Step 1 · search a pickup place, or click 📍 and tap the map');
    else if (!S.places.dropoff) setHint('Step 2 · choose the destination');
    else if (S.fleet && free === 0) setHint('All robots are busy or stopped — preview works; dispatch when one is free');
    else setHint(`Ready · ${free} robot${free === 1 ? '' : 's'} available — preview or dispatch`);
}

function planRequest(alternatives) {
    const req = {
        pickup_lat: S.places.pickup.lat, pickup_lon: S.places.pickup.lon,
        delivery_lat: S.places.dropoff.lat, delivery_lon: S.places.dropoff.lon,
        algorithm: S.algo, profile: S.profile, alternatives,
    };
    const rid = $('robot-select').value;
    if (/^R\d{3}$/.test(rid)) req.robot_id = rid;
    return req;
}

function planningHint() {
    if (S.mapState === 'downloading') return 'The road map is still downloading — this can take a few minutes the first time…';
    if (S.mapState === 'preparing') return 'Preparing the road network…';
    return 'Planning route…';
}

// ------------------------------------------------------------------ preview / dispatch
function clearPreview() {
    S.previewLines.forEach((l) => S.map.removeLayer(l));
    S.previewLines = [];
    S.preview = null;
    $('routes').replaceChildren();
}

function renderRoutes(plan) {
    clearPreview();
    S.preview = plan;
    const routes = [{ coords: plan.coords, distance_m: plan.distance_m, eta_min: plan.eta_min, energy_kwh: plan.energy_kwh },
        ...(plan.alternatives || []).map((a) => ({ ...a, eta_min: plan.eta_min * a.cost_ratio }))];
    const box = $('routes');
    const sug = plan.robot || {};
    const s = el('div', null, 'suggestion');
    if (sug.robot_id) {
        s.appendChild(el('span', 'Will be assigned to '));
        s.appendChild(el('strong', sug.robot_id));
        s.appendChild(el('span', sug.approach_m > 0
            ? ` — ${fmtKm(sug.approach_m)} (${fmt(sug.approach_min, 0)} min) from the pickup`
            : ' — already at the pickup'));
    } else {
        s.appendChild(el('span', `No robot can take it right now: ${sug.reason || 'all busy'}`));
    }
    box.appendChild(s);
    routes.forEach((r, i) => {
        const line = L.polyline(r.coords, {
            color: i === 0 ? '#e2e8f0' : '#818cf8', weight: i === 0 ? 6 : 4, opacity: i === 0 ? 0.9 : 0.55,
            dashArray: i === 0 ? null : '8 10',
        }).addTo(S.map);
        S.previewLines.push(line);
        const card = el('div', null, 'route' + (i === 0 ? ' active' : ''));
        const top = el('div', null, 'r-top');
        top.appendChild(el('span', `${fmt(r.eta_min, 0)} min`, 'r-eta num'));
        top.appendChild(el('span', i === 0 ? 'Best' : `Alt ${i}`, i === 0 ? 'tag' : 'tag alt'));
        card.appendChild(top);
        card.appendChild(el('div', `${fmtKm(r.distance_m)} · ${fmt(r.energy_kwh * 1000, 0)} Wh`, 'r-meta'));
        if (i === 0) card.appendChild(el('div', `${String(plan.algorithm).toUpperCase()} · ${plan.nodes_expanded} nodes searched · ${fmt(plan.compute_ms, 1)} ms`, 'r-meta'));
        card.addEventListener('click', () => {
            box.querySelectorAll('.route').forEach((c) => c.classList.remove('active'));
            card.classList.add('active');
            S.previewLines.forEach((l, j) => l.setStyle({ opacity: j === i ? 0.95 : 0.35, weight: j === i ? 6 : 4 }));
            line.bringToFront();
        });
        box.appendChild(card);
    });
    if (routes.length > 1) {
        box.appendChild(el('div', 'Dispatch always uses the best route; alternatives are shown for comparison.', 'r-meta'));
    }
    S.previewLines[0].bringToFront();
    fitTo(plan.coords);
}

async function previewRoutes() {
    if (!S.places.pickup || !S.places.dropoff) return;
    S.busy = true; updatePlanner(); kickMapPoll();
    busyButton($('preview-btn'), true, 'Planning…');
    setHint(planningHint(), 'busy');
    try {
        const plan = await Api.post('/api/v1/plan', planRequest(2));
        renderRoutes(plan);
        setHint(`Found ${1 + (plan.alternatives || []).length} route(s)`, null, true);
    } catch (e) {
        setHint(e.message, 'error', true);
        toast(e.message, 'error', 7000);
    } finally {
        S.busy = false; busyButton($('preview-btn'), false); updatePlanner(); kickMapPoll();
    }
}

async function dispatch() {
    if (!S.places.pickup || !S.places.dropoff) return;
    S.busy = true; updatePlanner(); kickMapPoll();
    busyButton($('dispatch-btn'), true, 'Dispatching…');
    setHint(planningHint(), 'busy');
    try {
        const r = await Api.post('/api/v1/start_delivery', planRequest(0));
        clearPreview();
        clearPlace('pickup'); clearPlace('dropoff');
        $('robot-select').value = '';
        toast(`Delivery #${r.delivery_id} → ${r.robot_id} · ${fmtKm(r.distance_m)} · done in ~${fmt(r.eta_min, 0)} min`, 'success', 6000);
        setHint(`${r.robot_id} is on its way. Set up the next delivery any time.`, null, true);
        selectRobot(r.robot_id, false);
        await pollFleet();
        loadHistory();
    } catch (e) {
        setHint(e.message, 'error', true);
        toast(e.message, 'error', 7000);
    } finally {
        S.busy = false; busyButton($('dispatch-btn'), false); updatePlanner(); kickMapPoll();
    }
}

// ------------------------------------------------------------------ fleet rendering
function botState(rid) {
    if (!S.bots[rid]) S.bots[rid] = { marker: null, base: null, lines: [], version: -1, legs: [], fetching: false, drawKey: '' };
    return S.bots[rid];
}

function removeBot(rid) {
    const b = S.bots[rid];
    if (!b) return;
    if (b.marker) S.map.removeLayer(b.marker);
    if (b.base) S.map.removeLayer(b.base);
    b.lines.forEach((l) => S.map.removeLayer(l));
    delete S.bots[rid];
}

async function syncRoute(r) {
    const b = botState(r.id);
    if (b.fetching) return;
    b.fetching = true;
    try {
        const route = await Api.get(`/api/v1/robots/${encodeURIComponent(r.id)}/route`);
        b.version = route.version;
        b.legs = route.legs || [];
        b.drawKey = '';
        drawRobotRoute(robotById(r.id) || r);
    } catch (_) { /* next update retries */ } finally { b.fetching = false; }
}

function drawRobotRoute(r) {
    const b = botState(r.id);
    const driving = ['to_pickup', 'to_dropoff', 'returning'].includes(r.mode);
    const cut = driving ? r.route_point : 0;
    const key = `${b.version}:${cut}:${S.selected === r.id}`;
    if (key === b.drawKey) return;
    b.drawKey = key;
    b.lines.forEach((l) => S.map.removeLayer(l));
    b.lines = [];
    const color = colorOf(r.id), sel = S.selected === r.id;
    b.legs.forEach((leg, i) => {
        let pts = leg.coords || [];
        if (i === 0 && driving) pts = [[r.lat, r.lon], ...pts.slice(Math.min(cut + 1, pts.length))];
        if (pts.length < 2) return;
        const current = i === 0;
        const line = L.polyline(pts, {
            color, weight: current ? (sel ? 6 : 4) : 3, opacity: current ? (sel ? 0.95 : 0.7) : 0.45,
            dashArray: current ? null : '6 9', interactive: false,
        }).addTo(S.map);
        b.lines.push(line);
    });
}

function robotSub(r) {
    if (r.stopped) return r.error ? `STOPPED · ${r.error}` : 'STOPPED';
    if (r.delivery_id) return `Delivery #${r.delivery_id} · ${MODE_TEXT[r.mode] || r.mode}`;
    if (r.mode === 'returning') return 'Returning to base';
    if (r.mode === 'charging') return 'Charging at base';
    return r.at_base ? 'Idle at base' : 'Idle';
}

function renderFleet(snap) {
    if (!snap || !Array.isArray(snap.robots)) return;
    S.fleet = snap;
    const robots = snap.robots;
    const busy = robots.filter((r) => r.delivery_id || ['to_pickup', 'to_dropoff', 'returning', 'loading', 'unloading'].includes(r.mode)).length;
    const stopped = robots.filter((r) => r.stopped).length;
    setPill('fleet-pill', 'fleet-text', stopped ? 'bad' : busy ? 'ok' : '',
        `${robots.length} robots · ${busy} active${stopped ? ` · ${stopped} stopped` : ''}`);
    $('estop').classList.toggle('latched', stopped > 0 && stopped === robots.length);
    $('release-all').classList.toggle('hidden', !(isOperator() && stopped > 0));
    document.querySelectorAll('#sim-speed button').forEach((btn) => btn.classList.toggle('active', Number(btn.dataset.v) === Number(snap.time_scale)));

    // robot <select>
    const sel = $('robot-select');
    const want = ['', ...robots.map((r) => r.id)].join(',');
    if (sel.dataset.ids !== want) {
        const keep = sel.value;
        sel.replaceChildren(Object.assign(el('option', 'Auto — nearest available robot'), { value: '' }));
        robots.forEach((r) => sel.appendChild(Object.assign(el('option', `${r.id} · ${r.name}`), { value: r.id })));
        sel.value = robots.some((r) => r.id === keep) ? keep : '';
        sel.dataset.ids = want;
    }
    Array.from(sel.options).forEach((o) => {
        const r = robots.find((x) => x.id === o.value);
        if (r) o.textContent = `${r.id} · ${r.name} — ${r.stopped ? 'stopped' : robotSub(r).toLowerCase()} · ${fmt(r.battery_pct, 0)}%`;
    });

    // list
    const list = $('fleet-list');
    list.replaceChildren();
    robots.forEach((r) => {
        const row = el('div', null, 'fleet-row' + (S.selected === r.id ? ' selected' : ''));
        const badge = el('div', String(numOf(r.id)), 'badge-num');
        badge.style.background = colorOf(r.id);
        const mid = el('div');
        mid.appendChild(el('div', `${r.id} · ${r.name}`, 'f-name'));
        mid.appendChild(el('div', robotSub(r), 'f-sub'));
        const right = el('div', null, 'f-right');
        right.appendChild(el('span', r.stopped ? 'stopped' : (MODE_TEXT[r.mode] || r.mode), 'status ' + (r.stopped ? 'stopped' : safeClass(r.mode))));
        const batt = el('div', null, 'batt' + (r.battery_pct < 20 ? ' low' : r.battery_pct < 45 ? ' mid' : ''));
        const fill = el('div');
        fill.style.width = `${Math.max(0, Math.min(100, Number(r.battery_pct) || 0))}%`;
        batt.appendChild(fill);
        batt.title = `Battery ${fmt(r.battery_pct, 0)}%`;
        right.appendChild(batt);
        row.append(badge, mid, right);
        row.addEventListener('click', () => selectRobot(r.id, true));
        list.appendChild(row);
    });

    // markers + routes
    const ids = new Set(robots.map((r) => r.id));
    Object.keys(S.bots).forEach((rid) => { if (!ids.has(rid)) removeBot(rid); });
    robots.forEach((r) => {
        const b = botState(r.id);
        const iconKey = `${r.stopped}:${S.selected === r.id}`;
        if (!b.marker) {
            b.marker = L.marker([r.lat, r.lon], { icon: botIcon(r, S.selected === r.id), zIndexOffset: 1000, keyboard: false }).addTo(S.map);
            b.marker.on('click', () => selectRobot(r.id, false));
            b.marker.bindTooltip(r.id, { direction: 'top', offset: [0, -14] });
            b.iconKey = iconKey;
        } else {
            b.marker.setLatLng([r.lat, r.lon]);
            if (b.iconKey !== iconKey) { b.marker.setIcon(botIcon(r, S.selected === r.id)); b.iconKey = iconKey; }
        }
        if (!b.base && Array.isArray(r.home)) {
            b.base = L.marker(r.home, { icon: baseIcon(r.id), keyboard: false, interactive: true }).addTo(S.map);
            b.base.bindTooltip(`Base of ${r.id}`);
        }
        if (r.route_version !== b.version) syncRoute(r);
        else drawRobotRoute(r);
    });

    if (S.selected && !ids.has(S.selected)) S.selected = null;
    renderSelected();
    if (S.follow) {
        const r = robotById(S.selected);
        if (r) S.map.panTo([r.lat, r.lon], { animate: true, duration: 0.6 });
    }
    handleEvents(snap.events || []);
    updatePlanner();
}

function selectRobot(rid, pan) {
    S.selected = rid;
    Object.values(S.bots).forEach((b) => { b.drawKey = ''; b.iconKey = ''; });
    if (S.fleet) renderFleet(S.fleet);
    const r = robotById(rid);
    if (r && pan) S.map.panTo([r.lat, r.lon]);
}

function renderSelected() {
    const r = robotById(S.selected);
    const btn = (id, show) => $(id).classList.toggle('hidden', !show);
    if (!r) {
        $('rc-title').textContent = 'Select a robot';
        $('rc-sub').textContent = 'Click a robot in the list or on the map';
        $('rc-status').textContent = '—';
        $('rc-status').className = 'status';
        $('rc-progress').style.width = '0%';
        ['rc-remain', 'rc-eta', 'rc-speed', 'rc-batt', 'rc-range', 'rc-odo'].forEach((id) => { $(id).textContent = '—'; });
        ['rc-stop', 'rc-resume', 'rc-cancel', 'rc-return'].forEach((id) => btn(id, false));
        return;
    }
    const op = isOperator();
    $('rc-title').textContent = `${r.id} · ${r.name}`;
    $('rc-sub').textContent = r.leg ? `${robotSub(r)} · driving ${LEG_TEXT[r.leg] || r.leg}` : robotSub(r);
    $('rc-status').textContent = r.stopped ? 'stopped' : (MODE_TEXT[r.mode] || r.mode);
    $('rc-status').className = 'status ' + (r.stopped ? 'stopped' : safeClass(r.mode));
    $('rc-progress').style.width = `${Math.max(0, Math.min(100, Number(r.leg_progress) || 0))}%`;
    const hasMission = r.remaining_m > 0 || r.delivery_id;
    $('rc-remain').textContent = hasMission ? `${fmtKm(r.remaining_m)} left` : '—';
    $('rc-eta').textContent = hasMission ? `done in ${fmtDuration(r.eta_s)}` : '—';
    $('rc-speed').textContent = `${fmt(r.speed_kmh, 1)} km/h`;
    $('rc-batt').textContent = `${fmt(r.battery_pct, 0)} %`;
    $('rc-range').textContent = `${fmt(r.range_km, 1)} km`;
    $('rc-odo').textContent = `${fmt(r.odometer_km, 2)} km`;
    btn('rc-stop', !r.stopped);
    btn('rc-resume', op && r.stopped);
    btn('rc-cancel', op && !!r.delivery_id);
    btn('rc-return', op && !r.delivery_id && !r.at_base && r.mode !== 'returning');
}

// Only important events pop up; everything is always in the Events feed.
let lastToastAt = 0;
function eventToast(e) {
    if (e.level !== 'critical' && e.level !== 'success') return;
    const now = Date.now();
    if (e.level !== 'success' && now - lastToastAt < 3000) return;
    lastToastAt = now;
    toast(e.message, e.level);
}

function handleEvents(events) {
    const fresh = events.filter((e) => e.id > S.lastEventId);
    if (!fresh.length) return;
    const ul = $('events');
    const first = S.lastEventId === 0;
    S.lastEventId = Math.max(...events.map((e) => e.id));
    if (ul.firstChild && ul.firstChild.classList.contains('muted')) ul.replaceChildren();
    fresh.forEach((e) => {
        const li = el('li', null, safeClass(e.level));
        li.appendChild(el('span', new Date(e.ts * 1000).toLocaleTimeString(), 'ev-t'));
        li.appendChild(el('span', null, 'ev-l'));
        li.appendChild(el('span', e.message));
        ul.insertBefore(li, ul.firstChild);
        if (!first) eventToast(e);
        if (!first && /completed|failed|cancelled/i.test(e.message)) loadHistory();
    });
    while (ul.children.length > 80) ul.lastChild.remove();
}

// ------------------------------------------------------------------ connections
function connectWebSocket() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}/api/v1/ws/fleet`);
    let ping = null;
    ws.onopen = () => {
        setPill('conn-pill', 'conn-text', 'ok', 'Live');
        ping = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}'); }, 30000);
    };
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.type === 'fleet') renderFleet(msg.data);
    };
    ws.onclose = (ev) => {
        clearInterval(ping);
        setPill('conn-pill', 'conn-text', 'bad', 'Reconnecting');
        if (ev.code === 4401) { window.location.replace('/login'); return; }
        setTimeout(connectWebSocket, 3000);
    };
}

async function pollFleet() {
    try { renderFleet(await Api.get('/api/v1/fleet')); } catch (_) { /* the pills show connection state */ }
}

async function pollMap() {
    let next = 20000;
    try {
        const m = await Api.get('/api/v1/map/status');
        S.mapState = m.state;
        drawArea(m.area);
        const txt = {
            ready: `Road map · ${Number(m.nodes).toLocaleString()} junctions`,
            offline: 'Offline demo grid',
            downloading: /contacting/i.test(m.message || '') ? 'Contacting map servers…' : 'Downloading road map…',
            preparing: 'Preparing road network…',
            idle: 'No road map loaded',
            error: /retrying/i.test(m.message || '') ? 'Map servers busy · retrying…' : 'Road map not available',
        }[m.state] || m.state;
        setPill('map-pill', 'map-text', { ready: 'ok', offline: 'warn', downloading: 'warn', preparing: 'warn', error: 'bad' }[m.state] || '', txt);
        $('map-pill').title = m.area ? `Trips must start and end inside the dashed circle (${fmt(m.area.radius_m / 1000, 1)} km). ${m.message || ''}` : (m.message || '');
        const noMap = !m.area && ['error', 'downloading', 'idle'].includes(m.state);
        $('map-fallback').classList.toggle('hidden', !(isOperator() && noMap));
        $('map-fallback-msg').textContent = m.message || '';
        $('map-online').classList.toggle('hidden', !(isOperator() && m.state === 'offline'));
        if (m.state === 'downloading' || m.state === 'preparing' || S.busy) next = 3000;
        if (S.busy) setHint(planningHint(), 'busy');
    } catch (_) { next = 10000; }
    clearTimeout(S.mapTimer);
    S.mapTimer = setTimeout(pollMap, next);
}

function kickMapPoll() { clearTimeout(S.mapTimer); S.mapTimer = setTimeout(pollMap, 300); }

// ------------------------------------------------------------------ dock: history / admin
async function loadHistory() {
    try {
        const rows = await Api.get('/api/v1/deliveries/history?limit=25');
        const tb = $('history-body');
        tb.replaceChildren();
        rows.forEach((d) => {
            const tr = el('tr', null, 'clickable');
            tr.appendChild(el('td', `#${d.id}`));
            tr.appendChild(el('td', d.robot_id || '—'));
            tr.appendChild(el('td', d.created_at ? new Date(d.created_at).toLocaleString() : '—'));
            tr.appendChild(el('td', fmtKm(d.total_distance_m || 0)));
            tr.appendChild(el('td', [d.algorithm, d.profile].filter(Boolean).join(' · ') || '—'));
            const st = el('td');
            st.appendChild(el('span', d.status, 'status ' + safeClass(d.status)));
            tr.appendChild(st);
            tr.addEventListener('click', () => showHistoric(d.id));
            tb.appendChild(tr);
        });
        if (!rows.length) { const tr = el('tr'); const td = el('td', 'No deliveries yet', 'muted'); td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr); }
    } catch (_) { /* ignore */ }
}

async function showHistoric(id) {
    try {
        const d = await Api.get(`/api/v1/deliveries/${id}`);
        S.historyLayer.clearLayers();
        const pts = d.path_planned || [];
        if (pts.length > 1) {
            S.historyLayer.addLayer(L.polyline(pts, { color: '#f59e0b', weight: 4, opacity: 0.8, dashArray: '2 8' }));
            fitTo(pts);
            toast(`Showing the planned route of delivery #${id} (${d.status})`, 'info');
        }
    } catch (e) { toast(e.message, 'error'); }
}

async function loadRobotsAdmin() {
    const rows = await Api.get('/api/v1/admin/robots');
    const tb = $('robots-body');
    tb.replaceChildren();
    rows.forEach((r) => {
        const tr = el('tr');
        const idTd = el('td');
        const badge = el('span', r.id, 'status');
        badge.style.background = colorOf(r.id);
        badge.style.color = '#04121a';
        idTd.appendChild(badge);
        tr.appendChild(idTd);
        tr.appendChild(el('td', r.name));
        tr.appendChild(el('td', `${fmt(r.home_lat, 4)}, ${fmt(r.home_lon, 4)}`, 'muted'));
        const act = el('td');
        const del = el('button', 'Remove', 'btn small danger');
        del.type = 'button';
        del.disabled = rows.length <= 1;
        del.addEventListener('click', () => {
            if (window.confirm(`Remove ${r.id} (${r.name}) from the fleet?`)) {
                adminAction(() => Api.del(`/api/v1/admin/robots/${encodeURIComponent(r.id)}`), loadRobotsAdmin);
            }
        });
        act.appendChild(del);
        tr.appendChild(act);
        tb.appendChild(tr);
    });
}

async function loadUsers() {
    const users = await Api.get('/api/v1/admin/users');
    const tb = $('users-body');
    tb.replaceChildren();
    users.forEach((u) => {
        const tr = el('tr');
        tr.appendChild(el('td', u.username));
        const roleTd = el('td');
        const sel = el('select', null, 'select');
        ['viewer', 'operator', 'admin'].forEach((r) => { const o = el('option', r); o.value = r; o.selected = r === u.role; sel.appendChild(o); });
        sel.disabled = u.id === S.me.id;
        sel.addEventListener('change', () => adminAction(() => Api.patch(`/api/v1/admin/users/${u.id}`, { role: sel.value }), loadUsers));
        roleTd.appendChild(sel); tr.appendChild(roleTd);
        const st = el('td');
        st.appendChild(el('span', !u.is_active ? 'disabled' : u.locked ? 'locked' : 'active', 'status ' + (!u.is_active ? 'failed' : u.locked ? 'pending' : 'completed')));
        tr.appendChild(st);
        const act = el('td');
        if (u.id !== S.me.id) {
            const t = el('button', u.is_active ? 'Disable' : 'Enable', 'btn small'); t.type = 'button';
            t.addEventListener('click', () => adminAction(() => Api.patch(`/api/v1/admin/users/${u.id}`, { is_active: !u.is_active }), loadUsers));
            act.appendChild(t);
            if (u.locked) {
                const ul = el('button', 'Unlock', 'btn small'); ul.type = 'button';
                ul.addEventListener('click', () => adminAction(() => Api.post(`/api/v1/admin/users/${u.id}/unlock`), loadUsers));
                act.appendChild(ul);
            }
            const del = el('button', 'Delete', 'btn small danger'); del.type = 'button';
            del.addEventListener('click', () => { if (window.confirm(`Delete user ${u.username}?`)) adminAction(() => Api.del(`/api/v1/admin/users/${u.id}`), loadUsers); });
            act.appendChild(del);
        } else act.appendChild(el('span', 'you', 'muted'));
        tr.appendChild(act);
        tb.appendChild(tr);
    });
}

async function adminAction(fn, reload) {
    try { await fn(); toast('Saved', 'success'); } catch (e) { toast(e.message, 'error'); }
    if (reload) reload().catch(() => {});
    pollFleet();
}

// ------------------------------------------------------------------ robot actions
async function robotAction(path, confirmText) {
    const rid = S.selected;
    if (!rid) return;
    if (confirmText && !window.confirm(confirmText.replace('{id}', rid))) return;
    try {
        const r = await Api.post(`/api/v1/robots/${encodeURIComponent(rid)}/${path}`);
        toast(r.message, path === 'stop' ? 'critical' : path === 'cancel' ? 'warning' : 'success');
        await pollFleet();
        if (path === 'cancel') loadHistory();
    } catch (e) { toast(e.message, 'error', 6000); }
}

// ------------------------------------------------------------------ boot
function wireChips(id, key) {
    $(id).addEventListener('click', (e) => {
        const b = e.target.closest('.chip');
        if (!b) return;
        $(id).querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === b));
        S[key] = b.dataset.v;
        clearPreview();
        S.hintSticky = false;
        updatePlanner();
    });
}

function wireDock() {
    $('dock-tabs').addEventListener('click', (e) => {
        const b = e.target.closest('button[data-tab]');
        if (!b) return;
        $('dock').classList.remove('collapsed');
        $('dock-tabs').querySelectorAll('button[data-tab]').forEach((x) => x.classList.toggle('active', x === b));
        document.querySelectorAll('[data-pane]').forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== b.dataset.tab));
        if (b.dataset.tab === 'history') loadHistory();
        if (b.dataset.tab === 'admin') {
            loadRobotsAdmin().catch((err) => toast(err.message, 'error'));
            loadUsers().catch((err) => toast(err.message, 'error'));
        }
    });
    $('dock-toggle').addEventListener('click', () => {
        const c = $('dock').classList.toggle('collapsed');
        $('dock-toggle').textContent = c ? '▴' : '▾';
    });
}

function fitFleet() {
    const pts = [];
    if (S.fleet) S.fleet.robots.forEach((r) => pts.push([r.lat, r.lon]));
    Object.values(S.bots).forEach((b) => b.lines.forEach((l) => l.getLatLngs().forEach((p) => pts.push([p.lat, p.lng]))));
    if (S.preview) S.preview.coords.forEach((p) => pts.push(p));
    if (pts.length === 1) S.map.setView(pts[0], 16);
    else fitTo(pts);
}

async function boot() {
    try { S.me = await Api.get('/api/v1/auth/me'); } catch (_) { return; }
    Api.setCsrf(S.me.csrf_token);
    $('user-name').textContent = S.me.username;
    $('user-role').textContent = S.me.role;
    $('avatar').textContent = S.me.username.slice(0, 1).toUpperCase();
    if (!isOperator()) {
        document.querySelectorAll('.op-only').forEach((n) => n.classList.add('hidden'));
        document.querySelectorAll('.viewer-only').forEach((n) => n.classList.remove('hidden'));
    }
    if (S.me.role !== 'admin') document.querySelectorAll('.admin-only').forEach((n) => n.classList.add('hidden'));

    let center = [12.9716, 77.5946];
    try {
        const m = await Api.get('/api/v1/map/status');
        center = m.area ? [m.area.lat, m.area.lon] : [m.geofence.lat, m.geofence.lon];
    } catch (_) { /* default */ }
    initMap(center);

    wireSearch('pickup'); wireSearch('dropoff');
    wireChips('algo-chips', 'algo'); wireChips('profile-chips', 'profile');
    wireDock();
    $('robot-select').addEventListener('change', () => { clearPreview(); S.hintSticky = false; updatePlanner(); });
    $('swap').addEventListener('click', () => {
        const a = S.places.pickup, b = S.places.dropoff;
        clearPlace('pickup'); clearPlace('dropoff');
        if (b) setPlace('pickup', b.lat, b.lon, b.label);
        if (a) setPlace('dropoff', a.lat, a.lon, a.label);
        updatePlanner();
    });
    $('preview-btn').addEventListener('click', previewRoutes);
    $('dispatch-btn').addEventListener('click', dispatch);
    $('estop').addEventListener('click', async () => {
        try { const r = await Api.post('/api/v1/fleet/stop_all'); toast(r.message, 'critical'); pollFleet(); } catch (e) { toast(e.message, 'error'); }
    });
    $('release-all').addEventListener('click', async () => {
        try { const r = await Api.post('/api/v1/fleet/resume_all'); toast(r.message, 'success'); pollFleet(); } catch (e) { toast(e.message, 'error'); }
    });
    $('rc-stop').addEventListener('click', () => robotAction('stop'));
    $('rc-resume').addEventListener('click', () => robotAction('resume'));
    $('rc-cancel').addEventListener('click', () => robotAction('cancel', 'Cancel the delivery {id} is doing?'));
    $('rc-return').addEventListener('click', () => robotAction('return'));
    $('sim-speed').addEventListener('click', async (e) => {
        const b = e.target.closest('button[data-v]');
        if (!b) return;
        try { await Api.post('/api/v1/fleet/sim_speed', { scale: Number(b.dataset.v) }); pollFleet(); } catch (err) { toast(err.message, 'error'); }
    });
    $('btn-follow').addEventListener('click', () => setFollow(!S.follow));
    $('btn-fit').addEventListener('click', fitFleet);
    $('btn-theme').addEventListener('click', () => {
        S.dark = !S.dark;
        document.body.classList.toggle('map-dark', S.dark);
        store('or-theme', S.dark ? 'dark' : 'light');
    });
    const toggleOffline = async (enabled) => {
        try {
            await Api.post('/api/v1/map/offline', { enabled });
            toast(enabled ? 'Offline demo grid enabled — routes use a synthetic street grid, not real roads'
                : 'Switched back to the real OpenStreetMap road map', enabled ? 'warning' : 'success', 6000);
            clearPreview(); S.hintSticky = false; updatePlanner(); kickMapPoll();
        } catch (e) { toast(e.message, 'error', 6000); }
    };
    $('offline-btn').addEventListener('click', () => toggleOffline(true));
    $('online-btn').addEventListener('click', () => toggleOffline(false));
    $('retry-map-btn').addEventListener('click', async () => {
        try {
            const r = await Api.post('/api/v1/map/retry');
            toast(r.started ? 'Retrying the road-map download…' : 'A download is already in progress', 'info');
            kickMapPoll();
        } catch (e) { toast(e.message, 'error'); }
    });
    $('new-robot-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = $('new-robot-name').value.trim();
        await adminAction(() => Api.post('/api/v1/admin/robots', name ? { name } : {}), loadRobotsAdmin);
        $('new-robot-name').value = '';
    });
    $('new-user-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await adminAction(() => Api.post('/api/v1/admin/users', {
            username: $('new-username').value.trim(), password: $('new-password').value, role: $('new-role').value,
        }), loadUsers);
        $('new-password').value = '';
    });
    $('logout-btn').addEventListener('click', async () => {
        try { await Api.post('/api/v1/auth/logout'); } catch (_) { /* ignore */ }
        window.location.replace('/login');
    });

    renderSelected();
    connectWebSocket();
    await pollFleet();
    pollMap();
    loadHistory();
    setInterval(pollFleet, 5000);
    setInterval(loadHistory, 30000);
    updatePlanner();
}

document.addEventListener('DOMContentLoaded', boot);
