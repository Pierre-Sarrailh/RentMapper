#!/usr/bin/env python3
"""Shared geometry helpers: great-circle distance and a coarse spatial index.

Used by fetch_grocery.py (dedupe), commute.py (stop lookup) and map_rentals.py
(nearest grocery). Stdlib only.
"""

import math

EARTH_R = 6371000  # metres

# Walking assumptions, shared so commute times and grocery distances agree.
# Straight-line distance understates a real walk, so scale it by a detour factor
# rather than pretending the crow flies down Yonge Street.
WALK_SPEED_MS = 4.8 * 1000 / 3600  # 4.8 km/h, an unhurried adult pace
WALK_DETOUR = 1.3


def haversine_m(a, b):
    """Great-circle distance in metres between two (lat, lng) pairs."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def walk_seconds(metres):
    """Seconds to walk a straight-line distance, including the detour factor."""
    return metres * WALK_DETOUR / WALK_SPEED_MS


class GridIndex:
    """Bucket points into fixed lat/lng cells for radius queries.

    The alternative is comparing every listing against every stop — 7,000 x 12,000
    haversine calls. Cells are sized from the query radius so `near()` only has to
    scan the 3x3 block around a point.
    """

    def __init__(self, points, cell_m=500):
        """points: iterable of (lat, lng, payload)."""
        self.cell_lat = cell_m / 111_320.0
        # Longitude degrees shrink with latitude; use the mid-latitude of the data.
        pts = list(points)
        mid_lat = sum(p[0] for p in pts) / len(pts) if pts else 43.7
        self.cell_lng = cell_m / (111_320.0 * max(0.1, math.cos(math.radians(mid_lat))))
        self.cells = {}
        for lat, lng, payload in pts:
            self.cells.setdefault(self._key(lat, lng), []).append((lat, lng, payload))

    def _key(self, lat, lng):
        return (int(lat / self.cell_lat), int(lng / self.cell_lng))

    def near(self, lat, lng, radius_m):
        """Yield (distance_m, payload) for every point within radius_m, unordered."""
        # Widen the block if the radius is larger than one cell.
        span_lat = int(radius_m / (self.cell_lat * 111_320.0)) + 1
        span_lng = max(span_lat, 1)
        ci, cj = self._key(lat, lng)
        for i in range(ci - span_lat, ci + span_lat + 1):
            for j in range(cj - span_lng, cj + span_lng + 1):
                for plat, plng, payload in self.cells.get((i, j), ()):
                    d = haversine_m((lat, lng), (plat, plng))
                    if d <= radius_m:
                        yield d, payload

    def nearest(self, lat, lng, radius_m):
        """Closest (distance_m, payload) within radius_m, or (None, None)."""
        best = min(self.near(lat, lng, radius_m), default=None, key=lambda x: x[0])
        return best if best else (None, None)
