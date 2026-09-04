"""Dataclasses returned by the repository and consumed by the tool layer."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import money
from .units import format_qty


@dataclass
class Product:
    id: int
    name: str
    brand: str | None
    aliases: str
    sku_code: str | None
    hsn_code: str | None
    unit: str
    is_loose: bool
    gst_rate: int
    cost_paise: int
    mrp_paise: int
    sell_paise: int
    stock_base: int
    reorder_base: int
    active: bool

    @property
    def stock_display(self) -> str:
        return format_qty(self.stock_base, self.unit)

    @property
    def reorder_display(self) -> str:
        return format_qty(self.reorder_base, self.unit)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "gst_rate": self.gst_rate,
            "mrp": money.format_inr(self.mrp_paise),
            "sell": money.format_inr(self.sell_paise),
            "cost": money.format_inr(self.cost_paise),
            "in_stock": self.stock_display,
            "hsn": self.hsn_code,
        }


@dataclass
class StockInfo:
    product: Product
    stock_display: str
    is_low: bool
    is_out: bool


@dataclass
class Batch:
    id: int
    product_id: int
    qty_base: int
    expiry: str | None
    cost_paise: int | None


@dataclass
class ExpiringBatch:
    name: str
    qty_display: str
    expiry: str
    days_left: int  # negative = already expired


@dataclass
class ReorderSuggestion:
    product: Product
    velocity_per_day: float
    days_of_cover: float
    suggested_qty_display: str
    reason: str


@dataclass
class CartLine:
    product_id: int
    name: str
    unit: str
    qty_base: int
    qty_display: str
    unit_sell_paise: int
    gst_rate: int
    line_total_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    available_base: int
    shortfall: bool  # requested more than currently in stock


@dataclass
class CartView:
    bill_id: int
    chat_id: int
    status: str
    customer_name: str | None
    payment_mode: str | None
    payment_ref: str | None
    lines: list[CartLine] = field(default_factory=list)
    subtotal_paise: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    round_off_paise: int = 0
    grand_total_paise: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class Bill:
    id: int
    invoice_no: str | None
    status: str
    chat_id: int
    customer_name: str | None
    payment_mode: str | None
    payment_ref: str | None
    subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    round_off_paise: int
    total_paise: int
    created_at: str
    finalized_at: str | None
    was_retry: bool = False  # True when finalize() returned an existing invoice


@dataclass
class BillDetail:
    bill: Bill
    lines: list[CartLine]
    slabs: list[dict]


@dataclass
class Customer:
    id: int
    name: str
    phone: str | None


@dataclass
class KhataEntry:
    id: int
    entry_type: str  # debit | credit
    amount_paise: int
    note: str | None
    bill_id: int | None
    created_at: str


@dataclass
class KhataState:
    customer: Customer
    balance_paise: int  # positive => customer owes the shop
    last_entry: KhataEntry | None = None


@dataclass
class ItemSales:
    product_id: int
    name: str
    qty_display: str
    revenue_paise: int
    count: int


@dataclass
class DailyReport:
    date: str
    bill_count: int
    gross_sales_paise: int
    total_tax_paise: int
    cgst_paise: int
    sgst_paise: int
    by_payment: dict[str, int]  # mode -> paise
    top_items: list[ItemSales]


@dataclass
class SalesReport:
    start: str
    end: str
    bill_count: int
    gross_sales_paise: int
    taxable_paise: int
    total_tax_paise: int
    by_payment: dict[str, int]
    by_day: list[tuple[str, int]]  # (date, gross_paise)
    top_items: list[ItemSales]


@dataclass
class GstReport:
    start: str
    end: str
    by_slab: list[dict]  # {rate, taxable, cgst, sgst}
    cgst_paise: int
    sgst_paise: int
    total_tax_paise: int


@dataclass
class StockHealth:
    total_skus: int
    out_of_stock: list[Product]
    low_stock: list[Product]
    inventory_cost_value_paise: int
    inventory_retail_value_paise: int


@dataclass
class ShopProfile:
    name: str | None
    gstin: str | None
    address: str | None
    phone: str | None
    state_code: str
    invoice_prefix: str
