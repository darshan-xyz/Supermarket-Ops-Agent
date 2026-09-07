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

Do this for every item the owner names.

**1. Single Catalog Match (Zero Confirmation — Direct Add):**
- If a search query resolves to **EXACTLY ONE** active product in the store catalogue (e.g. "butter" matches only `Amul Butter 100g`, "sugar" matches only `Loose Sugar`, "oil" matches only `Fortune Sunflower Oil 1L`, "toor dal" matches only `Loose Toor Dal`):
  - **THERE IS ZERO AMBIGUITY!**
  - **NEVER** ask confirmation questions like *"You only have Amul Butter 100g, should I bill this?"*.
  - Immediately add the item to the cart with the requested quantity!

**2. Unit-Aware Disambiguation (Loose vs Packaged):**
- When a name matches both a loose item and a packaged item (e.g. "atta" matches both `Loose Atta` [kg] and `Aashirvaad Atta 5kg` [packet]):
  - If the user specifies `kg` (e.g. "4 kg atta", "2 kg atta", "500g atta"):
    `Loose Atta` is the direct unit match because it is sold loose by kg. Add `Loose Atta` directly!
  - If the user specifies `packet` or `bag` (e.g. "1 packet atta", "2 bags atta"):
    `Aashirvaad Atta 5kg` is the direct unit match. Add `Aashirvaad Atta 5kg` directly!
  - ONLY ask for clarification if the unit is omitted or ambiguous (e.g. "bill 5 atta").

**3. Genuine Multi-Variant Disambiguation:**
- If a product has multiple active weight/pack variants in the catalog (e.g. `Maggi 70g` and `Maggi 10g`, `Aashirvaad Atta 1kg` and `5kg`):
  - If the owner names the item generically without specifying weight/size (e.g. "add 1 maggi"), ask which weight/size they want.
  - Once the owner confirms which variant they want, call `cart_add_item(product_id=..., confirm_variant=true)`.

**4. Progressive Multi-Item Billing (Never Drop Clear Items):**
- When the owner gives a multi-item order (e.g. "3 packet Maggi, 2 kg toor dal, 5 kg sugar, 4 kg atta, 2 packets of butter"):
  - **ALWAYS add all clear, unambiguous items to the cart immediately!**
  - Never leave the cart empty or stall the entire order because 1 item needs clarification.
  - Display the draft bill showing what has been added, and ask clarification ONLY for the single ambiguous item.

### Step 2 — Build the cart

```
cart_add_item(product_id, qty_display, unit?, product_name?, confirm_variant?, confirm_bulk?)
```

Repeat for each item. `qty_display` is the number the owner stated (e.g. `"2"`, `"0.5"`). `unit` is optional and defaults to the product's native unit.

**Bulk Quantities (> 1000) & Pasted Inventory Lists:**
- If any line item has quantity > 1000 (or total quantity > 1000):
  The tool enforces bulk confirmation. Ask the owner:
  *"The order seems to be bulk, is this the right quantity?"*
  Only proceed with `confirm_bulk=true` once the owner confirms.
- If the owner copy-pastes an inventory list, or requests bulk additions ("Bill all above items", "Half of all inventory", "Bill everything except X"):
  Use `cart_add_batch`:
  ```
  cart_add_batch(items=[{"product_name": "...", "quantity": ...}, ...], confirm_bulk=?)
  ```
  - For "bill all inventory": use available stock for each SKU.
  - For "half of all inventory": calculate 50% of available stock for each SKU (floor to whole integers for packaged discrete items).
  - NEVER refuse a bulk billing request or whole-store billing. NEVER say "cannot add entire store inventory to a bill as that would be thousands of items". Supermarket owners frequently process wholesale or bulk orders.

To adjust after adding:
```
cart_update_item(product_id, new_qty_display, unit?, confirm_bulk?)
cart_remove_item(product_id)
```

### Step 3 — Preview cart & Ask confirmation to edit or finalize (MANDATORY)

```
cart_view()
```

Always show the owner the cart summary (items, subtotal, GST, grand total, any warnings).
**BEFORE proceeding to payment selection or finalizing**, ALWAYS ask the owner:
**"Is this bill final, or would you like to edit the bill (add, update, or remove items)?"**

