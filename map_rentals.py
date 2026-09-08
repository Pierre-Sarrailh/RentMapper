#!/usr/bin/env python3
"""Ingest a rentals JSONL file and render every listing as a pin on a map.

Produces a single self-contained HTML file (Leaflet + marker clustering, loaded
from CDN) with no third-party Python dependencies.

Pulls in two optional side inputs when they exist:
  * groceries.jsonl   from fetch_grocery.py — the store layer, and the walking
                      distance from each listing to the nearest full-service shop
  * commute-*.json    from commute.py — transit minutes from each listing to an
                      office address

Usage:
    python3 map_rentals.py                          # auto-discovers everything
    python3 map_rentals.py rentals-toronto.jsonl -o map.html
"""

import argparse
import glob
import json
import os
import sys

from geo import GridIndex

LISTING_URL = "https://rentals.ca/{path}"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?",
                   help="rentals .jsonl file (default: newest rentals-*.jsonl here)")
    p.add_argument("-o", "--output", default="rentals_map.html",
                   help="output HTML file (default: rentals_map.html)")
    p.add_argument("-c", "--commute",
                   help="commute JSON from commute.py (default: newest "
                        "commute-*.json here); skipped silently if missing")
    p.add_argument("-g", "--groceries", default="groceries.jsonl",
                   help="grocery .jsonl from fetch_grocery.py (default: groceries.jsonl); "
                        "skipped silently if missing")
    p.add_argument("--open", action="store_true",
                   help="open the map in the default browser when done")
    return p.parse_args()


def find_input(explicit):
    if explicit:
        return explicit
    matches = sorted(glob.glob("rentals-*.jsonl"), key=os.path.getmtime, reverse=True)
    if not matches:
        sys.exit("No input given and no rentals-*.jsonl found in the current directory.")
    return matches[0]


def num(value):
    """Return value if it is a real number, else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def rng(record, key):
    """Pull a [low, high] range field, tolerating nulls and short lists."""
    raw = record.get(key) or []
    lo = num(raw[0]) if len(raw) > 0 else None
    hi = num(raw[1]) if len(raw) > 1 else lo
    return lo, (hi if hi is not None else lo)


def load_listings(path):
    """Parse the JSONL file into flat dicts ready for the map. Returns (rows, stats)."""
    rows = []
    stats = {"lines": 0, "bad_json": 0, "no_location": 0}

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["bad_json"] += 1
                continue

            loc = rec.get("location") or []
            # The feed stores coordinates as [longitude, latitude].
            lon = num(loc[0]) if len(loc) > 0 else None
            lat = num(loc[1]) if len(loc) > 1 else None
            if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                stats["no_location"] += 1
                continue

            addr = rec.get("address") or {}
            hood = addr.get("neighbourhood") or {}
            rent_lo, rent_hi = rng(rec, "rentRange")
            beds_lo, beds_hi = rng(rec, "bedsRange")
            baths_lo, baths_hi = rng(rec, "bathsRange")
            size_lo, size_hi = rng(rec, "sizeRange")

            # Availability + parking come from the floor plans, not the top level.
            plans = rec.get("floorPlans") or []
            parking = max((num(p.get("parkingSpots")) or 0 for p in plans), default=0)
            available_now = any((p.get("availability") or {}).get("now") for p in plans)

            rows.append({
                "id": rec.get("id"),
                "lat": lat,
                "lon": lon,
                "name": rec.get("name") or addr.get("street") or "Listing",
                "street": addr.get("street") or "",
                "hood": hood.get("name") or "",
                "postal": addr.get("postalCode") or "",
                "type": rec.get("type") or "unknown",
                "rent": rent_lo,
                "rent_hi": rent_hi,
                "beds": beds_lo,
                "beds_hi": beds_hi,
                "baths": baths_lo,
                "size": size_lo,
                "size_hi": size_hi,
                "parking": parking,
                "now": available_now,
                "url": LISTING_URL.format(path=rec["path"]) if rec.get("path") else "",
                "groc_m": None,     # filled by attach_grocery_distance()
                "groc_name": "",
                "commute": None,    # filled by attach_commute()
            })

    return rows, stats


def load_groceries(path):
    """Parse groceries.jsonl. Missing file is fine — the layer is optional."""
    if not path or not os.path.exists(path):
        return []

    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            lat, lng = num(rec.get("lat")), num(rec.get("lng"))
            if lat is None or lng is None:
                continue
            rows.append({
                "id": rec.get("id") or "",
                "name": rec.get("name") or "(unnamed)",
                "lat": lat,
                "lng": lng,
                "shop": rec.get("shop") or "",
                "tier": rec.get("tier") or "produce",
            })
    return rows


def attach_grocery_distance(rows, groceries, radius_m=5000):
    """Distance from each listing to the nearest full-service grocery store.

    Straight-line, matching the "within 1 km" framing of the filter. Only
    full_service counts — a greengrocer down the street does not make a listing
    walkable to a weekly shop.
    """
    full = [g for g in groceries if g["tier"] == "full_service"]
    if not full:
        return 0
    index = GridIndex(((g["lat"], g["lng"], g) for g in full), cell_m=1000)
    matched = 0
    for r in rows:
        dist, store = index.nearest(r["lat"], r["lon"], radius_m)
        if store:
            r["groc_m"] = round(dist)
            r["groc_name"] = store["name"]
            matched += 1
    return matched


def find_commute(explicit):
    if explicit:
        return explicit
    matches = sorted(glob.glob("commute-*.json"), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def attach_commute(rows, path):
    """Merge commute.py's output in by listing id. Returns (office, matched)."""
    if not path or not os.path.exists(path):
        return None, 0
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    minutes = data.get("minutes") or {}
    matched = 0
    for r in rows:
        m = minutes.get(r["id"])
        if m is not None:
            r["commute"] = m
            matched += 1
    return data.get("office"), matched


HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
  html, body { margin: 0; height: 100%; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  #map { position: absolute; inset: 0; }
  .panel {
    position: absolute; top: 12px; right: 12px; z-index: 1000; width: 240px;
    background: rgba(255,255,255,.96); border-radius: 10px; padding: 12px 14px;
    box-shadow: 0 2px 12px rgba(0,0,0,.25); max-height: calc(100% - 24px); overflow-y: auto;
  }
  .panel h2 { margin: 0 0 2px; font-size: 15px; }
  .panel .sub { color: #666; font-size: 12px; margin-bottom: 10px; }
  .group { margin-top: 12px; border-top: 1px solid #e3e3e3; padding-top: 4px; }
  .group-h { font-size: 11px; font-weight: 700; text-transform: uppercase;
             letter-spacing: .04em; color: #888; margin-top: 6px; }
  .hint { font-size: 11px; color: #777; margin-top: 5px; line-height: 1.35; }
  .near { color: #b06000; }
  .panel label { display: block; font-size: 12px; font-weight: 600; margin: 10px 0 3px; }
  .panel select, .panel input[type=range] { width: 100%; box-sizing: border-box; }
  .val { font-weight: 400; color: #666; }
  .legend { margin-top: 12px; border-top: 1px solid #e3e3e3; padding-top: 10px; }
  .legend div { display: flex; align-items: center; gap: 7px; font-size: 12px; margin: 3px 0; }
  .dot { width: 12px; height: 12px; border-radius: 50%; border: 1px solid rgba(0,0,0,.35); }
  .pin { border-radius: 50%; border: 1.5px solid rgba(255,255,255,.9); box-shadow: 0 0 3px rgba(0,0,0,.5); }
  /* Emoji sits in a white puck so it stays readable over any tile colour. */
  .gpin {
    width: 22px; height: 22px; border-radius: 50%; background: rgba(255,255,255,.95);
    box-shadow: 0 0 3px rgba(0,0,0,.45); text-align: center; line-height: 22px; font-size: 13px;
  }
  .office {
    width: 30px; height: 30px; border-radius: 50%; background: #1a3d6b;
    text-align: center; line-height: 30px; font-size: 16px;
    box-shadow: 0 0 0 3px rgba(255,255,255,.9), 0 1px 6px rgba(0,0,0,.5);
  }
  .gcluster {
    width: 30px; height: 30px; border-radius: 50%; background: rgba(26,107,60,.88);
    color: #fff; text-align: center; line-height: 30px; font-size: 12px; font-weight: 600;
    box-shadow: 0 0 4px rgba(0,0,0,.4);
  }
  .leaflet-popup-content { margin: 10px 12px; }
  .pop-title { font-weight: 600; margin-bottom: 4px; }
  .pop-rent { font-size: 17px; font-weight: 700; color: #1a6b3c; }
  .pop-meta { color: #555; font-size: 12px; margin-top: 4px; }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h2>__TITLE__</h2>
  <div class="sub"><span id="shown">0</span> of __COUNT__ listings<br>
    <span class="near" id="nearNote"></span></div>

  <label>Max rent <span class="val" id="rentVal"></span></label>
  <input type="range" id="rent" min="__RENT_MIN__" max="__RENT_MAX__" step="100" value="__RENT_MAX__">

  <label>Min bedrooms <span class="val" id="bedVal">any</span></label>
  <input type="range" id="beds" min="0" max="5" step="1" value="0">

  <label>Min bathrooms <span class="val" id="bathVal">any</span></label>
  <input type="range" id="baths" min="0" max="4" step="1" value="0">

  <div class="group">
    <div class="group-h">Walk &amp; commute</div>

    <label>Max commute <span class="val" id="commuteVal"></span></label>
    <input type="range" id="commute" min="10" max="90" step="5" value="30">

    <label>Max walk to groceries <span class="val" id="grocVal"></span></label>
    <input type="range" id="grocDist" min="250" max="3000" step="50" value="1000">

    <label>Slack <span class="val" id="slackVal"></span></label>
    <input type="range" id="slack" min="0" max="100" step="5" value="25">
    <div class="hint" id="slackHint"></div>
  </div>

  <label>Property type</label>
  <select id="type"><option value="">All types</option>__TYPE_OPTIONS__</select>

  <label><input type="checkbox" id="now"> Available now only</label>
  <label><input type="checkbox" id="groc" checked> Grocery stores (__GROCERY_COUNT__)</label>

  <div class="legend" id="legend"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const LISTINGS = __DATA__;
const GROCERIES = __GROCERIES__;
const OFFICE = __OFFICE__;

// Sliders at the top of their range mean "no limit" rather than a literal value;
// 90 minutes and 3 km are already past the point of caring.
const NO_COMMUTE_LIMIT = 90, NO_GROC_LIMIT = 3000;

// Either side input is optional. Without it every listing scores null on that
// axis, so an enabled limit would hide the entire map — pin the control open.
const HAS_COMMUTE = LISTINGS.some(l => l.commute !== null);
const HAS_GROC = LISTINGS.some(l => l.groc_m !== null);

// Overshoot budget. Each target contributes how far past it a listing sits, as a
// fraction of that target. Beating a target earns nothing, it just costs nothing.
// A listing qualifies while its total overshoot fits inside the slack budget, so
// 1.1 km from a shop (0.10 over) clears a 0.25 budget but 1.4 km (0.40) does not.
function overshoot(l, commuteMax, grocMax) {
  let over = 0;
  if (commuteMax !== null) {
    if (l.commute === null) return Infinity;  // unreachable by transit
    over += Math.max(0, l.commute / commuteMax - 1);
  }
  if (grocMax !== null) {
    if (l.groc_m === null) return Infinity;
    over += Math.max(0, l.groc_m / grocMax - 1);
  }
  return over;
}

// Three tiers, because "there is a grocery store nearby" means very different
// things: a weekly shop, a partial shop, or just a top-up.
const GROCERY_TIERS = {
  full_service: { emoji: "🛒", label: "Full service (produce, dairy, meat)" },
  limited:      { emoji: "🥫", label: "Limited selection" },
  produce:      { emoji: "🥬", label: "Produce / small" }
};

// Rent buckets drive both the pin colour and the legend.
const BUCKETS = [
  { max: 1500,     color: "#2b8cbe", label: "< $1,500" },
  { max: 2000,     color: "#41ab5d", label: "$1,500 - $2,000" },
  { max: 2500,     color: "#d9b400", label: "$2,000 - $2,500" },
  { max: 3500,     color: "#f16913", label: "$2,500 - $3,500" },
  { max: Infinity, color: "#cb181d", label: "$3,500+" }
];
const GREY = "#8a8a8a";

function colorFor(rent) {
  if (rent === null) return GREY;
  return BUCKETS.find(b => rent < b.max).color;
}

const money = n => n === null ? "n/a" : "$" + n.toLocaleString();
const distText = m => m < 1000 ? Math.round(m / 10) * 10 + " m"
                               : (m / 1000).toFixed(1) + " km";

function rentText(l) {
  if (l.rent === null) return "Price n/a";
  return l.rent_hi && l.rent_hi !== l.rent
    ? money(l.rent) + " - " + money(l.rent_hi) : money(l.rent);
}

function popup(l) {
  const bits = [];
  if (l.beds !== null) bits.push(l.beds === 0 ? "studio" : l.beds + " bed");
  if (l.baths !== null) bits.push(l.baths + " bath");
  if (l.size) bits.push(l.size.toLocaleString() + " sqft");
  if (l.parking) bits.push(l.parking + " parking");
  const where = [l.street, l.hood, l.postal].filter(Boolean).join(" &middot; ");
  return '<div class="pop-title">' + esc(l.name) + "</div>" +
         '<div class="pop-rent">' + rentText(l) + "</div>" +
         '<div class="pop-meta">' + bits.join(" &middot; ") + "</div>" +
         (where ? '<div class="pop-meta">' + esc(where) + "</div>" : "") +
         (l.commute !== null
            ? '<div class="pop-meta">\U0001F687 ' + Math.round(l.commute) +
              " min to " + esc(OFFICE ? OFFICE.label : "office") + "</div>"
            : (OFFICE ? '<div class="pop-meta">\U0001F687 no transit route found</div>' : "")) +
         (l.groc_m !== null
            ? '<div class="pop-meta">\U0001F6D2 ' + distText(l.groc_m) + " to " +
              esc(l.groc_name) + "</div>"
            : "") +
         '<div class="pop-meta">' + esc(l.type.replace(/_/g, " ")) +
           (l.now ? " &middot; available now" : "") + "</div>" +
         (l.url ? '<div class="pop-meta"><a href="' + esc(l.url) + '" target="_blank" rel="noopener">View listing &rarr;</a></div>' : "");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const map = L.map("map");
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  maxZoom: 20
}).addTo(map);

const cluster = L.markerClusterGroup({
  chunkedLoading: true,
  spiderfyOnMaxZoom: true,
  disableClusteringAtZoom: 17,
  maxClusterRadius: 55
}).addTo(map);

// Build every marker once, then swap the cluster's contents as filters change.
const markers = LISTINGS.map(l => {
  const size = l.rent === null ? 9 : Math.max(9, Math.min(17, 8 + l.rent / 700));
  const m = L.circleMarker([l.lat, l.lon], {
    radius: size / 2, color: "#fff", weight: 1.2, opacity: .9,
    fillColor: colorFor(l.rent), fillOpacity: .85
  });
  m.bindPopup(() => popup(l), { maxWidth: 300 });
  m.bindTooltip(rentText(l), { direction: "top" });
  m.listing = l;
  return m;
});

// Groceries get their own cluster so 900-odd pins stay legible when zoomed out,
// and never merge into the rent clusters.
const groceryCluster = L.markerClusterGroup({
  chunkedLoading: true,
  disableClusteringAtZoom: 15,
  maxClusterRadius: 45,
  iconCreateFunction: c => L.divIcon({
    html: '<div class="gcluster">' + c.getChildCount() + "</div>",
    className: "", iconSize: [30, 30]
  })
});

groceryCluster.addLayers(GROCERIES.map(g => {
  const tier = GROCERY_TIERS[g.tier] || GROCERY_TIERS.limited;
  const m = L.marker([g.lat, g.lng], {
    icon: L.divIcon({
      html: '<div class="gpin">' + tier.emoji + "</div>",
      className: "", iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -11]
    }),
    // Keep grocery pins under the listings, which are the point of the map.
    zIndexOffset: -500
  });
  const osm = g.id ? "https://www.openstreetmap.org/" + g.id : "";
  m.bindPopup('<div class="pop-title">' + esc(g.name) + "</div>" +
    '<div class="pop-meta">' + tier.label +
      (g.shop ? " &middot; " + esc(g.shop.replace(/_/g, " ")) : "") + "</div>" +
    (osm ? '<div class="pop-meta"><a href="' + osm + '" target="_blank" rel="noopener">OpenStreetMap &rarr;</a></div>' : ""),
    { maxWidth: 260 });
  m.bindTooltip(tier.emoji + " " + esc(g.name), { direction: "top" });
  return m;
}));

if (OFFICE) {
  L.marker([OFFICE.lat, OFFICE.lng], {
    icon: L.divIcon({
      html: '<div class="office">\U0001F3E2</div>',
      className: "", iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -15]
    }),
    zIndexOffset: 1000
  }).addTo(map).bindTooltip(esc(OFFICE.label), { direction: "top" });
}

const $ = id => document.getElementById(id);

for (const [id, ok, limit, why] of [
  ["commute", HAS_COMMUTE, NO_COMMUTE_LIMIT, "run commute.py to enable"],
  ["grocDist", HAS_GROC, NO_GROC_LIMIT, "run fetch_grocery.py to enable"]
]) {
  if (!ok) {
    $(id).value = limit;
    $(id).disabled = true;
    $(id).title = why;
  }
}

function apply() {
  const maxRent = +$("rent").value;
  const atMax = maxRent >= +$("rent").max;
  const minBeds = +$("beds").value;
  const minBaths = +$("baths").value;
  const type = $("type").value;
  const nowOnly = $("now").checked;

  const commuteRaw = +$("commute").value;
  const grocRaw = +$("grocDist").value;
  const commuteMax = commuteRaw >= NO_COMMUTE_LIMIT ? null : commuteRaw;
  const grocMax = grocRaw >= NO_GROC_LIMIT ? null : grocRaw;
  const slack = +$("slack").value / 100;

  $("rentVal").textContent = atMax ? "any" : "<= " + money(maxRent);
  $("bedVal").textContent = minBeds === 0 ? "any" : minBeds + "+";
  $("bathVal").textContent = minBaths === 0 ? "any" : minBaths + "+";
  $("commuteVal").textContent = !HAS_COMMUTE ? "no data"
    : commuteMax === null ? "any" : commuteMax + " min";
  $("grocVal").textContent = !HAS_GROC ? "no data"
    : grocMax === null ? "any" : distText(grocMax);
  $("slackVal").textContent = slack === 0 ? "strict" : "+" + Math.round(slack * 100) + "%";
  $("slackHint").textContent = slack === 0
    ? "Both limits are hard."
    : "A listing may exceed the limits by " + Math.round(slack * 100) +
      "% in total and still qualify.";

  let near = 0;
  const keep = markers.filter(m => {
    const l = m.listing;
    if (!atMax && (l.rent === null || l.rent > maxRent)) return false;
    if (minBeds && (l.beds === null || l.beds < minBeds)) return false;
    if (minBaths && (l.baths === null || l.baths < minBaths)) return false;
    if (type && l.type !== type) return false;
    if (nowOnly && !l.now) return false;

    const over = overshoot(l, commuteMax, grocMax);
    if (over > slack) return false;
    // Dim the ones that only qualified on slack, so a near-miss reads as one.
    const isNear = over > 0;
    if (isNear) near++;
    m.setStyle(isNear ? { fillOpacity: .3, opacity: .45 }
                      : { fillOpacity: .85, opacity: .9 });
    return true;
  });

  cluster.clearLayers();
  cluster.addLayers(keep);
  $("shown").textContent = keep.length.toLocaleString();
  $("nearNote").textContent = near
    ? near.toLocaleString() + " of them just outside the limits"
    : "";

  if ($("groc").checked) map.addLayer(groceryCluster);
  else map.removeLayer(groceryCluster);
}

["rent", "beds", "baths", "commute", "grocDist", "slack",
 "type", "now", "groc"].forEach(id => {
  $(id).addEventListener("input", apply);
  $(id).addEventListener("change", apply);
});

$("legend").innerHTML = BUCKETS
  .map(b => '<div><span class="dot" style="background:' + b.color + '"></span>' + b.label + "</div>")
  .join("") + '<div><span class="dot" style="background:' + GREY + '"></span>Price n/a</div>' +
  Object.values(GROCERY_TIERS)
    .map(t => '<div><span style="width:12px;text-align:center">' + t.emoji + "</span>" + t.label + "</div>")
    .join("");

apply();
map.fitBounds(L.latLngBounds(LISTINGS.map(l => [l.lat, l.lon])), { padding: [30, 30] });
</script>
</body>
</html>
"""


def render(rows, title, groceries=(), office=None):
    rents = sorted(r["rent"] for r in rows if r["rent"] is not None)
    if rents:
        rent_min = int(rents[0] // 100 * 100)
        # Clip the slider at the 99th percentile so a few outliers don't flatten it.
        rent_max = int(-(-rents[int(len(rents) * 0.99)] // 100) * 100)
    else:
        rent_min, rent_max = 0, 10000

    types = sorted({r["type"] for r in rows})
    options = "".join(
        '<option value="{0}">{1}</option>'.format(t, t.replace("_", " ").title())
        for t in types
    )

    data = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)

    out = HTML
    out = out.replace("__DATA__", data)
    out = out.replace("__GROCERIES__",
                      json.dumps(list(groceries), separators=(",", ":"), ensure_ascii=False))
    out = out.replace("__GROCERY_COUNT__", "{:,}".format(len(groceries)))
    out = out.replace("__OFFICE__", json.dumps(office) if office else "null")
    out = out.replace("__TITLE__", title)
    out = out.replace("__COUNT__", "{:,}".format(len(rows)))
    out = out.replace("__RENT_MIN__", str(rent_min))
    out = out.replace("__RENT_MAX__", str(rent_max))
    out = out.replace("__TYPE_OPTIONS__", options)
    return out


def main():
    args = parse_args()
    path = find_input(args.input)

    rows, stats = load_listings(path)
    if not rows:
        sys.exit("No mappable listings found in {}.".format(path))

    groceries = load_groceries(args.groceries)
    with_groc = attach_grocery_distance(rows, groceries)

    commute_path = find_commute(args.commute)
    office, with_commute = attach_commute(rows, commute_path)

    title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").title()
    html = render(rows, title, groceries, office)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("Read      {}  ({:,} lines)".format(path, stats["lines"]))
    print("Mapped    {:,} listings".format(len(rows)))
    if groceries:
        full = sum(1 for g in groceries if g["tier"] == "full_service")
        print("Mapped    {:,} grocery stores from {} ({:,} full service)".format(
            len(groceries), args.groceries, full))
        print("Measured  {:,} listings to their nearest full-service store".format(with_groc))
    else:
        print("Skipped   groceries ({} not found)".format(args.groceries))
    if commute_path:
        print("Joined    {:,} commute times from {}".format(with_commute, commute_path))
        if with_commute < len(rows):
            print("          {:,} listings have no transit route within range".format(
                len(rows) - with_commute))
    else:
        print("Skipped   commute times (no commute-*.json — run commute.py)")
    if stats["no_location"]:
        print("Skipped   {:,} without usable coordinates".format(stats["no_location"]))
    if stats["bad_json"]:
        print("Skipped   {:,} unparseable lines".format(stats["bad_json"]))
    print("Wrote     {} ({:.1f} MB)".format(args.output, os.path.getsize(args.output) / 1e6))

    if args.open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(args.output))


if __name__ == "__main__":
    main()
