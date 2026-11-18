"""Cost tracking: every API call or render records its units; USD is computed from a configurable price table.

Raw units (tokens, characters, minutes, images, seconds) are always recorded. USD is only filled in when a
price for that model/operation is configured (backend/data/pricing.json overrides the defaults below), so
the dashboard never shows made-up numbers. Fill in prices from https://openai.com/api/pricing.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from .models import CostEvent

log = logging.getLogger("ytshorts.studio.costs")

# Prices that are well established. Everything else: add to data/pricing.json, e.g.
# {"gpt-5.5": {"input_per_million": 1.25, "output_per_million": 10.0}, "web_search": {"per_call": 0.01}}
DEFAULT_PRICES: dict = {
    "whisper-1": {"per_minute": 0.006},
}


def prices() -> dict:
    p = dict(DEFAULT_PRICES)
    f = settings.data_dir / "pricing.json"
    if f.exists():
        try:
            p.update(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:
            log.warning("data/pricing.json is not valid JSON; ignoring it")
    return p


def estimate_usd(model: str | None, operation: str, units: dict) -> float | None:
    table = prices()
    price = table.get(model or "") or {}
    total, known = 0.0, False
    if "input_tokens" in units and price.get("input_per_million") is not None:
        total += units["input_tokens"] / 1e6 * price["input_per_million"]
        known = True
    if "output_tokens" in units and price.get("output_per_million") is not None:
        total += units["output_tokens"] / 1e6 * price["output_per_million"]
        known = True
    if "minutes" in units and price.get("per_minute") is not None:
        total += units["minutes"] * price["per_minute"]
        known = True
    if "images" in units and price.get("per_image") is not None:
        total += units["images"] * price["per_image"]
        known = True
    if "characters" in units and price.get("per_million_characters") is not None:
        total += units["characters"] / 1e6 * price["per_million_characters"]
        known = True
    if operation == "web_search" and (table.get("web_search") or {}).get("per_call") is not None:
        total += units.get("calls", 1) * table["web_search"]["per_call"]
        known = True
    return round(total, 6) if known else None


def record(project_id: int | None, stage: str | None, provider: str, operation: str, units: dict,
           model: str | None = None, feature: str = "studio", note: str | None = None) -> None:
    usd = estimate_usd(model, operation, units)
    try:
        with session_scope() as s:
            s.add(CostEvent(feature=feature, project_id=project_id, stage=stage, provider=provider, model=model,
                            operation=operation, units_json=json.dumps(units), usd=usd, note=note))
    except Exception:  # cost logging must never break the work itself
        log.exception("failed to record cost event")


def summary(project_id: int, feature: str = "studio") -> dict:
    with session_scope() as s:
        rows = list(s.scalars(select(CostEvent).where(CostEvent.project_id == project_id, CostEvent.feature == feature)
                              .order_by(CostEvent.id)))
        by_stage: dict = {}
        total, unpriced = 0.0, 0
        for r in rows:
            u = json.loads(r.units_json or "{}")
            st = by_stage.setdefault(r.stage or "other", {"usd": 0.0, "unpriced_calls": 0, "units": {}})
            for k, v in u.items():
                if isinstance(v, (int, float)):
                    st["units"][k] = round(st["units"].get(k, 0) + v, 3)
            if r.usd is None:
                st["unpriced_calls"] += 1
                unpriced += 1
            else:
                st["usd"] = round(st["usd"] + r.usd, 6)
                total += r.usd
        return {"total_usd": round(total, 4), "unpriced_calls": unpriced, "by_stage": by_stage, "events": len(rows),
                "note": None if not unpriced else
                "Some calls have no price configured (see backend/data/pricing.json); their units are still counted."}
