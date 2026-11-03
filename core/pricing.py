"""Pricing engine. Every amount is computed here, never by an AI model.

Rates live in the `rate_plans` table and are editable by the owner. Nothing in
this module invents a price: if no active rate plan matches the request, it
refuses to quote and asks the owner to set a rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from core.db import Database
from core.errors import NotFound, ValidationError

TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RatePlan:
    rate_plan_id: str
    name: str
    room_type: str
    food_included: bool
    daily_rate: Decimal | None
    hourly_rate: Decimal | None
    min_hours: int


@dataclass(frozen=True)
class Quote:
    room_type: str
    food_included: bool
    mode: str                 # "daily" or "hourly"
    units: int                # days or hours
    unit_rate: Decimal
    base_amount: Decimal
    extras: Decimal
    discount: Decimal
    charges_total: Decimal
    rate_plan_id: str
    breakdown: dict = field(default_factory=dict)


def _load_plan(db: Database, room_type: str, food_included: bool) -> RatePlan:
    row = db.query_one(
        """
        SELECT rate_plan_id, name, room_type, food_included, daily_rate, hourly_rate, min_hours
        FROM rate_plans
        WHERE room_type = %(room_type)s AND food_included = %(food)s AND active
        ORDER BY effective_from DESC
        LIMIT 1
        """,
        {"room_type": room_type, "food": food_included},
    )
    if not row:
        raise NotFound(
            f"no active rate plan for {room_type} "
            f"{'with' if food_included else 'without'} food; ask the owner to set one"
        )
    return RatePlan(**row)


def billable_units(start: datetime, end: datetime, mode: str) -> int:
    """Days or hours to charge. Partial units round up; a stay is at least one unit."""
    if end <= start:
        raise ValidationError("the checkout time must be after the check-in time")
    seconds = (end - start).total_seconds()
    if mode == "hourly":
        return max(1, math.ceil(seconds / 3600))
    return max(1, math.ceil(seconds / 86400))


def compute_charge(plan: RatePlan, start: datetime, end: datetime, *,
                   mode: str = "daily",
                   extras: Decimal | float | int = 0,
                   discount: Decimal | float | int = 0) -> Quote:
    """Pure calculation. No database, no AI, no side effects."""
    if mode == "hourly":
        if plan.hourly_rate is None:
            raise ValidationError(f"rate plan {plan.name!r} has no hourly rate")
        unit_rate = money(plan.hourly_rate)
        units = billable_units(start, end, "hourly")
        if units < plan.min_hours:
            units = plan.min_hours
    else:
        if plan.daily_rate is None:
            raise ValidationError(f"rate plan {plan.name!r} has no daily rate")
        unit_rate = money(plan.daily_rate)
        units = billable_units(start, end, "daily")

    base = money(unit_rate * units)
    extras_amt = money(extras)
    discount_amt = money(discount)
    if discount_amt < 0:
        raise ValidationError("a discount cannot be negative")
    total = money(base + extras_amt - discount_amt)
    if total < 0:
        raise ValidationError("the discount cannot exceed the total charge")

    return Quote(
        room_type=plan.room_type,
        food_included=plan.food_included,
        mode=mode,
        units=units,
        unit_rate=unit_rate,
        base_amount=base,
        extras=extras_amt,
        discount=discount_amt,
        charges_total=total,
        rate_plan_id=plan.rate_plan_id,
        breakdown={
            "unit_rate": str(unit_rate),
            "units": units,
            "base": str(base),
            "extras": str(extras_amt),
            "discount": str(discount_amt),
        },
    )


def quote(db: Database, *, room_type: str, food_included: bool,
          start: datetime, end: datetime, mode: str = "daily",
          extras: Decimal | float | int = 0,
          discount: Decimal | float | int = 0) -> Quote:
    """Look up the owner's rate plan and compute a quote."""
    plan = _load_plan(db, room_type, food_included)
    return compute_charge(plan, start, end, mode=mode, extras=extras, discount=discount)


def set_rate(db: Database, *, name: str, room_type: str, food_included: bool,
             daily_rate: Decimal | float | int | None = None,
             hourly_rate: Decimal | float | int | None = None,
             min_hours: int = 1) -> str:
    """Add a new rate plan. Older plans are deactivated so only one is current."""
    if daily_rate is None and hourly_rate is None:
        raise ValidationError("a rate plan needs a daily rate, an hourly rate, or both")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE rate_plans SET active = false "
            "WHERE room_type = %s AND food_included = %s AND active",
            (room_type, food_included),
        )
        cur.execute(
            """
            INSERT INTO rate_plans (name, room_type, food_included, daily_rate, hourly_rate, min_hours)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING rate_plan_id
            """,
            (name, room_type, food_included, daily_rate, hourly_rate, min_hours),
        )
        return str(cur.fetchone()["rate_plan_id"])
