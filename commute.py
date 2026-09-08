#!/usr/bin/env python3
"""Compute door-to-door transit commute times from every rental listing to an office.

Downloads the TTC, GO and UP Express GTFS feeds and runs a backward Connection
Scan from the office: one sweep yields, for every transit stop in the region, the
latest moment you could stand there and still reach the office on time. Each
listing then just walks to its nearby stops and takes the best option. That is
what makes 7,000 origins affordable — the expensive routing happens once, not
once per listing.

Usage:
    python3 commute.py --office "20 Bay St, Toronto"
    python3 commute.py --office-latlng 43.6426,-79.3871 --label "Union Station"

Writes commute-<slug>.json, which map_rentals.py picks up automatically.

The model is deliberately approximate — the request was for "approximately 30
minutes", not a timetable guarantee:
  * one weekday service day (next Wednesday unless --date says otherwise)
  * three target arrival times, median of the three, so a listing is not judged
    on one lucky or unlucky headway
  * walking is straight-line x1.3 at 4.8 km/h, capped at 800 m to a stop
  * 3 minute minimum transfer, no vehicle-specific dwell or reliability modelling
Expect roughly +/-5 minutes against Google Maps.

Deps: pip install requests
"""

import argparse
import csv
import io
import json
import os
import re
import statistics
import sys
import zipfile
from datetime import date, timedelta

import requests

from geo import GridIndex, haversine_m, walk_seconds

# All three verified live. GO carries UP Express's connecting rail; UP ships its
# own small feed. Namespacing matters because stop ids collide across agencies.
FEEDS = {
    "ttc": "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
           "7795b45e-e65a-4465-81fc-c36b9dfff169/resource/"
           "cfb6b2b8-6191-41e3-bda1-b175c51148cb/download/opendata_ttc_schedules.zip",
    "go":  "https://assets.metrolinx.com/raw/upload/v1683228856/Documents/"
           "Metrolinx/Open%20Data/GO-GTFS.zip",
    "up":  "https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/"
           "Open%20Data/UP-GTFS.zip",
}

GTFS_DIR = "gtfs"
HEADERS = {"User-Agent": "RentMapper/1.0 (personal rental search)"}

MAX_WALK_M = 800        # to a transit stop, either end
FOOTPATH_M = 300        # stop-to-stop walking transfers
MIN_TRANSFER_S = 180    # buffer when changing vehicles
ARRIVALS = ("08:45", "09:00", "09:15")
WINDOW_START = "05:30"  # earliest departure worth loading
MAX_COMMUTE_S = 150 * 60


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--office", help="office address, geocoded via Nominatim")
    g.add_argument("--office-latlng", help="'lat,lng', skips geocoding")
    p.add_argument("--label", help="name for the office pin (default: the address)")
    p.add_argument("-i", "--input", help="rentals .jsonl (default: newest here)")
    p.add_argument("-o", "--output", help="output JSON (default: commute-<slug>.json)")
    p.add_argument("--date", help="service date YYYYMMDD (default: next Wednesday)")
    p.add_argument("--refresh", action="store_true", help="re-download the GTFS feeds")
    return p.parse_args()


# ---------------------------------------------------------------- feeds & input

def feed_path(name):
    return os.path.join(GTFS_DIR, name + ".zip")


def download_feeds(refresh=False):
    os.makedirs(GTFS_DIR, exist_ok=True)
    for name, url in FEEDS.items():
        path = feed_path(name)
        if os.path.exists(path) and not refresh:
            print(f"  {name}: cached ({os.path.getsize(path) / 1e6:.0f} MB)",
                  file=sys.stderr)
            continue
        print(f"  {name}: downloading ...", file=sys.stderr)
        try:
            r = requests.get(url, headers=HEADERS, timeout=180)
            r.raise_for_status()
        except requests.RequestException as e:
            # Name the feed and URL: these links rot, and a bare HTTP error
            # gives no clue which of the three broke.
            sys.exit(f"could not download the {name} feed\n  {url}\n  {e}")
        with open(path, "wb") as fh:
            fh.write(r.content)
        print(f"  {name}: {len(r.content) / 1e6:.0f} MB", file=sys.stderr)


def geocode(address):
    """One Nominatim lookup. Needs a real User-Agent, same as Overpass."""
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": address, "format": "json", "countrycodes": "ca",
                             "limit": 1},
                     headers=HEADERS, timeout=60)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        sys.exit(f"could not geocode {address!r} — try --office-latlng instead")
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"]


def find_rentals(explicit):
    if explicit:
        return explicit
    import glob
    matches = sorted(glob.glob("rentals-*.jsonl"), key=os.path.getmtime, reverse=True)
    if not matches:
        sys.exit("No rentals-*.jsonl found — pass one with -i.")
    return matches[0]


