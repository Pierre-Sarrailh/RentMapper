#!/usr/bin/env python3
"""
Pull grocery stores for the GTA from OpenStreetMap via Overpass.

Outputs groceries.jsonl with one record per store:
    {id, name, brand, lat, lng, shop, tier}

`tier` is the useful part: "full" means a real grocery shop you could do a
week's shopping at; "produce" means greengrocers and small health-food shops
that are great for a top-up but shouldn't be the only thing within walking
distance. Score them differently.

Deps: pip install requests
"""

import json
import math
import re
import sys
import time

import requests

# South, West, North, East — Overpass order. Covers Toronto plus the inner GTA,
# matching the spread of the rentals data.
BBOX = "43.55,-79.70,43.90,-79.05"

QUERY = f"""
[out:json][timeout:180];
(
  nwr["shop"~"^(supermarket|greengrocer|grocery|health_food)$"]({BBOX});
  nwr["shop"="wholesale"]({BBOX});
  nwr["shop"="department_store"]["name"~"Walmart",i]({BBOX});
);
out center tags;
"""

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",  # mirror, if the main one is busy
]

# Chains that are unambiguously a full grocery shop, regardless of how the
# individual store got tagged. OSM tagging is inconsistent across contributors.
FULL_CHAINS = re.compile(
    r"loblaw|no.?frills|metro|sobeys|freshco|food.?basics|zehrs|fortinos|"
    r"valu.?mart|independent|farm.?boy|longo|whole.?foods|t&t|tnt supermarket|"
    r"h.?mart|galleria|nations fresh|food.?land|costco|walmart|real canadian|"
    r"superstore|bulk barn|adonis|highland farms|coppa|organic garage|"
    r"rabba|summerhill market|fiesta farms|sunny food|oceans fresh",
    re.I,
)


def fetch():
    for url in ENDPOINTS:
        try:
            print(f"querying {url} ...", file=sys.stderr)
            r = requests.post(url, data={"data": QUERY}, timeout=300)
            if r.status_code == 429 or r.status_code == 504:
                print(f"  busy ({r.status_code}), trying next mirror", file=sys.stderr)
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()["elements"]
        except requests.RequestException as e:
            print(f"  failed: {e}", file=sys.stderr)
    sys.exit("all Overpass endpoints failed — wait a minute and retry")


def coords(el):
    """Nodes carry lat/lon directly; ways and relations get a `center`."""
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return None


def classify(tags):
    name = f"{tags.get('name', '')} {tags.get('brand', '')}"
    shop = tags.get("shop", "")
    if FULL_CHAINS.search(name):
        return "full"
    if shop in ("supermarket", "wholesale", "department_store", "grocery"):
        return "full"
    return "produce"  # greengrocer, health_food, and unbranded odds and ends


def haversine_m(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def dedupe(stores, radius_m=60):
    """A store mapped as both a node and a building polygon appears twice.
    Collapse same-name entries that sit within `radius_m` of each other."""
    kept = []
    for s in sorted(stores, key=lambda x: x["name"].lower()):
        key = s["name"].lower().strip()
        dup = False
        for k in kept:
            if k["name"].lower().strip() == key and key:
                if haversine_m((s["lat"], s["lng"]), (k["lat"], k["lng"])) < radius_m:
                    dup = True
                    break
        if not dup:
            kept.append(s)
    return kept


def main():
    elements = fetch()
    print(f"{len(elements)} raw elements", file=sys.stderr)

    stores = []
    skipped_nocoord = 0
    for el in elements:
        c = coords(el)
        if not c:
            skipped_nocoord += 1
            continue
        tags = el.get("tags", {})
        stores.append({
            "id": f"{el['type']}/{el['id']}",
            "name": tags.get("name") or tags.get("brand") or "(unnamed)",
            "brand": tags.get("brand"),
            "lat": c[0],
            "lng": c[1],
            "shop": tags.get("shop"),
            "tier": classify(tags),
        })

    before = len(stores)
    stores = dedupe(stores)

    with open("groceries.jsonl", "w") as f:
        for s in stores:
            f.write(json.dumps(s) + "\n")

    full = sum(1 for s in stores if s["tier"] == "full")
    unnamed = sum(1 for s in stores if s["name"] == "(unnamed)")
    print(f"\n{len(stores)} stores after dedupe ({before - len(stores)} duplicates "
          f"removed, {skipped_nocoord} without coordinates)", file=sys.stderr)
    print(f"  full grocery: {full}", file=sys.stderr)
    print(f"  produce/small: {len(stores) - full}", file=sys.stderr)
    print(f"  unnamed: {unnamed}", file=sys.stderr)
    print("\n-> groceries.jsonl", file=sys.stderr)


if __name__ == "__main__":
    main()