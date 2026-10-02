# Rollout Plan, KPIs and Open Decisions

The software is complete. Value now depends on **data quality, a measured baseline and disciplined rollout**, not more code. This plan follows the phases in architecture doc §12, compressed because the build is done.

## Go-live sequence

| Step | Duration | What happens | Exit gate (do not skip) |
|---|---|---|---|
| **0. Connect + baseline** | 1–2 weeks | Map `config/erp.yaml`, run `check-erp`, load 12+ months of history (`run-cycle --full-sync`), mode = `SHADOW`. Record baseline KPIs from `/dashboard/kpis` | All data-quality checks pass for 7 consecutive days. Baseline signed off by CFO. **No KPI target goes to investors before this.** |
| **1. Bounce capture + sourcing** | 2–4 weeks | Portal/app/WhatsApp send `bounce_candidate` events; retailers see "Available on request". Supplier WhatsApp replies are parsed into offers | 100% of bounces carry a reason code; bounce-recovery rate measured weekly |
| **2. Classification + stocking candidates (shadow)** | 2 weeks | Procurement reviews the stocking-candidate list weekly. Compare its decisions vs the engine's | Class flip rate within limit; procurement agrees with ≥80% of STOCK/DON'T STOCK calls |
| **3. Suggested PO (assist)** | 4 weeks | Mode = `ASSIST`. Procurement approves/edits in the console by 07:00. Track PO acceptance + override reasons | PO acceptance ≥ 85%; Fast-class WAPE under the gate for 4 straight weeks |
| **4. Auto PO for Fast movers** | ongoing | Mode = `AUTO` for Fast class only, under value cap | Override rate on auto lines < 5%; rollback = set mode back to `ASSIST` |
| **5. Dynamic discount pilot** | 6–10 weeks | One retailer cluster; special lots + term flag at checkout + ERP GRB block; fast-mover **price tests with holdout** | Legal sign-off; liquidation recovery measured vs control; no rise in disputes |
| **6. Scale** | — | Navi Mumbai on the same platform (already warehouse-keyed); retailer-side inventory module | Second warehouse live with no code fork |

**Discounting is last on purpose.** It touches margin and retailer trust, and it depends on legal and ERP changes.

## KPIs (live in `/dashboard/kpis`)

| Area | Metric | Why it matters to the P&L / investors |
|---|---|---|
| Availability | Line fill rate, bounce rate, **bounce recovery rate** | Revenue captured; wallet share with pharmacies |
| Inventory | Inventory days, working capital as % of turnover, ageing buckets | Cash conversion; debt capacity |
| Risk | Near-expiry value (90d), liquidation recovery, supplier-return value | Expiry/GRB write-offs hit gross margin directly |
| Margin | Gross margin %, discount leakage on fast movers, special-lot uptake | Unit economics |
| Model health | WAPE + bias by class (champion vs challenger), class flip rate, PO acceptance, **override rate** | Shows where the rules are wrong; gates autonomy |
| Platform | Data-quality pass rate, job status, time to PO | Operational reliability |

## Where I challenged the source documents

1. **Flat +1–2% discount on slow movers will mostly not move stock.** The architecture doc says the same. The engine instead uses a configurable tier table by days-to-expiry and ageing (`pricing.tiers`). Calibrate it in the Phase 5 pilot against a control group.
2. **"Discount fast movers less" is a revenue risk, not a free margin win.** Fast movers are the price-benchmark SKUs retailers compare across distributors. The engine detects leakage but only proposes a **cohort price test with holdout**, measured on volume and wallet share. It never cuts discounts automatically.
3. **Bounce-to-stock economics (architecture doc E6)** puts `sourcing_delay_cost` on the cost side of "stock". It is a cost of *not* stocking, so it is implemented as a stocking benefit (`engines/bounce_to_stock.py` docstring). Please confirm with procurement.
4. **"Every bounce = lost demand" understates the truth.** Retailers stop asking for what we never have. The engine adds a suppressed-demand estimate from each retailer's ordering cadence. Treat it as directional until validated against search/cart events.
5. **Recovered bounces are not "hard to source".** The Critical class counts only bounces that outside sourcing failed to recover. Counting all bounces mislabelled fast movers as critical in testing.
6. **Daily SKU-level forecasts are noisy at ~300 orders/day.** On synthetic data, Fast-class WAPE is ~0.45 and intermittent classes are much higher. That is normal and is why buffers, class-specific methods and the AUTO gate exist. Do not quote forecast accuracy to investors as a single blended number.
7. **Non-returnable special terms are a legal and ERP dependency.** The platform stores the flag and exposes `/returns/check`, but the ERP must block GRB on flagged lines and the invoice/T&C wording needs legal sign-off (Diddi & Co.) before launch. The per-retailer exposure cap (`pricing.max_retailer_special_exposure_inr`) protects retailer trust.

## Decisions needed (owner)

| Decision | Owner | Default in config |
|---|---|---|
| ERP integration pattern: API/CDC vs nightly CSV extract | CTO | `connector: mock` |
| Can the ERP enforce a line-level GRB block? | CTO | endpoint ready: `/returns/check` |
| Working-capital cap per warehouse per daily PO | CFO | HYD01 ₹15 L, default ₹15 L |
| Service levels by class | Procurement head | Fast 95%, Medium 90%, Critical 98% |
| Discount tier table + margin floor + liquidation recovery floor | CFO + Sales | tiers in `policy.yaml`, recovery 85% of cost |
| Max special-term exposure per retailer | Sales head | ₹50,000 / 90 days |
| Top-account list (drives sourcing priority, stocking economics) | Sales head | ERP `key_account` flag |
| Claude API key + WhatsApp Business webhook | CTO | optional: the platform runs without them |

## Assumptions to validate in Phase 0

- Bounce / "not available" logs carry SKU, retailer, warehouse and a reason code.
- Supplier lead times, schemes and return rights exist in a system, not only in procurement's heads.
- Batch and expiry exist on every stock row (the data-quality check blocks the cycle otherwise).
- Manufacturer return rights cover most expiry exposure. This decides how aggressive liquidation can be: the engine always tries supplier return **before** discounting.

## Operating notes

- **Daily, procurement:** open the Purchase tab by 07:00, approve HIGH priority lines first, give a reason on every rejection. Rejection and override reasons are the feedback loop for tuning the rules.
- **Weekly, procurement + business owner:** review the stocking-candidate list, override reasons, class switches and the sourcing failure reasons.
- **Monthly:** review `policy.yaml` thresholds against KPIs; champion/challenger results per class are in `/dashboard/kpis → model_health`.
- **Rollback:** `PUT /api/v1/admin/policy/autonomy {"mode": "SHADOW"}` stops all ERP write-back immediately.
