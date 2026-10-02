"""Deterministic synthetic ERP for demos, tests and shadow-mode dry runs.

Generates a pharma distribution book with every archetype the business spec
names: fast/regular, medium, slow/rare, one-time/sporadic, critical
hard-to-source, non-moving and near-expiry stock, plus bounce history and
supplier price lists with compliance defects to exercise the hard gates.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .base import ERPConnector

MOLECULES = [
    "Paracetamol 650mg", "Amoxicillin 500mg + Clavulanic Acid 125mg", "Metformin 500mg", "Atorvastatin 10mg",
    "Telmisartan 40mg", "Pantoprazole 40mg", "Azithromycin 500mg", "Cetirizine 10mg", "Montelukast 10mg + Levocetirizine 5mg",
    "Glimepiride 2mg", "Amlodipine 5mg", "Rosuvastatin 10mg", "Losartan 50mg", "Ondansetron 4mg", "Domperidone 10mg + Rabeprazole 20mg",
    "Vitamin D3 60000IU", "Calcium 500mg + Vitamin D3", "Ferrous Ascorbate 100mg", "Insulin Glargine 100IU", "Levothyroxine 50mcg",
    "Clopidogrel 75mg", "Aspirin 75mg", "Ceftriaxone 1g Inj", "Ofloxacin 200mg + Ornidazole 500mg", "Diclofenac 50mg",
    "Aceclofenac 100mg + Paracetamol 325mg", "Sitagliptin 100mg", "Vildagliptin 50mg + Metformin 500mg", "Dapagliflozin 10mg",
    "Empagliflozin 25mg", "Linagliptin 5mg", "Enoxaparin 40mg Inj", "Tacrolimus 1mg", "Mycophenolate 500mg", "Hydroxychloroquine 200mg",
    "Methotrexate 7.5mg", "Pregabalin 75mg", "Gabapentin 300mg", "Duloxetine 30mg", "Escitalopram 10mg", "Sertraline 50mg",
    "Olanzapine 5mg", "Levetiracetam 500mg", "Sodium Valproate 500mg", "Carbamazepine 200mg", "Tamsulosin 0.4mg", "Finasteride 5mg",
    "Budesonide Respules 0.5mg", "Salbutamol Inhaler 100mcg", "Formoterol + Budesonide Inhaler", "Teneligliptin 20mg",
    "Cefixime 200mg", "Cefpodoxime 200mg", "Linezolid 600mg", "Doxycycline 100mg", "Fluconazole 150mg", "Itraconazole 100mg",
    "Ivermectin 12mg", "Albendazole 400mg", "Ursodeoxycholic Acid 300mg", "Rifaximin 550mg", "Sucralfate Suspension",
    "Lactulose Solution", "Ranolazine 500mg", "Ticagrelor 90mg", "Apixaban 5mg", "Rivaroxaban 20mg", "Dabigatran 110mg",
]
MANUFACTURERS = ["Sun Pharma", "Cipla", "Dr Reddy's", "Lupin", "Zydus", "Mankind", "Alkem", "Torrent", "Glenmark",
                 "Intas", "Abbott India", "Micro Labs", "Macleods", "Ajanta", "Eris"]
PACKS = ["10 Tab", "15 Tab", "10 Cap", "30 Tab", "1 Vial", "100 ml", "200 ml", "1 Inhaler", "4 Cap", "20 Respules"]
CITIES = ["Hyderabad", "Secunderabad", "Warangal", "Karimnagar", "Navi Mumbai", "Thane", "Panvel"]

ARCHETYPES = [  # (name, share)
    ("fast", 0.14),
    ("medium", 0.20),
    ("slow", 0.26),
    ("sporadic", 0.12),
    ("hard", 0.06),
    ("nonmoving", 0.07),
    ("bounce_only_regular", 0.05),
    ("bounce_only_rare", 0.10),
]


class MockERPConnector(ERPConnector):
    name = "mock"

    def __init__(self, cfg: dict[str, Any] | None = None, today: date | None = None, outbox: str | None = None):
        cfg = cfg or {}
        self.seed = int(cfg.get("seed", 42))
        self.n_skus = int(cfg.get("skus", 400))
        self.n_retailers = int(cfg.get("retailers", 150))
        self.n_suppliers = int(cfg.get("suppliers", 10))
        self.history_days = int(cfg.get("history_days", 200))
        self.today = today or date.today()
        self.outbox = Path(outbox or cfg.get("outbox", "./data/erp_outbox"))
        self.pushed: list[dict[str, Any]] = []
        self._data: dict[str, list[dict[str, Any]]] | None = None
        self.stock_overrides: dict[tuple[str, str], float] = {}

    # ---------------------------------------------------------------- public
    def fetch(self, resource: str, since: datetime | None = None) -> list[dict[str, Any]]:
        data = self._generate()
        rows = data.get(resource, [])
        if since and resource in ("order_lines", "bounces"):
            rows = [r for r in rows if r["ts"] >= since]
        return [dict(r) for r in rows]

    def push_po_draft(self, po: dict[str, Any]) -> str | None:
        ref = f"MOCK-PO-{uuid.uuid4().hex[:8].upper()}"
        self._record("po_draft", ref, po)
        return ref

    def push_price_rule(self, rule: dict[str, Any]) -> str | None:
        ref = f"MOCK-RULE-{uuid.uuid4().hex[:8].upper()}"
        self._record("price_rule", ref, rule)
        return ref

    def live_stock(self, sku_id: str, warehouse_id: str) -> float | None:
        if (sku_id, warehouse_id) in self.stock_overrides:
            return self.stock_overrides[(sku_id, warehouse_id)]
        return None  # fall back to the synced snapshot

    def _record(self, kind: str, ref: str, payload: dict[str, Any]) -> None:
        entry = {"kind": kind, "ref": ref, **payload}
        self.pushed.append(entry)
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            with open(self.outbox / f"{kind}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------- generator
    def _generate(self) -> dict[str, list[dict[str, Any]]]:
        if self._data is not None:
            return self._data
        rng = np.random.default_rng(self.seed)
        today = self.today
        start = today - timedelta(days=self.history_days)

        warehouses = [
            {"warehouse_id": "HYD01", "name": "Hyderabad Central", "working_capital_cap_inr": 1_500_000},
            {"warehouse_id": "NMB01", "name": "Navi Mumbai", "working_capital_cap_inr": 900_000},
        ]
        wh_weight = {"HYD01": 1.0, "NMB01": 0.55}

        # ---- suppliers: mostly compliant, with deliberate defects for the gates
        suppliers = []
        for i in range(self.n_suppliers):
            sid = f"SUP{i + 1:03d}"
            approved = i not in (7,)
            licence = today + timedelta(days=int(rng.integers(60, 700))) if i != 8 else today - timedelta(days=20)
            suppliers.append({
                "supplier_id": sid,
                "name": f"{['Sri Sai', 'Balaji', 'Venkateswara', 'Lakshmi', 'Mahalaxmi', 'Raghavendra', 'Om Sai', 'Ganesh', 'Sairam', 'Durga'][i % 10]} Pharma Distributors",
                "approved": approved,
                "licence_valid_until": licence.isoformat(),
                "gst_compliant": i != 9,
                "lead_time_days": float(rng.choice([0.5, 1, 1, 1, 2, 3])),
                "lead_time_std_days": float(rng.choice([0.2, 0.3, 0.5, 1.0])),
                "return_rights": bool(i % 3 == 0),
                "channel": ["api", "whatsapp", "whatsapp", "email", "manual"][i % 5],
                "contact": f"+9198{rng.integers(10_000_000, 99_999_999)}",
            })
        good_suppliers = [s["supplier_id"] for s in suppliers if s["approved"]]

        # ---- retailers
        retailers = []
        for i in range(self.n_retailers):
            retailers.append({
                "retailer_id": f"RET{i + 1:04d}",
                "name": f"{['Apollo', 'Sri Ram', 'Vijaya', 'Lakshmi', 'Care', 'Health Plus', 'MedPoint', 'Sai Krupa', 'Janata', 'Wellness'][i % 10]} Medicals {i + 1}",
                "city": CITIES[i % len(CITIES)],
                "is_top_account": i < max(5, self.n_retailers // 7),
                "credit_status": "ACTIVE" if i % 23 else "WATCH",
                "drug_licence_no": f"TS/HYD/20B-{10000 + i}",
            })
        retailer_ids = np.array([r["retailer_id"] for r in retailers])
        # Zipf-ish ordering propensity: top accounts order more
        r_weight = 1.0 / np.arange(1, self.n_retailers + 1) ** 0.6
        r_weight /= r_weight.sum()

        # ---- SKUs
        names, shares = zip(*ARCHETYPES)
        archetype = rng.choice(names, size=self.n_skus, p=np.array(shares) / sum(shares))
        skus = []
        for i in range(self.n_skus):
            mol = MOLECULES[i % len(MOLECULES)]
            mrp = float(np.round(rng.lognormal(mean=5.0, sigma=0.8), 2))
            mrp = float(min(max(mrp, 18.0), 4500.0))
            ptr = round(mrp * 0.80, 2)
            cost = round(ptr * float(rng.uniform(0.80, 0.86)), 2)
            pack_size = int(rng.choice([1, 1, 5, 10]))
            skus.append({
                "sku_id": f"SKU{i + 1:05d}",
                "name": f"{mol.split()[0]} {MANUFACTURERS[i % len(MANUFACTURERS)].split()[0]} {PACKS[i % len(PACKS)]}",
                "composition": mol,
                "pack": PACKS[i % len(PACKS)],
                "pack_size": pack_size,
                "mrp": mrp,
                "ptr": ptr,
                "cost": cost,
                "schedule": rng.choice(["H", "H", "H1", "G", "OTC"]),
                "manufacturer": MANUFACTURERS[i % len(MANUFACTURERS)],
                "shelf_life_days": int(rng.choice([540, 730, 730, 1095])),
                "moq": int(rng.choice([1, 1, 1, 5, 10])),
                "normal_discount_pct": float(rng.choice([7.0, 8.0, 8.0, 9.0, 10.0])),
                "margin_floor_pct": 2.0,
                "price_ceiling": round(mrp * 0.95, 2) if i % 9 == 0 else None,
                "_archetype": str(archetype[i]),
            })

        # ---- demand simulation
        order_lines: list[dict[str, Any]] = []
        bounces: list[dict[str, Any]] = []
        n_days = self.history_days
        order_counter = 0
        for sku in skus:
            arch = sku["_archetype"]
            base_qty = max(1, int(rng.integers(1, 6)) * sku["pack_size"])
            for wh in warehouses:
                w = wh_weight[wh["warehouse_id"]]
                if arch == "fast":
                    lam = rng.uniform(2.0, 6.0) * w
                    daily_lines = rng.poisson(lam, n_days)
                    bounce_p = 0.03
                elif arch == "medium":
                    p = rng.uniform(0.18, 0.45) * w
                    daily_lines = (rng.random(n_days) < p).astype(int) * rng.integers(1, 3, n_days)
                    bounce_p = 0.05
                elif arch == "slow":
                    p = rng.uniform(0.02, 0.08) * w
                    daily_lines = (rng.random(n_days) < p).astype(int)
                    bounce_p = 0.04
                elif arch == "sporadic":
                    daily_lines = np.zeros(n_days, dtype=int)
                    k = int(rng.integers(1, 3))
                    daily_lines[rng.choice(n_days, size=k, replace=False)] = 1
                    bounce_p = 0.0
                elif arch == "hard":
                    p = rng.uniform(0.10, 0.25) * w
                    daily_lines = (rng.random(n_days) < p).astype(int)
                    bounce_p = 0.7
                elif arch == "nonmoving":
                    daily_lines = np.zeros(n_days, dtype=int)
                    bounce_p = 0.0
                elif arch == "bounce_only_regular":
                    p = rng.uniform(0.06, 0.15) * w
                    daily_lines = (rng.random(n_days) < p).astype(int)
                    bounce_p = 1.0
                else:  # bounce_only_rare
                    daily_lines = np.zeros(n_days, dtype=int)
                    if rng.random() < 0.6 * w:
                        k = int(rng.integers(1, 3))
                        daily_lines[rng.choice(np.arange(n_days - 90, n_days), size=k, replace=False)] = 1
                    bounce_p = 1.0

                # weekly seasonality on fast movers: Sunday low, Monday high
                for d in np.nonzero(daily_lines)[0]:
                    day = start + timedelta(days=int(d))
                    lines = int(daily_lines[d])
                    if arch == "fast" and day.weekday() == 6:
                        lines = max(0, lines // 3)
                    for _ in range(lines):
                        rid = str(rng.choice(retailer_ids, p=r_weight))
                        if arch in ("hard", "bounce_only_regular"):
                            rid = str(rng.choice(retailer_ids[: max(10, self.n_retailers // 3)]))
                        qty = float(max(1, int(rng.poisson(base_qty))))
                        ts = datetime.combine(day, time(hour=int(rng.integers(8, 21)), minute=int(rng.integers(0, 60))))
                        order_counter += 1
                        oid = f"INV{order_counter:08d}"
                        if rng.random() < bounce_p:
                            recovered = arch in ("fast", "medium", "slow") and rng.random() < 0.85
                            recovered = recovered or (arch == "bounce_only_rare" and rng.random() < 0.5)
                            if arch == "hard":
                                recovered = rng.random() < 0.08
                            reason = "NOT_IN_STOCK" if recovered else (
                                "SUPPLY_SHORTAGE" if arch == "hard" and rng.random() < 0.5 else "EXTERNAL_SOURCING_FAILED"
                            )
                            bounces.append({
                                "bounce_id": f"BNC{order_counter:08d}",
                                "order_line_id": f"{oid}-1",
                                "sku_id": sku["sku_id"],
                                "retailer_id": rid,
                                "warehouse_id": wh["warehouse_id"],
                                "qty": qty,
                                "reason_code": reason,
                                "sourcing_attempted": True,
                                "outcome": "recovered" if recovered else "final",
                                "ts": ts,
                            })
                            if not recovered:
                                continue
                        disc = sku["normal_discount_pct"]
                        if arch == "fast" and rng.random() < 0.35:
                            disc += float(rng.choice([1.0, 2.0, 3.0]))  # leakage to detect
                        order_lines.append({
                            "order_id": oid,
                            "line_id": "1",
                            "retailer_id": rid,
                            "sku_id": sku["sku_id"],
                            "warehouse_id": wh["warehouse_id"],
                            "qty": qty,
                            "price": sku["ptr"],
                            "discount_pct": disc,
                            "channel": str(rng.choice(["portal", "app", "whatsapp", "salesman"])),
                            "ts": ts,
                            "term_flag": "NORMAL",
                        })

        # ---- inventory snapshot (batch level)
        inventory: list[dict[str, Any]] = []
        demand_index: dict[tuple[str, str], float] = {}
        cutoff = datetime.combine(today - timedelta(days=30), time())
        for ol in order_lines:
            if ol["ts"] >= cutoff:
                key = (ol["sku_id"], ol["warehouse_id"])
                demand_index[key] = demand_index.get(key, 0.0) + ol["qty"] / 30.0
        batch_no = 0
        for sku in skus:
            arch = sku["_archetype"]
            for wh in warehouses:
                rate = demand_index.get((sku["sku_id"], wh["warehouse_id"]), 0.0)
                batches: list[tuple[float, int, int]] = []  # qty, age_days, days_to_expiry
                if arch == "fast":
                    days_cover = float(rng.choice([0, 0.5, 1, 1.5, 2, 3, 4]))
                    if days_cover:
                        batches.append((round(rate * days_cover) + 1, int(rng.integers(3, 25)), int(rng.integers(300, 700))))
                elif arch == "medium":
                    if rng.random() < 0.75:
                        batches.append((float(rng.integers(3, 25)) * sku["pack_size"], int(rng.integers(10, 80)), int(rng.integers(200, 600))))
                elif arch == "slow":
                    if rng.random() < 0.8:
                        dte = int(rng.choice([60, 85, 120, 140, 240, 400, 500]))
                        batches.append((float(rng.integers(5, 40)) * sku["pack_size"], int(rng.integers(40, 260)), dte))
                elif arch == "nonmoving":
                    batches.append((float(rng.integers(5, 30)), int(rng.integers(150, 400)), int(rng.choice([45, 100, 200, 350]))))
                elif arch == "sporadic" and rng.random() < 0.3:
                    batches.append((float(rng.integers(1, 6)), int(rng.integers(30, 200)), int(rng.integers(150, 500))))
                for qty, age, dte in batches:
                    if qty <= 0:
                        continue
                    batch_no += 1
                    inventory.append({
                        "warehouse_id": wh["warehouse_id"],
                        "sku_id": sku["sku_id"],
                        "batch": f"B{batch_no:06d}",
                        "expiry": (today + timedelta(days=dte)).isoformat(),
                        "qty": float(qty),
                        "cost": sku["cost"],
                        "inward_date": (today - timedelta(days=age)).isoformat(),
                        "supplier_id": str(rng.choice(good_suppliers)),
                        "on_hold": False,
                    })
        # one quarantined batch to exercise P0
        if inventory:
            inventory[min(5, len(inventory) - 1)]["on_hold"] = True

        # ---- open POs
        open_pos = []
        fast_skus = [s for s in skus if s["_archetype"] == "fast"]
        for j, sku in enumerate(fast_skus[: max(1, len(fast_skus) // 4)]):
            open_pos.append({
                "po_id": f"PO{j + 1:06d}",
                "warehouse_id": "HYD01",
                "sku_id": sku["sku_id"],
                "supplier_id": good_suppliers[j % len(good_suppliers)],
                "qty": float(rng.integers(5, 30)),
                "expected_date": (today + timedelta(days=1)).isoformat(),
            })

        # ---- supplier price lists
        supplier_prices = []
        all_sup = [s["supplier_id"] for s in suppliers]
        for sku in skus:
            arch = sku["_archetype"]
            n = int(rng.integers(2, 5))
            for sid in rng.choice(all_sup, size=n, replace=False):
                avail = float(rng.integers(0, 200))
                if arch == "hard":
                    avail = float(rng.choice([0, 0, 0, 5]))
                if arch == "bounce_only_regular":
                    avail = float(rng.choice([0, 20, 50]))
                scheme = str(rng.choice(["", "", "", "10+1", "5%", "20+3"]))
                dte = int(rng.choice([45, 300, 400, 500, 600, 700, 700, 540, 365, 420]))  # ~10% short-dated lots
                supplier_prices.append({
                    "supplier_id": str(sid),
                    "sku_id": sku["sku_id"],
                    "price": round(sku["cost"] * float(rng.uniform(0.96, 1.10)), 2),
                    "scheme": scheme or None,
                    "available_qty": avail,
                    "batch": f"S{rng.integers(100000, 999999)}",
                    "expiry": (today + timedelta(days=dte)).isoformat(),
                })

        for s in skus:
            s.pop("_archetype", None)
        self._archetypes = {s["sku_id"]: str(a) for s, a in zip(skus, archetype)}
        self._data = {
            "warehouses": warehouses,
            "skus": skus,
            "retailers": retailers,
            "suppliers": suppliers,
            "order_lines": order_lines,
            "bounces": bounces,
            "inventory": inventory,
            "open_pos": open_pos,
            "supplier_prices": supplier_prices,
        }
        return self._data

    def archetype_of(self, sku_id: str) -> str | None:
        self._generate()
        return self._archetypes.get(sku_id)