- If the owner wants to edit:
  - Add items: `cart_add_item` or `cart_add_batch`
  - Remove items: `cart_remove_item` (e.g. "drop the butter", "remove 1 maggi")
  - Update quantities: `cart_update_item` (e.g. "make it 6 Maggi", "change sugar to 3kg")
  Show the updated draft bill and ask confirmation again:
  *"Is this bill final, or would you like to make any other changes?"*

- ONLY when the owner confirms the bill is final (e.g. "Yes final", "it's final", "proceed", "done", "no changes"):
  Proceed to Step 4 (Payment selection).

### Step 4 — Set cashier, customer details & payment mode

```
cart_set_biller(employee_name)
cart_set_customer(name, phone?)
cart_set_payment(mode, reference?)   # mode: "cash" | "upi" | "card" | "khata"
```

1. **Cashier / Employee Attribution**:
   - The active cashier defaults to the store owner or preference `last_biller` / `active_cashier`.
   - Cashier attribution NEVER blocks adding items to the cart!
   - If an employee is starting a shift or ringing up, set the biller:
     `cart_set_biller(employee_name="...")`
   - Stored in store preferences so subsequent bills inherit this cashier for the shift.
2. **Customer Details (Name & 10-Digit Mobile)**:
   - Ask: *"May I have the customer's name and mobile number to track their purchase history and generate the bill?"*
   - Call `cart_set_customer(name="...", phone="...")`.
   - If phone is provided, it indexes their lifetime purchases, visits, and loyalty.
   - If quick walk-in cash customer declines phone, proceed as Walk-in.
3. **Payment Mode Selection**:
   - Ask: *"Please select the payment method: Cash, UPI, Card, or Khata (credit)."*
   - (If the owner already named the payment method earlier, apply it).
   - `khata` = sell on credit; it requires a customer (`cart_set_customer`) and posts a debit to their ledger on finalize. Consult the STORE CONTEXT / `get_preferences()` for `default_payment` and apply it when the owner doesn't say how they paid.

### Step 5 — Finalise

```
cart_finalize(payment_mode?, customer_name?, customer_phone?, employee_name?, payment_ref?, confirm_below_cost?)
```

This is the **only** step that commits stock and creates the invoice. Finalize once the bill is confirmed and payment mode is set.

### Step 6 — Track Customer Purchase History

```
customer_purchase_history(phone="...", name="...")
```
Use when the owner asks about a customer's buying history (e.g. *"what has 9845012345 bought?"*, *"purchase history of Ramesh"*, *"what are Anita's favorite products?"*).
Returns:
- Lifetime spend, total visit count, and average order value.
- Outstanding Khata balance (if any).
- Top / favorite purchased products and quantities.
- List of recent bills (invoice numbers, dates, payment modes, cashiers).

### Handling Skipped Payment Questions (MANDATORY)
When an open draft bill is awaiting payment confirmation, if the owner skips the payment question and asks something else (e.g. "show bill 1 of yesterday", "check stock", "show inventory"):
1. **FIRST**, ask the owner to confirm the payment method for the open draft bill (Total ₹X for items) so stock can be deducted.
2. **THEN**, immediately proceed to answer whatever the owner asked in the very same reply!

### Inspecting past bills ("show bill 2 of today", "bill 2 of Sep 3")

```
lookup_bill(bill_no?, date?, invoice_no?, bill_id?)
```
- Bill numbers reset each day.
- Always refer to bills with their date context: *"Bill #2 of Today"*, *"Bill #2 of September 3"*.
- If the owner asks for "bill 2" without a date, look up today first. If multiple exist across dates, ask for clarification.

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
- After finalise succeeds, relay the date-scoped reference (e.g. `Bill #1 of Today (5 Sep 2026)`), `invoice_no` (e.g. `INV-20260905-0001`), and grand total.
- Payment mode `"khata"` automatically posts a credit-ledger debit for the customer — tell the owner the new balance.
- The draft bill lives in the DB (survives restarts); the tools are thin, stateless adapters.

