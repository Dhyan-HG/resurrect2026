"""Master data: materials, suppliers and products (bill of materials).

Master data is shared knowledge: the plant (simulator) and the Sentinel
system both see it. Supplier *reliability* is NOT master data: it lives only
inside the simulator, and Sentinel has to learn it from deliveries.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

PROD_DAYS = 2          # days from production start to shipment
EXPEDITE_PREMIUM = 0.30  # expedited purchase costs 30 % more per unit

MATERIAL_NAMES = [
    ("Steel sheet 2mm", "kg"), ("Aluminium bar", "kg"), ("Copper wire 1.5mm", "m"),
    ("ABS pellets", "kg"), ("M6 bolts", "pcs"), ("M6 nuts", "pcs"),
    ("Bearing 6202", "pcs"), ("PCB control board", "pcs"), ("Motor 0.5 kW", "pcs"),
    ("Power cord", "pcs"), ("Rubber gasket", "pcs"), ("Grey paint", "L"),
    ("Packaging carton", "pcs"), ("Foam insert", "pcs"), ("Glass panel", "pcs"),
    ("Thermostat", "pcs"), ("Heating element", "pcs"), ("Fan blade", "pcs"),
    ("Switch assembly", "pcs"), ("LED module", "pcs"), ("Hinge set", "pcs"),
    ("Plastic housing", "pcs"), ("M4 screws", "pcs"), ("Label set", "pcs"),
]
PRODUCT_NAMES = [
    "Table fan", "Room heater", "Water purifier", "Mixer grinder",
    "Exhaust fan", "Air cooler", "Ceiling light", "Geyser",
]
SUPPLIER_NAMES = [
    "Apex Metals", "Bharat Components", "Coastal Plastics", "Deccan Electricals",
    "Everest Packaging", "Fortune Fasteners",
]


@dataclass
class Supplier:
    id: str
    name: str


@dataclass
class Material:
    id: str
    name: str
    unit: str
    supplier: str
    lead_days: int
    expedite_lead_days: int
    unit_cost: float
    opening_stock: int
    daily_forecast: float = 0.0  # expected consumption per day (planner's forecast)


@dataclass
class Product:
    id: str
    name: str
    bom: dict[str, int]          # material id -> quantity per unit
    orders_per_day: float        # mean order arrivals per day
    mean_order_qty: float


@dataclass
class Catalog:
    materials: dict[str, Material] = field(default_factory=dict)
    products: dict[str, Product] = field(default_factory=dict)
    suppliers: dict[str, Supplier] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "materials": [vars(m) for m in self.materials.values()],
            "products": [vars(p) for p in self.products.values()],
            "suppliers": [vars(s) for s in self.suppliers.values()],
            "prod_days": PROD_DAYS,
        }


def generate_catalog(seed: int, n_materials: int = 24, n_products: int = 8) -> Catalog:
    rng = random.Random(seed * 7919 + 11)
    cat = Catalog()
    for i, name in enumerate(SUPPLIER_NAMES):
        sid = f"S{i + 1}"
        cat.suppliers[sid] = Supplier(sid, name)
    sids = list(cat.suppliers)

    for i in range(n_materials):
        name, unit = MATERIAL_NAMES[i % len(MATERIAL_NAMES)]
        mid = f"M{i + 1:02d}"
        lead = rng.randint(4, 12)
        cat.materials[mid] = Material(
            id=mid, name=name, unit=unit, supplier=rng.choice(sids), lead_days=lead,
            expedite_lead_days=max(2, math.ceil(lead / 2)),
            unit_cost=round(rng.uniform(2, 60), 2), opening_stock=0,
        )
    mids = list(cat.materials)

    # Every material is used by at least one product; products use 2-4 materials.
    pool = mids[:]
    rng.shuffle(pool)
    for i in range(n_products):
        pid = f"P{i + 1}"
        k = rng.randint(2, 4)
        chosen: list[str] = []
        while pool and len(chosen) < k:
            chosen.append(pool.pop())
        while len(chosen) < k:
            m = rng.choice(mids)
            if m not in chosen:
                chosen.append(m)
        cat.products[pid] = Product(
            id=pid, name=PRODUCT_NAMES[i % len(PRODUCT_NAMES)],
            bom={m: rng.randint(1, 3) for m in chosen},
            orders_per_day=round(rng.uniform(0.35, 0.65), 2),
            mean_order_qty=15.0,
        )
    # Leftover materials (if more materials than BOM slots) go to random products.
    for m in pool:
        rng.choice(list(cat.products.values())).bom[m] = rng.randint(1, 3)

    for m in cat.materials.values():
        m.daily_forecast = round(sum(
            p.orders_per_day * p.mean_order_qty * p.bom.get(m.id, 0)
            for p in cat.products.values()), 2)
        # Opening stock: lead time + 1-4 days of forecast demand (a lean plant).
        m.opening_stock = int(m.daily_forecast * (m.lead_days + rng.uniform(1, 4)))
    return cat
