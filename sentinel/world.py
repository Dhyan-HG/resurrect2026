"""The plant: a seeded discrete-day simulation that *is* the ground truth.

It owns the physical stock, the true supplier behaviour and the true ship
date of every order. It talks to Sentinel only through an event feed, and
that feed is deliberately imperfect:

  * suppliers deliver late (sometimes they warn, sometimes they don't)
  * demand spikes (a burst of orders for one product)
  * stock silently goes missing (revealed only by a later cycle count)
  * events are duplicated, delivered out of order, and junk events appear
  * one feed goes down for a while; its events arrive late in one burst

Everything planted is written to `truth` so the evaluation can score
Sentinel against it.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from .catalog import EXPEDITE_PREMIUM, PROD_DAYS, Catalog, generate_catalog
from .events import FEED_SOURCES, EventStore
from . import engine


@dataclass
class Faults:
    supplier_delays: bool = True
    demand_spikes: int = 2
    shrinkage: int = 3
    duplicate_rate: float = 0.03
    shuffle_rate: float = 0.10
    junk_rate: float = 0.02
    outage: bool = True

    @classmethod
    def none(cls) -> "Faults":
        return cls(False, 0, 0, 0.0, 0.0, 0.0, False)


@dataclass
class Truth:
    orders: dict = field(default_factory=dict)      # oid -> {created, crd, ship_day}
    pos: dict = field(default_factory=dict)         # po -> {expected, actual, notified_at, ...}
    spikes: list = field(default_factory=list)      # {day, product}
    shrinks: list = field(default_factory=list)     # {day, material, qty, count_day}
    outages: list = field(default_factory=list)     # {source, start, end}
    clean_events: list = field(default_factory=list)  # every real event once, with first arrival
    junk: int = 0
    dups: int = 0
    po_spend: float = 0.0
    stock_value_days: float = 0.0
    stockout_material_days: int = 0


class World:
    def __init__(self, seed: int = 1, days: int = 60, faults: Faults | None = None,
                 catalog: Catalog | None = None, safety_days: float = 1.0, cover_days: float = 6.0):
        self.seed = seed
        self.safety_days = safety_days   # baseline planner: reorder point = lead + safety days of forecast
        self.cover_days = cover_days     # ...and orders up to lead + cover days
        self.days = days
        self.faults = faults or Faults()
        self.cat = catalog or generate_catalog(seed)
        self.rng = random.Random(seed)
        self.frng = random.Random(seed * 31 + 7)   # feed-fault randomness, separate stream
        self.physical = {m: float(mat.opening_stock) for m, mat in self.cat.materials.items()}
        self.orders: dict[str, dict] = {}
        self.pos: dict[str, dict] = {}
        self.truth = Truth()
        self.store = EventStore(self.cat)
        self._pending: list = []      # heap of (arrival, seq, event)
        self._seq = 0
        self._eid = 0
        self._order_n = 0
        self._po_n = 0
        self.day = -1
        self.clock = 0.0
        # Hidden supplier reliability.
        self.reliab = {s: {"p_late": self.rng.uniform(0.15, 0.5), "notify": 0.5}
                       for s in self.cat.suppliers}
        self._plan_faults()
        for m, mat in self.cat.materials.items():   # opening balances
            self._emit("stock_count", 0.0, "wms", material=m, counted=mat.opening_stock)

    # ------------------------------------------------------------ setup
    def _plan_faults(self):
        f, r = self.faults, self.rng
        lo, hi = 8, max(9, self.days - 15)
        self.spike_days = {}
        for _ in range(f.demand_spikes):
            self.spike_days[r.randint(lo, hi)] = r.choice(list(self.cat.products))
        self.shrink_days = {}
        for _ in range(f.shrinkage):
            self.shrink_days.setdefault(r.randint(lo, hi), []).append(r.choice(list(self.cat.materials)))
        self.count_due: dict[int, list] = {}
        self.outage = None
        if f.outage:
            src = r.choice(["erp", "wms"])
            start = r.randint(lo, hi) + r.uniform(0.1, 0.6)
            self.outage = (src, start, start + r.uniform(0.75, 2.0))
            self.truth.outages.append({"source": src, "start": round(self.outage[1], 3),
                                       "end": round(self.outage[2], 3)})

    # ------------------------------------------------------------ feed
    def _emit(self, etype: str, ts: float, source: str, **fields) -> dict:
        self._eid += 1
        ev = {"event_id": f"E{self.seed}-{self._eid:06d}", "type": etype, "ts": round(ts, 4),
              "source": source, **fields}
        arrival = ts + self.frng.uniform(0.001, 0.02)
        if self.frng.random() < self.faults.shuffle_rate:
            arrival += self.frng.uniform(0.0, 0.5)
        if self.outage and source == self.outage[0] and self.outage[1] <= ts < self.outage[2]:
            arrival = max(arrival, self.outage[2] + self.frng.uniform(0.0, 0.05))
        self._push(arrival, ev)
        self.truth.clean_events.append((arrival, ev))
        if self.frng.random() < self.faults.duplicate_rate:
            self.truth.dups += 1
            self._push(arrival + self.frng.uniform(0.0, 1.0), dict(ev))
        if self.frng.random() < self.faults.junk_rate:
            self.truth.junk += 1
            self._push(arrival, self._junk(ev))
        return ev

    def _junk(self, ev: dict) -> dict:
        self._eid += 1
        bad = dict(ev, event_id=f"J{self.seed}-{self._eid:06d}")
        kind = self.frng.randint(0, 4)
        if kind == 0:
            bad["qty"] = -5
            bad["type"] = "goods_received"
        elif kind == 1:
            bad.pop("type", None)
        elif kind == 2:
            bad["material"] = "M99"
            bad["type"] = "stock_count"
        elif kind == 3:
            bad["ts"] = "yesterday"
        else:
            bad["type"] = "order_created"
            bad.pop("crd", None)
        return bad

    def _push(self, arrival: float, ev: dict):
        self._seq += 1
        heapq.heappush(self._pending, (arrival, self._seq, ev))

    def deliver_until(self, t: float):
        """Move every event that has arrived by time t into Sentinel's store."""
        while self._pending and self._pending[0][0] <= t:
            arrival, _, ev = heapq.heappop(self._pending)
            self.store.ingest(ev, arrival)
        self.clock = max(self.clock, t)

    def _heartbeats(self, d: int):
        for src in FEED_SOURCES:
            for k in range(4):
                ts = d + 0.02 + 0.25 * k + self.frng.uniform(0, 0.01)
                if self.outage and src == self.outage[0] and self.outage[1] <= ts < self.outage[2]:
                    continue
                self._emit("heartbeat", ts, src)

    # ------------------------------------------------------------ purchasing
    def create_po(self, material: str, qty: int, ts: float, expedite: bool = False,
                  source: str = "erp") -> str:
        mat = self.cat.materials[material]
        self._po_n += 1
        pid = f"PO{self._po_n:04d}"
        lead = mat.expedite_lead_days if expedite else mat.lead_days
        expected = math.floor(ts) + lead
        rel = self.reliab[mat.supplier]
        delay = 0
        notified_at = None
        if self.faults.supplier_delays and self.rng.random() < rel["p_late"] * (0.5 if expedite else 1.0):
            delay = self.rng.randint(2, 7)
            if self.rng.random() < rel["notify"]:
                notified_at = round(ts + max(0.3, 0.4 * (expected - ts)), 3)
        actual = expected + delay
        self.pos[pid] = {"material": material, "qty": qty, "expected": expected, "actual": actual,
                         "received": False, "notified_at": notified_at, "expedite": expedite}
        self.truth.pos[pid] = {"material": material, "qty": qty, "created": ts, "expected": expected,
                               "actual": actual, "delay": delay, "notified_at": notified_at,
                               "expedite": expedite, "source": source}
        self.truth.po_spend += qty * mat.unit_cost * (1 + EXPEDITE_PREMIUM if expedite else 1)
        self._emit("po_created", ts, "erp" if source == "erp" else source, po_id=pid,
                   material=material, qty=qty, expected=expected, expedite=expedite)
        return pid

    # ------------------------------------------------------------ one day
    def simulate_day(self, d: int):
        """Physical events of day d (everything except the planners)."""
        r = self.rng
        self.day = d
        self._heartbeats(d)
        # supplier delay notices due today
        for pid, p in self.pos.items():
            if p["notified_at"] is not None and math.floor(p["notified_at"]) == d and not p["received"]:
                self._emit("po_date_changed", p["notified_at"], "supplier", po_id=pid, expected=p["actual"])
        # receipts
        for pid, p in self.pos.items():
            if not p["received"] and p["actual"] == d:
                p["received"] = True
                self.physical[p["material"]] += p["qty"]
                self._emit("goods_received", d + 0.3, "wms", po_id=pid, material=p["material"], qty=p["qty"])
        # silent shrinkage + a count a few days later
        for m in self.shrink_days.get(d, []):
            q = math.floor(self.physical[m] * r.uniform(0.25, 0.45))
            if q > 0:
                self.physical[m] -= q
                cd = d + r.randint(2, 5)
                self.count_due.setdefault(cd, []).append(m)
                self.truth.shrinks.append({"day": d, "material": m, "qty": q, "count_day": cd})
        # cycle counts: scheduled ones + one routine count a day
        counts = list(dict.fromkeys(self.count_due.pop(d, []) + [r.choice(list(self.cat.materials))]))
        for m in counts:
            self._emit("stock_count", d + 0.4, "wms", material=m, counted=int(self.physical[m]))
        # new orders
        spike_p = self.spike_days.get(d)
        for pid, p in self.cat.products.items():
            n = _poisson(r, p.orders_per_day)
            if pid == spike_p:
                n += r.randint(8, 14)
                self.truth.spikes.append({"day": d, "product": pid})
            for _ in range(n):
                self._order_n += 1
                oid = f"SO{self._order_n:04d}"
                qty = max(1, int(round(r.gauss(p.mean_order_qty, 5))))
                crd = d + r.randint(6, 16)
                ts = d + r.uniform(0.05, 0.5)
                self.orders[oid] = {"id": oid, "product": pid, "qty": qty, "crd": crd,
                                    "created": ts, "started": None, "ship_day": None}
                self.truth.orders[oid] = {"product": pid, "qty": qty, "crd": crd,
                                          "created": round(ts, 4), "ship_day": None}
                self._emit("order_created", ts, "erp", order_id=oid, product=pid, qty=qty, crd=crd)
        # shipments of orders that finished production
        for o in self.orders.values():
            if o["started"] is not None and o["ship_day"] is None and o["started"] + PROD_DAYS == d:
                o["ship_day"] = d
                self.truth.orders[o["id"]]["ship_day"] = d
                self._emit("order_shipped", d + 0.8, "erp", order_id=o["id"])
        # production starts, by the documented rule (CRD priority, all-or-nothing)
        free = dict(self.physical)
        waiting = sorted((o for o in self.orders.values() if o["started"] is None),
                         key=lambda o: (o["crd"], o["created"], o["id"]))
        blocked = set()
        for o in waiting:
            bom = self.cat.products[o["product"]].bom
            ok = True
            for m, per in bom.items():
                need = per * o["qty"]
                take = min(need, max(0.0, free[m]))
                free[m] -= take
                if take + 1e-9 < need:
                    ok = False
                    if o["crd"] - PROD_DAYS <= d:
                        blocked.add(m)
            if ok:
                o["started"] = d
                for m, per in bom.items():
                    self.physical[m] -= per * o["qty"]
                self._emit("production_started", d + 0.6, "erp", order_id=o["id"])
        self.truth.stockout_material_days += len(blocked)
        self.truth.stock_value_days += sum(max(0.0, q) * self.cat.materials[m].unit_cost
                                           for m, q in self.physical.items())

    # ------------------------------------------------------------ planners
    def rop_planner(self, d: int):
        """The company's existing MRP rule (the baseline): order-up-to on
        inventory position = book on hand + on order - allocated."""
        t = d + 0.9
        self.deliver_until(t)
        state = engine.rebuild(self.store, t)
        incoming = {m: 0.0 for m in self.cat.materials}
        for p in state.pos.values():
            if p["status"] == "open":
                incoming[p["material"]] += p["qty"] - p["received"]
        alloc = {m: 0.0 for m in self.cat.materials}
        for o in state.orders.values():
            if o["status"] == "open":
                for m, per in self.cat.products[o["product"]].bom.items():
                    alloc[m] += per * o["qty"]
        for m, mat in self.cat.materials.items():
            position = state.on_hand[m] + incoming[m] - alloc[m]
            rop = mat.daily_forecast * (mat.lead_days + self.safety_days)
            target = mat.daily_forecast * (mat.lead_days + self.cover_days)
            if position < rop:
                self.create_po(m, int(math.ceil(target - position)), t + 0.001 * (int(m[1:])))


def _poisson(r: random.Random, lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= r.random()
        if p <= L:
            return k
        k += 1


def agent_planner(world: "World", d: int, approve=None, allow_expedite: bool = True,
                  opt: "engine.EngineOptions | None" = None) -> list[dict]:
    """Sentinel's agent proposes; `approve(proposal) -> bool` plays the human.
    Approved proposals become real POs in the plant. Returns the proposals."""
    from . import agent
    t = d + 0.95
    world.deliver_until(t)
    snap = engine.snapshot(world.store, t, opt)
    proj = {"orders": snap["orders"], "materials": snap["materials"], "suppliers": snap["suppliers"]}
    props = agent.propose(snap["_state"], proj, world.cat, opt, allow_expedite=allow_expedite)
    for p in props:
        if approve is None or approve(p):
            p["status"] = "approved"
            world.create_po(p["material"], p["qty"], t + 0.001, expedite=p["expedite"], source="erp")
        else:
            p["status"] = "rejected"
    return props
