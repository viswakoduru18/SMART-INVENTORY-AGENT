"""Supplier Offer Parser agent.

Unstructured supplier WhatsApp / email / PDF-text price lists and scheme
circulars -> schema-validated offers -> SKU matching -> compliance gates ->
stored offers -> open sourcing requests for those SKUs are re-evaluated.
Claude extracts; it never decides whether an offer is acceptable (the hard
gates do).
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Policy
from ..engines import sourcing
from ..engines.compliance import effective_price, gate_supplier_offer
from ..models import Sku, Supplier, SupplierOffer
from . import llm

SYSTEM = """You extract structured purchase offers from messages sent to a pharmaceutical distributor in India by its suppliers \
(WhatsApp texts, emails, OCR'd price lists, scheme circulars). Messages mix English, Telugu, Hindi and trade shorthand.

Extract one offer per product line. Rules:
- product_name: the product as written (brand + strength + pack if present).
- composition: the molecule(s) and strength if stated or unambiguous from a well-known brand; otherwise null.
- manufacturer: the pharma company if stated (e.g. Cipla, Sun Pharma), else null.
- price: the per-unit purchase rate offered to the distributor (PTS / rate / net rate), as a number in rupees. Not MRP. Null if absent.
- mrp: MRP per unit if stated, else null.
- scheme: trade scheme exactly in the forms "10+1" (free goods) or "5%" (extra discount); null if none.
- available_qty: units available, null if not stated. "plenty"/"available" -> null.
- batch: batch number as written, else null.
- expiry: ISO date YYYY-MM-DD. For month/year expiries like "06/27" or "Jun-2027" use the last day of that month. Null if absent.
- Never invent values. Missing information stays null; the compliance gates will reject incomplete offers, which is correct."""

SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": ["string", "null"]},
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "composition": {"type": ["string", "null"]},
                    "manufacturer": {"type": ["string", "null"]},
                    "price": {"type": ["number", "null"]},
                    "mrp": {"type": ["number", "null"]},
                    "scheme": {"type": ["string", "null"]},
                    "available_qty": {"type": ["number", "null"]},
                    "batch": {"type": ["string", "null"]},
                    "expiry": {"type": ["string", "null"]},
                },
                "required": ["product_name", "composition", "manufacturer", "price", "mrp", "scheme", "available_qty", "batch", "expiry"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["supplier_name", "offers"],
    "additionalProperties": False,
}


_SPLIT = re.compile(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)")
# Manufacturer tokens recognised even when the SKU master has none of their products yet.
KNOWN_MANUFACTURERS = {
    "sun", "cipla", "reddy", "reddys", "lupin", "zydus", "cadila", "mankind", "alkem", "torrent", "glenmark", "intas", "abbott",
    "micro", "macleods", "ajanta", "eris", "aristo", "alembic", "ipca", "emcure", "fdc", "wockhardt", "biocon", "usv", "franco",
    "pfizer", "gsk", "glaxo", "sanofi", "novartis", "msd", "astrazeneca", "boehringer", "lilly", "bayer", "jb", "unichem",
    "hetero", "aurobindo", "natco", "indoco", "medley", "corona", "leeford", "mylan",
}
_STOP = {"tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules", "mg", "ml", "mcg", "iu", "inj", "the", "of", "x"}


def _tokens(s: str | None) -> list[str]:
    s = _SPLIT.sub(" ", re.sub(r"[^a-z0-9. ]", " ", (s or "").lower()))
    return [t for t in s.split() if t]


def _numbers(tokens: list[str]) -> set[str]:
    return {t.rstrip(".") for t in tokens if re.fullmatch(r"\d+(\.\d+)?", t.rstrip("."))}


def match_sku(skus: list[Sku], product_name: str, composition: str | None = None, manufacturer: str | None = None,
              cutoff: float = 0.5, margin: float = 0.08) -> tuple[Sku | None, float, str]:
    """Conservative product matcher. A wrong match attaches a supplier offer to the wrong drug, so:
    - the leading product word (brand/molecule) or the composition molecule must agree;
    - every number in the offer (strength, pack count) must appear on the candidate;
    - a manufacturer named in the offer must not contradict the candidate's manufacturer;
    - near-ties between different SKUs are rejected as AMBIGUOUS for manual mapping.
    Returns (sku, score, reason)."""
    q = _tokens(product_name)
    if not q:
        return None, 0.0, "EMPTY"
    q_nums = _numbers(q)
    q_words = {t for t in q if t not in _STOP and t not in q_nums}
    comp_words = {t for t in _tokens(composition) if t not in _STOP and not _numbers([t])}
    mfr_tokens = {m for s in skus for m in _tokens(s.manufacturer)} | KNOWN_MANUFACTURERS
    q_mfr = (set(_tokens(manufacturer)) | (q_words & mfr_tokens)) - comp_words
    scored: list[tuple[float, Sku]] = []
    for sku in skus:
        name_t = _tokens(sku.name)
        cand = name_t + _tokens(sku.composition) + _tokens(sku.pack)
        c_words = {t for t in cand if t not in _STOP and not _numbers([t])}
        lead_ok = name_t and name_t[0] == q[0]
        mol_ok = bool(comp_words) and comp_words <= {t for t in _tokens(sku.composition) if t not in _STOP}
        if not (lead_ok or mol_ok):
            continue
        if q_nums and not q_nums <= _numbers(cand):
            continue
        sku_mfr = set(_tokens(sku.manufacturer))
        if q_mfr and sku_mfr and not (q_mfr & sku_mfr):
            continue
        overlap = len(q_words & c_words) / max(len(q_words | (c_words & (q_words | comp_words))), 1)
        score = 0.5 * overlap + 0.3 * (1.0 if q_nums else 0.5) + 0.2 * (1.0 if q_mfr else 0.5)
        scored.append((round(score, 3), sku))
    if not scored:
        return None, 0.0, "SKU_NOT_MATCHED"
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    if best_score < cutoff:
        return None, best_score, "SKU_NOT_MATCHED"
    if len(scored) > 1 and best_score - scored[1][0] < margin and scored[1][1].sku_id != best.sku_id:
        return None, best_score, "AMBIGUOUS"
    return best, best_score, "MATCHED"


def extract(text: str) -> dict[str, Any]:
    resp = llm.create(
        effort="low", max_tokens=8000, system=SYSTEM,
        messages=[{"role": "user", "content": f"<supplier_message>\n{text}\n</supplier_message>"}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise llm.LLMUnavailable("model declined to parse this message")
    return json.loads(llm.text_of(resp))


def parse_and_store(db: Session, policy: Policy, text: str, supplier_id: str | None, today: date, channel: str = "whatsapp") -> dict[str, Any]:
    supplier = db.get(Supplier, supplier_id) if supplier_id else None
    try:
        data = extract(text)
    except llm.LLMUnavailable as exc:
        llm.record(db, "supplier_parser", text, None, status="UNAVAILABLE", response=str(exc))
        raise
    if supplier is None and data.get("supplier_name"):
        cands = list(db.scalars(select(Supplier)))
        names = {s.name: s for s in cands}
        m = difflib.get_close_matches(data["supplier_name"], list(names), n=1, cutoff=0.6)
        supplier = names[m[0]] if m else None
    skus = list(db.scalars(select(Sku).where(Sku.active.is_(True))))
    stored, unmatched, touched = [], [], set()
    for o in data.get("offers", []):
        sku, score, why = match_sku(skus, o["product_name"], o.get("composition"), o.get("manufacturer"))
        if sku is None or supplier is None:
            unmatched.append({**o, "match_score": score, "reason": why if sku is None else "SUPPLIER_UNKNOWN"})
            continue
        exp = None
        if o.get("expiry"):
            try:
                exp = datetime.fromisoformat(o["expiry"][:10]).date()
            except ValueError:
                exp = None
        price = float(o["price"]) if o.get("price") is not None else 0.0
        offer = SupplierOffer(
            supplier_id=supplier.supplier_id, sku_id=sku.sku_id, price=price, scheme=o.get("scheme"),
            effective_price=effective_price(price, o.get("scheme")), available_qty=o.get("available_qty"),
            batch=o.get("batch"), expiry=exp, source="llm_parsed", gate_status="PENDING", gate_failures=[], raw_text=text[:4000],
        )
        gate = gate_supplier_offer({"batch": offer.batch, "expiry": exp, "effective_price": offer.effective_price, "price": price},
                                   sourcing._supplier_dict(supplier), sourcing._sku_dict(sku), today, policy.section("sourcing"),
                                   held=sourcing.is_held(db, sku.sku_id, None))
        offer.gate_status, offer.gate_failures = gate.status, gate.failures
        db.add(offer)
        db.flush()
        touched.add(sku.sku_id)
        stored.append({"offer_id": offer.id, "sku_id": sku.sku_id, "sku_name": sku.name, "match_score": score, **o})
    resolved = []
    for sku_id in touched:
        resolved += sourcing.reevaluate_for_sku(db, policy, sku_id, today)
    # gate status for the response
    for item in stored:
        o = db.get(SupplierOffer, item["offer_id"])
        item["gate_status"], item["gate_failures"] = o.gate_status, o.gate_failures
    result = {"supplier_id": supplier.supplier_id if supplier else None, "channel": channel, "offers_stored": stored,
              "unmatched": unmatched, "sourcing_requests_resolved": resolved}
    llm.record(db, "supplier_parser", text, llm.AgentResult(text=json.dumps(result, default=str)[:20000]))
    return result
