---
name: billing
description: >
  Use when the owner is making a sale, ringing up a customer, or managing the
  current cart. Trigger phrases: "bill banao", "ek kilo atta aur 2 Maggi",
  "bill podunga", "1 kg arisi aur 2 maggi", "customer ka bill", "sell karo",
  "add to bill", "remove ek item", "vennai eduthudunga", "payment cash hai",
  "UPI se diya", "finalize karo", "bill mudichidu", "bill ho gaya",
  "kitna total hua", "moththam evvalavu", "customer ka naam Ramu", "bill cancel karo",
  "பில் போடுங்க", "பில்லை முடித்துவிடு".
---

## WHEN to use

Activate whenever the owner is ringing up a sale:
- Adding one or more items to the current bill
- Editing quantities or removing an item
- Setting payment mode or customer name
- Finalising/closing the bill
- Viewing the running cart total

## Multi-turn cart flow (follow this order)

### Step 1 — Identify products

```
search_products(query="<name>")
```

Do this for every item the owner names. If a search returns multiple candidates, list them and ask which one. Never guess the `product_id`.

### Step 2 — Build the cart

```
cart_add_item(product_id, qty_display, unit?)
```

Repeat for each item. `qty_display` is the number the owner stated (e.g. `"2"`, `"0.5"`). `unit` is optional and defaults to the product's native unit.

To adjust after adding:
```
cart_update_item(product_id, new_qty_display, unit?)
cart_remove_item(product_id)
```

### Step 3 — Preview cart

```
cart_view()
```

Always show the owner the cart summary (items, subtotal, GST, grand total, any warnings) before finalising. Relay any `warnings` from the `CartView` clearly.

### Step 4 — Set payment mode and customer (optional)

```
cart_set_payment(mode, reference?)   # mode: "cash" | "upi" | "card" | "khata"
cart_set_customer(name)
```

`khata` = sell on credit; it requires a customer (`cart_set_customer`) and posts a debit to their ledger on finalize. Consult the STORE CONTEXT / `get_preferences()` for `default_payment` and apply it when the owner doesn't say how they paid.

### Step 5 — Finalise

```
cart_finalize(confirm_below_cost?)
```

This is the **only** step that commits stock and creates the invoice. Do not call it speculatively. It is idempotent (a retry returns the same invoice — no double charge), so you never need to manage keys yourself. Pass `confirm_below_cost=true` only after the owner confirms a loss-making price.

## Handling errors

| Error | Action |
|---|---|
| `InsufficientStock` | Report exactly how much stock is available; ask owner to reduce qty or skip the item |
| `BelowCostSale` | Report the warning with cost and sell price; ask owner to confirm before calling `cart_finalize(confirm_below_cost=True)` |
| `EmptyCart` | Let owner know the cart is empty |
| `ValidationError` | Surface the message; owner must correct input |

Never suppress or silently retry an error that requires owner confirmation.

## Tips

- Only `cart_finalize` is irreversible (creates the invoice and deducts stock).
- If the owner asks to cancel mid-bill or start over, call `cart_cancel()`.
- After finalise succeeds, relay the invoice number (`invoice_no`) and grand total.
- Payment mode `"khata"` automatically posts a credit-ledger debit for the customer — tell the owner the new balance.
- The draft bill lives in the DB (survives restarts); the tools are thin, stateless adapters.
