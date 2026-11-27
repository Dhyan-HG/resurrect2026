"""Event schema, validation and an idempotent event store.

Design: every event carries a unique `event_id` and an event time `ts`
(fractional days). The store keeps at most one copy per id, sets malformed
events aside in a quarantine with the reason, and never mutates state
directly. State is always *rebuilt* from the stored events sorted by
(ts, event_id), so the result does not depend on arrival order, on
duplicates, or on how many times you replay the feed.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .catalog import Catalog

SOURCES = {"erp", "wms", "supplier", "sentinel"}
FEED_SOURCES = ("erp", "wms", "supplier")

# field -> kind. kinds: str, posint, nonnegint, num, bool?
SCHEMAS: dict[str, dict[str, str]] = {
    "order_created": {"order_id": "str", "product": "product", "qty": "posint", "crd": "num"},
    "order_cancelled": {"order_id": "str"},
    "po_created": {"po_id": "str", "material": "material", "qty": "posint", "expected": "num"},
    "po_date_changed": {"po_id": "str", "expected": "num"},
    "goods_received": {"po_id": "str", "material": "material", "qty": "posint"},
    "production_started": {"order_id": "str"},
    "order_shipped": {"order_id": "str"},
    "stock_count": {"material": "material", "counted": "nonnegint"},
    "supplier_message": {"po_id": "str", "text": "str"},   # free text, read by sentinel/ambiguity.py
    "heartbeat": {},
}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(ev: Any, catalog: Catalog) -> str | None:
    """Return None if the event is valid, else a short reason."""
    if not isinstance(ev, dict):
        return "not an object"
    eid = ev.get("event_id")
    if not isinstance(eid, str) or not eid.strip():
        return "missing event_id"
    etype = ev.get("type")
    if etype is None:
        return "missing type"
    if etype not in SCHEMAS:
        return f"unknown type {etype!r}"
    if not _is_num(ev.get("ts")) or ev["ts"] < 0:
        return "bad ts"
    if ev.get("source") not in SOURCES:
        return f"unknown source {ev.get('source')!r}"
    for fname, kind in SCHEMAS[etype].items():
        if fname not in ev:
            return f"missing {fname}"
        v = ev[fname]
        if kind == "str" and (not isinstance(v, str) or not v.strip() or len(v) > 2000):
            return f"bad {fname}"
        if kind == "num" and (not _is_num(v) or v < 0):
            return f"bad {fname}"
        if kind == "posint" and (not isinstance(v, int) or isinstance(v, bool) or v <= 0):
            return f"bad {fname}"
        if kind == "nonnegint" and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            return f"bad {fname}"
        if kind == "material" and v not in catalog.materials:
            return f"unknown material {v!r}"
        if kind == "product" and v not in catalog.products:
            return f"unknown product {v!r}"
    return None


def canonical(ev: dict) -> str:
    return json.dumps(ev, sort_keys=True, separators=(",", ":"))


class EventStore:
    """Idempotent store. `ingest` may be called any number of times with the
    same event; only the first valid copy counts."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.events: dict[str, dict] = {}
        self.arrival: dict[str, float] = {}
        self.quarantine: list[dict] = []
        self.stats = {"received": 0, "accepted": 0, "duplicates": 0,
                      "conflicts": 0, "quarantined": 0}
        self.last_seen: dict[str, float] = {}   # source -> max event ts accepted
        self._sorted: list[dict] | None = None

    def ingest(self, ev: Any, arrival: float | None = None) -> str:
        self.stats["received"] += 1
        reason = validate(ev, self.catalog)
        if reason:
            self.stats["quarantined"] += 1
            self.quarantine.append({"reason": reason, "arrival": arrival,
                                    "event": ev if isinstance(ev, dict) else repr(ev)})
            return "quarantined"
        eid = ev["event_id"]
        if eid in self.events:
            if canonical(self.events[eid]) == canonical(ev):
                self.stats["duplicates"] += 1
                return "duplicate"
            # Same id, different content: keep the first, flag the second.
            self.stats["conflicts"] += 1
            self.stats["quarantined"] += 1
            self.quarantine.append({"reason": "conflicting duplicate id",
                                    "arrival": arrival, "event": ev})
            return "conflict"
        self.events[eid] = dict(ev)
        self.arrival[eid] = arrival if arrival is not None else ev["ts"]
        self.stats["accepted"] += 1
        src = ev["source"]
        if ev["ts"] > self.last_seen.get(src, -1.0):
            self.last_seen[src] = ev["ts"]
        self._sorted = None
        return "accepted"

    def sorted_events(self) -> list[dict]:
        if self._sorted is None:
            self._sorted = sorted(self.events.values(), key=lambda e: (e["ts"], e["event_id"]))
        return self._sorted
