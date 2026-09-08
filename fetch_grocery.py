#!/usr/bin/env python3
"""
Pull grocery stores for the GTA from OpenStreetMap via Overpass.

Outputs groceries.jsonl with one record per store:
    {id, name, brand, lat, lng, shop, tier}

`tier` is the useful part, and there are three:
    "full_service" — a complete shop: fresh produce, dairy, meat, grains.
                     Matched against FULL_SERVICE_CHAINS, a hand-kept allowlist.
    "limited"      — sells groceries but is not on the allowlist, so a full
                     shop is not guaranteed. Independents, corner groceries,
                     bulk shops, wholesalers.
    "produce"      — greengrocers and small health-food shops. Fine for a
                     top-up, not for a weekly run.
Score them differently.

Deps: pip install requests
"""

import json
import re
import sys
import time

import requests

from geo import haversine_m

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

# overpass-api.de answers 406 to the default python-requests User-Agent, so this
# is not optional. Identifying the tool is also what the Overpass usage policy asks for.
HEADERS = {"User-Agent": "RentMapper/1.0 (grocery data for a rental map)"}

# 429 = over the per-IP rate limit, 504 = the server is at capacity. Both mean
# "come back shortly", so they are worth a retry rather than a hard failure.
RETRY_STATUS = {429, 504}
ATTEMPTS_PER_ENDPOINT = 3

# The full-service allowlist, supplied by hand. This is deliberately a closed
# list rather than a rule over OSM tags: shop=supermarket turned out to cover
# everything from Loblaws to a bulk-bin shop, so nothing is promoted here
# unless it is named. Patterns are matched against name + brand.
FULL_SERVICE_CHAINS = re.compile(
    r"loblaw|metro|food.?basics|longo|farm.?boy|bestco|blue sky|bruno|"
    r"btrust|c&c|freshco|costco|city\s?market|fiesta farms|food.?land|"
    r"fortinos|galleria|healthy planet|march[eé]\s*leo|no.?frills|"
    r"p\.a\.t|rabba|real canadian|superstore|sobeys|summerhill market|"
    r"walmart|whole foods market|independent|t&t",
    re.I,
)

# shop=wholesale is a catch-all in OSM — it pulls in tool warehouses, surplus
# stores and range-hood suppliers. Keep only the ones that are plausibly food.
FOOD_WHOLESALE = re.compile(
    r"costco|wholesale club|restaurant depot|cash.?(&|and).?carry|food|"
    r"grocer|market|produce|meat|halal|fruit|farm",
    re.I,
)


def fetch():
    for url in ENDPOINTS:
        for attempt in range(1, ATTEMPTS_PER_ENDPOINT + 1):
            print(f"querying {url} (attempt {attempt}) ...", file=sys.stderr)
            try:
                r = requests.post(url, data={"data": QUERY}, headers=HEADERS, timeout=300)
            except requests.RequestException as e:
                print(f"  network error: {e}", file=sys.stderr)
                break  # a dead connection will not fix itself; move to the mirror
            if r.status_code in RETRY_STATUS:
                wait = 5 * attempt  # back off a little further each time
                print(f"  busy ({r.status_code}), retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if not r.ok:
                # Overpass explains query errors in the body; the status alone is not enough.
                print(f"  HTTP {r.status_code}: {r.text[:300].strip()}", file=sys.stderr)
                break
            return r.json()["elements"]
    sys.exit("all Overpass endpoints failed — see the errors above")


def coords(el):
    """Nodes carry lat/lon directly; ways and relations get a `center`."""
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return None


def is_food_wholesale(tags):
    """shop=wholesale needs a name check before we believe it sells groceries."""
    if tags.get("shop") != "wholesale":
        return True
    return bool(FOOD_WHOLESALE.search(f"{tags.get('name', '')} {tags.get('brand', '')}"))


def classify(tags):
    name = f"{tags.get('name', '')} {tags.get('brand', '')}"
    shop = tags.get("shop", "")

    if FULL_SERVICE_CHAINS.search(name):
        return "full_service"
    if shop in ("supermarket", "wholesale", "department_store", "grocery"):
        # Sells groceries, but not on the allowlist — an independent, a corner
        # grocery or a warehouse. Real, just not a guaranteed weekly shop.
        return "limited"
    return "produce"  # greengrocer, health_food, and unbranded odds and ends


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
    skipped_nonfood = 0
    for el in elements:
        c = coords(el)
        if not c:
            skipped_nocoord += 1
            continue
        tags = el.get("tags", {})
        if not is_food_wholesale(tags):
            skipped_nonfood += 1
            continue
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

    tiers = {t: sum(1 for s in stores if s["tier"] == t)
             for t in ("full_service", "limited", "produce")}
    unnamed = sum(1 for s in stores if s["name"] == "(unnamed)")
    print(f"\n{len(stores)} stores after dedupe ({before - len(stores)} duplicates "
          f"removed, {skipped_nocoord} without coordinates, "
          f"{skipped_nonfood} non-food wholesale)", file=sys.stderr)
    print(f"  full service:  {tiers['full_service']}", file=sys.stderr)
    print(f"  limited:       {tiers['limited']}", file=sys.stderr)
    print(f"  produce/small: {tiers['produce']}", file=sys.stderr)
    print(f"  unnamed: {unnamed}", file=sys.stderr)
    print("\n-> groceries.jsonl", file=sys.stderr)


if __name__ == "__main__":
    main()