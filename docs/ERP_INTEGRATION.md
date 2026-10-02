# ERP & Channel Integration Guide

The platform is a **decision layer beside the Acintyo ERP**. The ERP stays the system of record. The platform reads from it and writes back **only drafts**: purchase-order drafts, price/offer rules and invoice-line term flags. Every write carries a reason code, and a human can override it.

There are three integration surfaces:

| Surface | Direction | Who builds it | Effort |
|---|---|---|---|
| ERP data feed (9 read resources) | ERP → platform | ERP team exposes APIs **or** drops nightly CSVs | Config only: `config/erp.yaml` |
| Draft write-back (PO draft, price rule) | platform → ERP | ERP team exposes 2 POST endpoints | Config only: `config/erp.yaml` |
| Real-time channel events (portal, app, WhatsApp) | channels → platform | Portal/app team calls 4 platform endpoints | Small portal change |

---

## 1. Connect the ERP: edit one file

`config/erp.yaml` → set `connector: rest`, then for each resource set the ERP `path` and map ERP field names (right) to canonical names (left). Dotted paths reach nested JSON (`stock.closing.qty`). Secrets come from environment variables (`ERP_BASE_URL`, `ERP_API_TOKEN`, …), never from the file.

Verify without running anything else:

```bash
ERP_CONNECTOR=rest ERP_BASE_URL=https://erp.internal ERP_API_TOKEN=... \
  python -m smart_inventory.cli check-erp
```

It prints row counts per resource and any canonical fields that came back empty.

**No API yet?** Use `connector: csv` and have the ERP drop nightly extracts into `data/erp_drop/` with the canonical column names below. Drafts are written to `data/erp_outbox/*.jsonl` for ERP import. At ~300 orders/day this is acceptable (architecture doc §7).

**Reference implementation.** `scripts/reference_erp_server.py` is a working ERP API that matches `erp.yaml` exactly, including pagination and the write endpoints. Hand it to the ERP team as the contract. `tests/test_rest_connector.py` proves a full decision cycle runs through it.

### Read resources (canonical fields)

Required fields are **bold**. Incremental resources accept `updated_since=<ISO timestamp>`.

| Resource | Canonical fields | Notes |
|---|---|---|
| `warehouses` | **warehouse_id**, name, working_capital_cap_inr | HYD01, NMB01… every table keys on warehouse |
| `skus` | **sku_id**, **name**, composition, pack, pack_size, **mrp**, ptr, **cost**, schedule, manufacturer, shelf_life_days, moq, normal_discount_pct, margin_floor_pct, price_ceiling | `composition` powers pooling and alternatives; `price_ceiling` = DPCO/NPPA ceiling |
| `retailers` | **retailer_id**, name, city, is_top_account, credit_status, drug_licence_no | `is_top_account` drives sourcing priority and stock economics |
| `suppliers` | **supplier_id**, name, **approved**, **licence_valid_until**, **gst_compliant**, lead_time_days, lead_time_std_days, return_rights, channel, contact | approved/licence/GST are **hard gates** |
| `order_lines` (incremental) | **order_id**, line_id, **retailer_id**, **sku_id**, **warehouse_id**, **qty**, price, discount_pct, channel, **ts**, term_flag | demand history |
| `bounces` (incremental) | **bounce_id**, order_line_id, **sku_id**, **retailer_id**, **warehouse_id**, qty, **reason_code**, outcome, **ts** | blank reason code fails the daily data-quality check |
| `inventory` | **warehouse_id**, **sku_id**, **batch**, **expiry**, **qty**, cost, inward_date, supplier_id, on_hold | batch-level snapshot. Batch + expiry are required on 100% of rows |
| `open_pos` | **warehouse_id**, **sku_id**, **qty**, po_id, supplier_id, expected_date | subtracted from suggested PO |
| `supplier_prices` | **supplier_id**, **sku_id**, **price**, scheme, available_qty, batch, expiry | scheme as `10+1` or `5%` |

### Write-back (drafts only)

