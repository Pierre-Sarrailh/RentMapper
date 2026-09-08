#!/usr/bin/env python3
"""Ingest a rentals JSONL file and render every listing as a pin on a map.

Produces a single self-contained HTML file (Leaflet + marker clustering, loaded
from CDN) with no third-party Python dependencies.

Usage:
    python3 map_rentals.py                          # auto-discovers rentals-*.jsonl
    python3 map_rentals.py rentals-toronto.jsonl -o map.html
"""

import argparse
import glob
import json
import os
import sys

LISTING_URL = "https://rentals.ca/{path}"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?",
                   help="rentals .jsonl file (default: newest rentals-*.jsonl here)")
    p.add_argument("-o", "--output", default="rentals_map.html",
                   help="output HTML file (default: rentals_map.html)")
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
  <div class="sub"><span id="shown">0</span> of __COUNT__ listings</div>

  <label>Max rent <span class="val" id="rentVal"></span></label>
  <input type="range" id="rent" min="__RENT_MIN__" max="__RENT_MAX__" step="100" value="__RENT_MAX__">

  <label>Min bedrooms <span class="val" id="bedVal">any</span></label>
  <input type="range" id="beds" min="0" max="5" step="1" value="0">

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

const $ = id => document.getElementById(id);

function apply() {
  const maxRent = +$("rent").value;
  const atMax = maxRent >= +$("rent").max;
  const minBeds = +$("beds").value;
  const type = $("type").value;
  const nowOnly = $("now").checked;

  $("rentVal").textContent = atMax ? "any" : "<= " + money(maxRent);
  $("bedVal").textContent = minBeds === 0 ? "any" : minBeds + "+";

  const keep = markers.filter(m => {
    const l = m.listing;
    if (!atMax && (l.rent === null || l.rent > maxRent)) return false;
    if (minBeds && (l.beds === null || l.beds < minBeds)) return false;
    if (type && l.type !== type) return false;
    if (nowOnly && !l.now) return false;
    return true;
  });

  cluster.clearLayers();
  cluster.addLayers(keep);
  $("shown").textContent = keep.length.toLocaleString();

  if ($("groc").checked) map.addLayer(groceryCluster);
  else map.removeLayer(groceryCluster);
}

["rent", "beds", "type", "now", "groc"].forEach(id => {
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


def render(rows, title, groceries=()):
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

    title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").title()
    html = render(rows, title, groceries)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("Read      {}  ({:,} lines)".format(path, stats["lines"]))
    print("Mapped    {:,} listings".format(len(rows)))
    if groceries:
        print("Mapped    {:,} grocery stores from {}".format(len(groceries), args.groceries))
    else:
        print("Skipped   groceries ({} not found)".format(args.groceries))
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
