# Acintyo Smart Inventory: Predictive Distribution Intelligence Platform

**Minimum inventory → maximum availability → minimum bounce → better margin → lower expiry/GRB risk.**

This is a decision layer that sits **beside** the Acintyo ERP. It reads sales, bounces, stock, purchases and supplier data, then produces **one recommended action per SKU per warehouse per day**:

```
COMPLIANCE_HOLD → LIQUIDATE → SOURCE → STOCK → DISCOUNT → SELL → DON'T STOCK
       P0            P1         P2       P3       P4        P5       P6
```

It writes back **only drafts**: purchase orders, price/offer rules and invoice term flags. Each draft carries a reason code, and a human approves or overrides it. The ERP stays the system of record.

Built to the business spec (*Smart Inventory, Dynamic Discounting & Bounce Reduction Model*) and the architecture doc (*Predictive Distribution Platform Architecture v1.0*).

---

## What it does

| Spec requirement | Where | How |
|---|---|---|
| 1. SKU movement classification (Fast / Medium / Slow / One-time / Critical hard-to-source), continuously updated | `engines/classifier.py` (E2) | Syntetos-Boylan ADI/CV² on 180 days of true demand (fulfilled + lost), business overlays, 14-day hysteresis so SKUs don't flip daily, composition pooling for new SKUs |
| 2. Dynamic discount strategy | `engines/pricing.py` (E7) | Fast movers: margin-leakage detection → cohort **price test with holdout**, never a blind cut. Slow/near-expiry: tiered special-lot discounts capped by margin floor and MRP/DPCO ceiling |
| 3. Special discount = non-returnable | `pricing.checkout_options`, `/returns/check` | Two options at checkout (Normal + GRB terms vs Special + Non-Returnable), term flag on the invoice line, ERP GRB block endpoint, per-retailer exposure cap |
| 4. Bounce intelligence | `engines/signal.py` (E1) | Per SKU: 30/60/90-day bounces, distinct retailers, regular/rare/one-time, single vs multi-retailer, external availability (Easy/Difficult/Shortage), value lost, **suppressed demand** estimate |
| 5. Bounced product sourcing | `engines/sourcing.py` (E5) | Real time: bounce → supplier search (API, price lists, WhatsApp-parsed, manual) → **hard compliance gates** → ranked by landed cost/ETA → retailer sees "Available on request, ETA N h" |
| 6. Bounce-to-stock decision | `engines/bounce_to_stock.py` (E6) | Stock / Limited safety stock / Source-on-demand / Don't stock, with an explicit monthly economic test |
| 7. 1-day / 2-day forecast + daily suggested PO | `engines/forecast.py` (E3), `engines/replenishment.py` (E4) | Class-aware: champion/challenger GBM vs seasonal EWMA (Fast), Croston-SBA vs TSB (Medium), bounce-adjusted (Critical), no forecast (Slow/Sporadic) |
| 8. Dynamic safety stock | E4 | `SS = z·√(LT·σ²_d + μ²_d·σ²_LT)` + bounce-risk uplift; service level by class; MOQ, pack, shelf-life, supplier availability, **working-capital cap** |
| 9. Overall decision engine | `engines/orchestrator.py` (E8) | Priority resolution, one action per SKU-warehouse-day, inputs snapshot + hash + version for audit, graduated autonomy |
| 10. Management dashboard | `analytics.py`, web console | Inventory, Bounce, Purchase and Margin intelligence + KPIs |

The worked example in the spec reproduces exactly (`tests/test_engines.py::test_spec_example_table`):

| SKU | Stock | 2-day forecast | Buffer | Suggested PO |
|---|---|---|---|---|
| Product A | 40 | 55 | 10 | **25** |
| Product B | 15 | 9 | 3 | **0** |
| Product C | 0 | 20 | 5 | **25** |

### AI agents (Claude): at the edges, never in the decision loop

Stock quantities, prices, discounts and PO values are **deterministic and auditable**. Claude (`claude-opus-5-5`, with server-side refusal fallbacks) handles the unstructured and human-facing work:

| Agent | Job | Without an API key |
|---|---|---|
| **Supplier Parser** | Supplier WhatsApp/email/price-list text → schema-validated offers → conservative SKU matching (never crosses manufacturer or pack) → hard gates → re-runs open sourcing | 503 with a pointer to manual entry |
| **Decision Explainer** | "Why was a PO raised for SKU X?" in **English or Telugu**, citing the decision's own inputs | Deterministic template explanation |
| **Insights** | Natural-language questions over the dashboards (read-only tools) | 503 |
| **Operations** | Runs the daily cycle, sweeps sourcing, checks approvals and data quality, sends the morning WhatsApp briefing. **Cannot** approve POs or change prices | Same runbook, executed deterministically, templated briefing |

The platform runs fully without Claude. Agents add speed and convenience, not correctness.

---

## Quick start

### 1. Local demo on synthetic data (5 minutes, no ERP, no key)

```bash
pip install -r requirements.txt
PYTHONPATH=. python scripts/run_demo.py                       # builds data/demo.db with 7 days of history
DATABASE_URL=sqlite:///data/demo.db PYTHONPATH=. uvicorn smart_inventory.main:app --port 8000
# open http://localhost:8000   (API docs at /docs)
```

The mock ERP generates 400 SKUs × 2 warehouses with every archetype in the spec: fast, medium, slow, sporadic, hard-to-source, non-moving, near-expiry, plus suppliers with compliance defects.

### 2. Production (Docker)

