"""Independent reference calculator, written separately from sentinel/engine.py
on purpose (no shared code), used to check Sentinel's numbers.

Input: the plant's clean event list (each real event exactly once, no junk),
restricted to events that had arrived by time t. Output per material:
on_hand, incoming, allocated, net = on_hand + incoming - allocated.
"""


def reference_numbers(clean_events, catalog, t):
    seen = [ev for (arr, ev) in clean_events if arr <= t and ev["ts"] <= t]
    seen.sort(key=lambda e: (e["ts"], e["event_id"]))
    stock = {}
    po_qty, po_got = {}, {}
    po_mat = {}
    order_info, order_state = {}, {}
    for e in seen:
        kind = e["type"]
        if kind == "stock_count":
            stock[e["material"]] = e["counted"]
        elif kind == "goods_received":
            stock[e["material"]] = stock.get(e["material"], 0) + e["qty"]
            po_got[e["po_id"]] = po_got.get(e["po_id"], 0) + e["qty"]
        elif kind == "po_created":
            po_qty[e["po_id"]] = e["qty"]
            po_mat[e["po_id"]] = e["material"]
        elif kind == "order_created":
            order_info[e["order_id"]] = (e["product"], e["qty"])
            order_state[e["order_id"]] = "open"
        elif kind == "order_cancelled":
            if order_state.get(e["order_id"]) == "open":
                order_state[e["order_id"]] = "cancelled"
        elif kind in ("production_started", "order_shipped"):
            oid = e["order_id"]
            if order_state.get(oid) == "open":
                product, qty = order_info[oid]
                for m, per in catalog.products[product].bom.items():
                    stock[m] = stock.get(m, 0) - per * qty
                order_state[oid] = "started"
    result = {}
    for m in catalog.materials:
        incoming = 0
        for pid, q in po_qty.items():
            if po_mat[pid] == m:
                incoming += max(0, q - po_got.get(pid, 0))
        allocated = 0
        for oid, st in order_state.items():
            if st == "open":
                product, qty = order_info[oid]
                allocated += catalog.products[product].bom.get(m, 0) * qty
        oh = stock.get(m, 0)
        result[m] = {"on_hand": oh, "incoming": incoming, "allocated": allocated,
                     "net": oh + incoming - allocated}
    return result