def load_listings(path):
    """(id, lat, lng) for every listing with usable coordinates."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            loc = rec.get("location") or []
            if len(loc) < 2 or not rec.get("id"):
                continue
            lng, lat = loc[0], loc[1]  # the feed stores [lng, lat]
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                out.append((rec["id"], lat, lng))
    return out


# ------------------------------------------------------------------ GTFS parsing

def read_csv(zf, name):
    """GTFS text files routinely carry a UTF-8 BOM; utf-8-sig eats it."""
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig", newline=""))


def hms_to_s(value):
    """GTFS times legitimately exceed 24:00:00, so this cannot use datetime."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def active_services(zf, names, day):
    """service_ids running on `day`, from calendar.txt plus its exceptions."""
    ymd = day.strftime("%Y%m%d")
    dow = ("monday", "tuesday", "wednesday", "thursday", "friday",
           "saturday", "sunday")[day.weekday()]
    active = set()
    if "calendar.txt" in names:
        for row in read_csv(zf, "calendar.txt"):
            if row.get(dow) == "1" and row["start_date"] <= ymd <= row["end_date"]:
                active.add(row["service_id"])
    if "calendar_dates.txt" in names:
        for row in read_csv(zf, "calendar_dates.txt"):
            if row.get("date") == ymd:
                if row.get("exception_type") == "1":
                    active.add(row["service_id"])
                elif row.get("exception_type") == "2":
                    active.discard(row["service_id"])
    return active


def load_feed(name, day, win_lo, win_hi):
    """Return (stops, connections) for one feed, ids prefixed with the feed name."""
    prefix = name + ":"
    with zipfile.ZipFile(feed_path(name)) as zf:
        names = set(zf.namelist())
        services = active_services(zf, names, day)

        stops = {}
        for row in read_csv(zf, "stops.txt"):
            try:
                stops[prefix + row["stop_id"]] = (float(row["stop_lat"]),
                                                  float(row["stop_lon"]))
            except (KeyError, ValueError):
                continue

        keep = {row["trip_id"] for row in read_csv(zf, "trips.txt")
                if row.get("service_id") in services}

        conns = []
        # stop_times.txt is the big one (TTC's is ~250 MB unzipped). It arrives
        # grouped by trip, so emit each trip's connections and drop it rather
        # than holding the whole file in memory.
        current, rows = None, []

        def flush(trip_id, trip_rows):
            if len(trip_rows) < 2:
                return
            trip_rows.sort(key=lambda r: r[0])
            for (_, _, dep_t, dep_s), (_, arr_t, _, arr_s) in zip(trip_rows,
                                                                  trip_rows[1:]):
                if dep_t is None or arr_t is None or arr_t < dep_t:
                    continue
                if dep_t > win_hi or arr_t < win_lo:
                    continue
                conns.append((dep_t, arr_t, prefix + dep_s, prefix + arr_s,
                              prefix + trip_id))

        for row in read_csv(zf, "stop_times.txt"):
            trip = row["trip_id"]
            if trip != current:
                flush(current, rows)
                current, rows = trip, []
            if trip not in keep:
                continue
            try:
                seq = int(row["stop_sequence"])
            except (KeyError, ValueError):
                continue
            rows.append((seq, hms_to_s(row.get("arrival_time")),
                         hms_to_s(row.get("departure_time")), row["stop_id"]))
        flush(current, rows)

    return stops, conns


# --------------------------------------------------------------------- routing

def build_footpaths(stops):
    """Walking transfers between stops within FOOTPATH_M of each other."""
    index = GridIndex(((lat, lng, sid) for sid, (lat, lng) in stops.items()),
                      cell_m=FOOTPATH_M)
    paths = {}
    for sid, (lat, lng) in stops.items():
        near = [(other, walk_seconds(d))
                for d, other in index.near(lat, lng, FOOTPATH_M) if other != sid]
        if near:
            paths[sid] = near
    return paths


def scan(conns_desc, footpaths, seed):
    """Backward Connection Scan.

    Returns tau: stop -> the latest clock time you can be standing at that stop
    and still reach the office. `seed` holds the stops within walking distance of
    the office, already discounted by their walk time.

    Connections are processed in decreasing arrival time, so by the time a
    connection is considered, everything that could follow it has been settled.
    """
    tau = dict(seed)
    trip_ok = set()

    # Walking between two nearby stops on the way to the office.
    for sid, t in list(seed.items()):
        for other, wsec in footpaths.get(sid, ()):
            if t - wsec > tau.get(other, -1e18):
                tau[other] = t - wsec

    for dep_t, arr_t, dep_s, arr_s, trip in conns_desc:
        if trip in trip_ok:
            ok = True  # already riding this vehicle, no transfer to pay for
        else:
            # Alighting here and walking to the office costs no transfer buffer;
            # changing to another vehicle does.
            ok = (arr_s in seed and arr_t <= seed[arr_s]) or \
                 (arr_t + MIN_TRANSFER_S <= tau.get(arr_s, -1e18))
            if ok:
                trip_ok.add(trip)
        if not ok:
            continue
        if dep_t > tau.get(dep_s, -1e18):
            tau[dep_s] = dep_t
            for other, wsec in footpaths.get(dep_s, ()):
                if dep_t - wsec > tau.get(other, -1e18):
                    tau[other] = dep_t - wsec
    return tau