```bash
cp .env.example .env            # set ERP_*, API_KEYS, ANTHROPIC_API_KEY, WhatsApp webhook
vi config/erp.yaml              # map ERP endpoints + field names (see docs/ERP_INTEGRATION.md)
docker compose up -d --build    # postgres + api (port 8000) + scheduler
docker compose exec api python -m smart_inventory.cli check-erp      # verify every ERP resource
docker compose exec api python -m smart_inventory.cli run-cycle --full-sync
```

### 3. Connect the ERP: configuration only

1. `config/erp.yaml` → `connector: rest`, endpoint paths, field mapping (ERP names → canonical names)
2. `.env` → `ERP_BASE_URL`, `ERP_API_TOKEN` (or API key / basic auth)
3. `python -m smart_inventory.cli check-erp`

No API yet? Use `connector: csv` with nightly extracts. `scripts/reference_erp_server.py` is a working implementation of the ERP side to hand to the ERP team as the contract. Full details are in **[docs/ERP_INTEGRATION.md](docs/ERP_INTEGRATION.md)**.

---

## Daily cycle (IST)

| Time | Job |
|---|---|
| continuous | event ingestion, real-time sourcing, intraday ERP sync + sourcing sweep every 15 min |
| 23:30 | ERP cut-off → sync → data quality → E1 bounce → E2 classify → E3 forecast → E6 bounce-to-stock → E4 buffers + PO → E7 pricing → E8 orchestrator |
| 05:30 | alert to procurement if the cycle has not succeeded |
| 06:30 | publish approved / auto-approved drafts to ERP, operations agent sends the briefing |

The cycle is **idempotent**: a re-run replaces machine proposals but never touches anything a human approved, rejected, overrode or pushed. Blocking data-quality failures (missing reason codes, missing batch/expiry, stale feeds) **halt** the cycle instead of producing decisions on bad data.

## Graduated autonomy (`config/policy.yaml → autonomy.mode`)

| Mode | Behaviour |
|---|---|
| `SHADOW` | Decisions computed and logged; nothing is written to the ERP. **Start here.** |
| `ASSIST` | Procurement approves / edits / rejects in the console; approval pushes a DRAFT PO to the ERP |
| `AUTO` | Fast-class PO lines are auto-approved **only** when the class's back-tested WAPE passes the gate and the line is under the value cap; everything else stays ASSIST |

All thresholds live in `config/policy.yaml`: classification, service levels, cover days, discount tiers, guardrails, working-capital caps and the schedule. Tune them after the Phase 0 baseline; no code change needed.

---

## API overview

Interactive docs are at `/docs`. Auth uses the `X-API-Key` header, with roles admin / procurement / management / portal / erp.

| Area | Endpoints |
|---|---|
| Channels (portal/app/WhatsApp) | `POST /api/v1/events` · `GET /availability` · `GET /offers` · `POST /webhooks/whatsapp` |
| ERP | `GET /returns/check` (GRB block) |
| Procurement | `GET /decisions` · `POST /decisions/{id}/override` · `GET /po-drafts` · `POST /po-drafts/{id}/approve` · `/reject` · `/approve-bulk` · `GET/POST /price-offers/{id}/approve` · `GET /sourcing/queue` · `POST /sourcing/requests/{id}/{action}` · `POST /supplier-offers` · `POST /supplier-offers/parse` · `POST/DELETE /compliance/holds` |
| Management | `GET /dashboard/{kpis,inventory,bounce,purchase,margin}` · `GET /skus/{id}` |
| Agents | `POST /agents/ask` · `GET /agents/explain/{decision_id}?lang=te` · `POST /agents/ops/run` |
| Admin | `POST /admin/run-cycle` · `PUT /admin/policy/autonomy` · `GET /admin/jobs` · `/data-quality` · `/notifications` · `/erp/health` |

## CLI

```bash
python -m smart_inventory.cli init-db | sync [--full] | run-cycle [--date] [--publish] | backfill --days 14
python -m smart_inventory.cli ops-agent [--run-cycle] [--notify] | scheduler | serve | check-erp
```

## Tests

```bash
PYTHONPATH=. pytest -q                                                          # SQLite
DATABASE_URL=postgresql+psycopg2://user@host/db PYTHONPATH=. pytest -q          # PostgreSQL
```

57 tests cover:
- every engine rule, including the spec's worked example
- the compliance gates
- the full cycle against the mock ERP **and** through the REST connector + reference ERP API
- the real-time bounce → sourcing → retailer message flow
- PO approve/edit/reject and push to ERP, with re-runs preserving human decisions
- special-term checkout, the GRB block and the exposure cap
- the P0 compliance hold
- the agent tool loop (scripted Claude), agent degradation without Claude, supplier-message parsing, and role-based auth

## Project layout

```
config/            policy.yaml (business rules) · erp.yaml (ERP mapping)
smart_inventory/
  integrations/    ERP connectors (rest, csv, mock) · WhatsApp notifications
  pipeline/        ingest · data quality · daily cycle · scheduler
  engines/         E1 signal · E2 classifier · E3 forecast · E4 replenishment · E5 sourcing
                   E6 bounce-to-stock · E7 pricing · E8 orchestrator · compliance gates
  agents/          Claude: supplier parser · explainer · insights · operations
  api/             REST API (channels + console) · web/ console
  analytics.py     KPIs and the four management views (single source of truth)
scripts/           run_demo.py · reference_erp_server.py
docs/              ERP_INTEGRATION.md · ROLLOUT.md
tests/
```

See **[docs/ROLLOUT.md](docs/ROLLOUT.md)** for the go-live plan, KPIs and decisions still open.
