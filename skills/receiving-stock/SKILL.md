---
name: receiving-stock
description: >
  Use when the owner mentions stock arriving, adding a new product, or updating
  prices/MRP. Trigger phrases: "50 packets Maggi aaya", "50 packets Maggi vanthuchu",
  "new item add karo", "pudhu item add pannu", "stock aaya", "maal aaya", "stock vanthuchu",
  "add Amul Butter GST 12%", "naya product", "items came in", "received 2 dozen",
  "update MRP", "kitna stock hai", "kya khatam ho raha hai", "reorder karo",
  "what's running low", "சரக்கு வந்தது".
---

## WHEN to use

Activate whenever the owner:
- Reports fresh stock arriving ("50 packets Maggi came in, cost ₹12, MRP ₹14")
- Wants to add a brand-new SKU to the system ("new item: Amul Butter 100g GST 12% MRP ₹62")
- Asks about current stock levels or what to reorder
- Wants to update cost/MRP/sell price on an existing product

## DECISION: New product vs. restock

**Always disambiguate first.**

1. Call `search_products` with the product name.
   - If results come back → it is likely a **restock**. Show the candidate(s) and confirm with the owner before calling `receive_stock`.
   - If no results → it is a **new product**. Collect the required fields and call `add_product`.

Never assume. A mis-matched product ID creates ghost stock.

## Tool call sequence

### Restocking an existing product (Inward Shipments ONLY)

```
search_products(query="Maggi 70g")
  → confirm product with owner if multiple hits
receive_stock(product_id, quantity, unit, cost_rupees, mrp_rupees, sell_rupees)
```

`receive_stock` is strictly for inward incoming stock (adding stock). It can NEVER be used to reduce stock.
`sell_rupees` is optional; omit if the owner doesn't specify (the repo will default to the last sell price).

### Adjusting stock (Reductions, Damage, Spoilage, Physical Audit)

```
adjust_stock(
  product_id,
  quantity,
  unit?,                # optional, defaults to product unit
  mode="reduce",        # "reduce" for damage/spoilage/waste; "set" for physical audit count; "add" for audit surplus
  reason="damage",      # "damage" | "spoilage" | "audit_correction" | "theft" | "internal_use" | "expired"
  note?                 # optional notes
)
```

Use `adjust_stock` whenever stock needs to be reduced, discarded, or corrected:
- Damaged goods: `adjust_stock(product_id=X, quantity=5, mode="reduce", reason="damage", note="crushed in transit")`
- Spoiled / Expired: `adjust_stock(product_id=X, quantity=2, mode="reduce", reason="spoilage")`
- Audit physical count: `adjust_stock(product_id=X, quantity=30, mode="set", reason="audit_correction")`

### Updating product prices & catalog details (without restocking)

```
update_product(
  product_id,
  sell_rupees?,         # new selling price
  mrp_rupees?,          # new MRP
  cost_rupees?,         # new cost/purchase price
  reorder_qty?,         # new reorder level
  brand?, aliases?, gst_rate?
)
```

Use `update_product` when changing selling price, MRP, or cost when no stock arrived. Enforces Legal Metrology (`sell_rupees <= mrp_rupees`).

### Adding a new product

```
add_product(
  name, unit, gst_rate,
  mrp_rupees, sell_rupees,
  cost_rupees?,          # optional
  brand?, hsn_code?,
  is_loose?,             # true for atta/rice/dal sold by weight
  opening_stock?,        # qty owner has right now
  reorder_qty?           # qty that triggers a reorder alert
)
```

### Checking stock / inventory
```
list_inventory()               # all available products with health summary metrics and stock quantities
get_stock(product_id)          # single item
list_low_stock()               # all items at or below reorder point
reorder_suggestions()          # velocity-based suggestions
```

When listing store inventory, always present both the inventory health block (Total SKUs, out of stock, low stock, cost value, retail value) and the full product list together.

## GST slab guidance (informational — the repo applies the actual rate)

| Category | Typical GST |
|---|---|
| Loose staples — atta, rice, dal, sugar, fresh veg | 0 % |
| Packaged staples — packaged atta, packaged spices | 5 % |
| FMCG — biscuits, noodles, soap, shampoo | 12–18 % |

Ask the owner if unsure; never guess.

## Tips

- `unit` for `add_product` is the product's native unit (`"packet"`, `"kg"`, `"litre"`, `"piece"`).
- `quantity` and `unit` in `receive_stock` are in the owner's language; the repo converts to base.
- If the owner gives a carton count, ask for the per-unit weight/count before calling the tool.
- Always echo back the confirmed stock level after a successful receive.

## Expiry & FEFO (perishables)

- For perishables (milk, butter, bread, paneer…), pass `expiry` (`YYYY-MM-DD`) to `add_product` or `receive_stock`. Stock is tracked as batches and sold earliest-expiry-first automatically — no action needed at billing time.
- When the owner asks what's about to expire / spoil ("kya kharab hone wala hai", "expiry check"), call `list_expiring` (optional `within_days`, default 7). It also flags already-expired stock.
