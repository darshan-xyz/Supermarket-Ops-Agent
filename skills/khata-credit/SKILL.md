---
name: khata-credit
description: >
  Use when the owner records credit sales, collects debt payments, or checks
  how much a customer owes. Trigger phrases: "udhaar de do", "Ramu ko credit
  do ₹200", "Ramesh kadan la podu", "Sunita ne ₹500 diya", "Ramesh ₹500 kuduthaaru",
  "udhaar chukao", "Ramu ka kitna baaki hai", "Ramesh balance enna", "kadan evvalavu",
  "khata dikhao", "who owes me money", "list all debtors", "udhaar waale",
  "record payment", "payment mila", "balance check karo", "ரமேஷ் கடன் எவ்வளவு".
---

## WHEN to use

Activate whenever the owner:
- Extends credit to a customer without billing through the cart (`khata_add_credit`)
- Receives a debt repayment from a customer (`khata_record_payment`)
- Wants to see a customer's outstanding balance or full statement
- Wants a list of all customers with outstanding balances

## Tool call sequence

### Give credit (udhaar dena)

```
khata_add_credit(customer_name, amount_rupees, note?)
```

- `customer_name`: customer name as the owner states it (case-insensitive match in repo)
- `note`: optional short reason (e.g. "monthly tab", "extra dal")

### Record a payment received

```
khata_record_payment(customer_name, amount_rupees, note?)
```

If the amount exceeds the outstanding balance, the tool raises `NeedsConfirmation`. Surface this clearly:

> "Ramu ka baaki ₹150 hai, aap ₹500 record karna chahte hain — confirm?"

On owner confirmation, call again with `confirm_overpay=true`.

### Check balance

```
khata_balance(customer_name)
```

Returns the balance (positive = owes the shop, negative = advance) — relay it with the ₹ figure.

### View statement

```
khata_statement(customer_name, limit?)
```

Default `limit` is 20 entries. Show the date, type (credit/payment), amount, and running balance.

### List all debtors

```
list_khata_debtors()
```

Returns all customers with a positive balance. Useful for end-of-day reconciliation.

## Handling errors

| Error | Action |
|---|---|
| `CustomerNotFound` | Relay the message; ask owner if the name is correct or if it's a new customer |
| `NeedsConfirmation` | Always surface to the owner with the exact figures before re-calling with `confirm_overpay=true` |

## Tips

- A credit sale through the billing cart (`payment_mode="khata"`) automatically posts the khata debit — no need to call `khata_add_credit` separately.
- Khata records persist across sessions; they survive `/new`.
- When checking balance, always state the direction clearly: "Ramu aapka ₹350 deta hai" vs "Ramu ka ₹50 advance hai".