| Resource | Payload (canonical) | ERP returns |
|---|---|---|
| `po_draft` | warehouse_id, supplier_id, sku_id, qty, unit_cost, reason_code, status=`DRAFT` | PO reference (`response_ref_path`) |
| `price_rule` | sku_id, warehouse_id, batch, discount_pct, **term_flag** (`NORMAL` / `SPECIAL_NON_RETURNABLE`), valid_from, valid_to | rule reference |
| `live_stock` (optional, read) | sku_id, warehouse_id → qty | used by `/availability` in real time |

### GRB / credit-note block (ERP change required)

Special-discount lines must not be returnable. The ERP credit-note/GRB flow must call:

```
GET /api/v1/returns/check?order_id=INV123&line_id=1
→ {"allowed": false, "reason": "SPECIAL_NON_RETURNABLE_TERMS", "offer_id": "..."}
```

The ERP must also store `term_flag` on the invoice line. This depends on an **ASSUMPTION** in the architecture doc (that the ERP can enforce a line-level return block) and needs **legal sign-off on invoice/T&C wording before launch.**

---

## 2. Portal, Local app and WhatsApp ordering

Every channel must emit **the same events**. Use the `portal` role API key in the `X-API-Key` header.

### Events: `POST /api/v1/events` (single or array; idempotent on `event_id`)

```json
{"event_id": "app-7f3a-1", "type": "bounce_candidate", "retailer_id": "RET0042", "sku_id": "SKU00123",
 "warehouse_id": "HYD01", "qty": 2, "order_id": "ORD991", "line_id": "3", "channel": "app"}
```

| type | When | Platform response |
|---|---|---|
| `order_line` | line confirmed | stored as demand. Special-term lines **must** carry the `offer_id` shown at checkout |
| `bounce_candidate` | ERP shows zero available stock | **real-time sourcing**: `retailer_message` = `AVAILABLE_ON_REQUEST` (+`eta_hours`) / `CHECKING_SUPPLIERS` / `NOT_AVAILABLE` |
| `search`, `cart_add`, `ask` | retailer intent | uncensored demand signal |

**Replace the "Product Not Available" message** with the `retailer_message` state:
- `AVAILABLE_ON_REQUEST` → "Available on request, ETA N hours" (hold the order)
- `CHECKING_SUPPLIERS` → "Checking authorised suppliers; we'll confirm shortly"
- `NOT_AVAILABLE` → tell the retailer once, with a callback option (the platform sends the WhatsApp message)

### Availability: `GET /api/v1/availability?sku_id&warehouse_id&qty`
Returns `IN_STOCK` / `AVAILABLE_ON_REQUEST` (+ETA) / `NOT_AVAILABLE` / `COMPLIANCE_HOLD`, plus same-composition alternatives **as information only**. Substitution is the pharmacist's decision. It **never errors**: on internal failure it returns `state: UNKNOWN, fail_open: true`. The portal must then fall back to normal ERP stock behaviour.

### Checkout: `GET /api/v1/offers?retailer_id&sku_id&warehouse_id`
Returns the two transparent options on eligible slow-moving SKUs:
- `NORMAL`: normal discount, normal return/GRB terms
- `SPECIAL`: higher discount, **non-returnable** (`returnable: false`, `max_qty` respects the retailer's special-term exposure cap)

### WhatsApp inbound: `POST /api/v1/webhooks/whatsapp`
Point the Gupshup/AiSensy inbound webhook here (`{"from": "+9198…", "text": "…"}`). Messages from a known supplier number are parsed by the Claude Supplier Parser agent into gated offers. Open sourcing requests for those SKUs are then re-evaluated automatically.

---

## 3. Roles & keys

`API_KEYS="key1:admin,key2:procurement,key3:management,key4:portal,key5:erp"`

| Role | Can |
|---|---|
| portal | events, availability, offers, sourcing request status, WhatsApp webhook |
| erp | events, GRB check, WhatsApp webhook |
| procurement | decisions + overrides, PO approve/edit/reject, price offers, sourcing, supplier offers, holds, agents |
| management | dashboards, decisions (read), Ask-AI, explanations |
| admin | everything, including run-cycle and autonomy mode |

Leave `API_KEYS` empty only for local development (everything is open).
