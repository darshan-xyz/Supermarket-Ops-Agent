---
name: store-memory
description: >
  Use when the owner sets a standing preference or updates shop details. Trigger
  phrases: "default payment UPI rakho", "hamesha cash", "from now on UPI",
  "eppovum UPI nu vechiko", "language tamil nu maathu", "shop ka naam set karo",
  "GSTIN add karo", "invoice prefix change karo", "shop ki details",
  "apna address update karo", "mera GST number", "preferences dekho",
  "remember that I prefer cash".
---

## WHEN to use

Activate whenever the owner:
- Sets or changes a standing preference that should persist across sessions (default payment mode, language, reorder threshold, etc.)
- Updates shop identity details used on invoices (name, GSTIN, address, phone)
- Asks what preferences or shop profile are currently saved
- Says "from now on…", "always…", "hamesha…", "remember that…"

## Tool call sequence

### Set a preference

```
set_preference(key, value)
```

Common preference keys:

| Key | Example value | Purpose |
|---|---|---|
| `default_payment` | `"cash"` / `"upi"` / `"card"` | Pre-fill payment mode in billing |
| `language` | `"hi"` / `"en"` | Response language hint for the agent |
| `low_stock_alert` | `"true"` | Notify on low stock at session start |
| `reorder_cover_days` | `"7"` | Days of cover for reorder suggestions |

Ask the owner to confirm any preference before setting it, especially if the value changes an existing one.

### Read preferences

```
get_preferences()
```

Returns all key-value pairs currently saved.

### Update shop profile (for invoices)

```
set_shop_profile(
  name?,        # "Ram Kirana Store"
  gstin?,       # "27ABCDE1234F1Z5"
  address?,     # "123 Main Market, Pune"
  phone?,       # "9876543210"
  state_code?,  # "27" (Maharashtra)
  invoice_prefix?  # "INV" or "RKS"
)
```

Only pass the fields the owner is updating. The repo merges them.

### Read shop profile

```
get_shop_profile()
```

Shows the current shop name, GSTIN, address, etc. Use this when the owner asks "what's my shop profile?" or before generating the first invoice if the profile may be incomplete.

## Persistence

Preferences and shop profile are **stored in the database and survive `/new`** (session restarts). They are not chat memory — they are explicit store-level settings. The billing skill reads `default_payment` to pre-fill the payment mode; the invoice-PDF tool reads the shop profile to populate the invoice header.

## Tips

- GSTIN format: 15-character alphanumeric (e.g. `27ABCDE1234F1Z5`). If the owner gives an invalid format, surface the issue and ask them to verify.
- `state_code` is the 2-digit code from the GST council list (e.g. `"27"` for Maharashtra, `"07"` for Delhi).
- `invoice_prefix` appears before the sequential number on invoices (e.g. prefix `"RKS"` → `"RKS-00001"`).
- Do not store sensitive data (passwords, OTPs) as preferences.
- When the owner says "remember X", confirm back what you stored so there is no ambiguity.