def listing_stops(listings, stops):
    """Pre-resolve each listing's walkable stops once; reused for every arrival."""
    index = GridIndex(((lat, lng, sid) for sid, (lat, lng) in stops.items()),
                      cell_m=MAX_WALK_M)
    out = {}
    for lid, lat, lng in listings:
        out[lid] = [(sid, walk_seconds(d))
                    for d, sid in index.near(lat, lng, MAX_WALK_M)]
    return out


def door_to_door(near_stops, tau, target_s, walk_only_s):
    """Minutes from this listing's door to the office, or None if unreachable."""
    best = walk_only_s
    for sid, wsec in near_stops:
        t = tau.get(sid)
        if t is None:
            continue
        total = (target_s - t) + wsec  # leave home at tau - walk
        if 0 < total < MAX_COMMUTE_S and (best is None or total < best):
            best = total
    return best / 60.0 if best is not None else None


# ------------------------------------------------------------------------- main

def next_wednesday(today=None):
    today = today or date.today()
    return today + timedelta(days=(2 - today.weekday()) % 7 or 7)


def main():
    args = parse_args()

    if args.office_latlng:
        lat_s, lng_s = args.office_latlng.split(",")
        office = (float(lat_s), float(lng_s))
        label = args.label or args.office_latlng
    else:
        lat, lng, display = geocode(args.office)
        office, label = (lat, lng), args.label or args.office
        print(f"office: {display}\n        {lat:.5f}, {lng:.5f}", file=sys.stderr)

    day = (date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:8]))
           if args.date else next_wednesday())
    print(f"service date: {day} ({day.strftime('%A')})", file=sys.stderr)

    print("feeds:", file=sys.stderr)
    download_feeds(args.refresh)

    win_lo = hms_to_s(WINDOW_START + ":00")
    win_hi = max(hms_to_s(a + ":00") for a in ARRIVALS)

    stops, conns = {}, []
    for name in FEEDS:
        s, c = load_feed(name, day, win_lo, win_hi)
        stops.update(s)
        conns.extend(c)
        print(f"  {name}: {len(s):,} stops, {len(c):,} connections in window",
              file=sys.stderr)
    if not conns:
        sys.exit("no connections found — is the service date a holiday, "
                 "or are the feeds stale? try --date or --refresh")

    print(f"total: {len(stops):,} stops, {len(conns):,} connections", file=sys.stderr)
    conns.sort(key=lambda c: c[1], reverse=True)  # by arrival time, descending
    footpaths = build_footpaths(stops)
    print(f"{sum(len(v) for v in footpaths.values()):,} walking transfers",
          file=sys.stderr)

    path = find_rentals(args.input)
    listings = load_listings(path)
    print(f"{len(listings):,} listings from {path}", file=sys.stderr)
    near = listing_stops(listings, stops)

    # Stops you could walk to the office from, and the walk-the-whole-way option.
    stop_index = GridIndex(((lat, lng, sid) for sid, (lat, lng) in stops.items()),
                           cell_m=MAX_WALK_M)
    office_stops = list(stop_index.near(office[0], office[1], MAX_WALK_M))
    print(f"{len(office_stops):,} stops within {MAX_WALK_M} m of the office",
          file=sys.stderr)
    if not office_stops:
        sys.exit("no transit stops near the office — check the address")

    per_arrival = []
    for arrival in ARRIVALS:
        target_s = hms_to_s(arrival + ":00")
        seed = {}
        for d, sid in office_stops:
            t = target_s - walk_seconds(d)
            if t > seed.get(sid, -1e18):
                seed[sid] = t
        tau = scan(conns, footpaths, seed)
        got = {}
        for lid, lat, lng in listings:
            d_office = haversine_m((lat, lng), office)
            walk_only = walk_seconds(d_office) if d_office <= 2 * MAX_WALK_M else None
            m = door_to_door(near[lid], tau, target_s, walk_only)
            if m is not None:
                got[lid] = m
        per_arrival.append(got)
        print(f"  arrive {arrival}: {len(got):,} listings reachable", file=sys.stderr)

    minutes = {}
    for lid, _, _ in listings:
        vals = [a[lid] for a in per_arrival if lid in a]
        if vals:
            minutes[lid] = round(statistics.median(vals), 1)

    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "office"
    out_path = args.output or f"commute-{slug}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({
            "office": {"label": label, "lat": office[0], "lng": office[1]},
            "date": day.isoformat(),
            "arrivals": list(ARRIVALS),
            "minutes": minutes,
        }, fh, separators=(",", ":"))

    vals = sorted(minutes.values())
    print(f"\n{len(minutes):,} of {len(listings):,} listings reachable", file=sys.stderr)
    if vals:
        def pct(p):
            return vals[min(len(vals) - 1, int(len(vals) * p))]
        print(f"  min {vals[0]:.0f}  p25 {pct(.25):.0f}  median {pct(.5):.0f}  "
              f"p75 {pct(.75):.0f}  max {vals[-1]:.0f}  (minutes)", file=sys.stderr)
        print(f"  within 30 min: {sum(1 for v in vals if v <= 30):,}", file=sys.stderr)
    print(f"\n-> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
