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
    stay_mode: str            # "NIGHT" or "DAY"
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


def _load_plan(db: Database, room_type: str, food_included: bool,
               stay_mode: str = "NIGHT") -> RatePlan:
    row = db.query_one(
        """
        SELECT rate_plan_id, name, room_type, food_included, stay_mode,
               daily_rate, hourly_rate, min_hours
        FROM rate_plans
        WHERE room_type = %(room_type)s AND food_included = %(food)s
          AND stay_mode = %(stay_mode)s AND active
        ORDER BY effective_from DESC
        LIMIT 1
        """,
        {"room_type": room_type, "food": food_included, "stay_mode": stay_mode},
    )
    if not row:
        raise NotFound(
            f"no active rate plan for {room_type} "
            f"{'with' if food_included else 'without'} food per "
            f"{stay_mode.lower()}; ask the owner to set one"
        )
    return RatePlan(**row)


def rates_for(db: Database, room_type: str, food_included: bool) -> dict[str, RatePlan]:
    """Every active plan for a room type, keyed by stay mode. May be empty."""
    rows = db.query_all(
        """
        SELECT rate_plan_id, name, room_type, food_included, stay_mode,
               daily_rate, hourly_rate, min_hours
        FROM rate_plans
        WHERE room_type = %s AND food_included = %s AND active
        ORDER BY effective_from DESC
        """,
        (room_type, food_included),
    )
    out: dict[str, RatePlan] = {}
    for row in rows:
        out.setdefault(row["stay_mode"], RatePlan(**row))
    return out


def food_options(db: Database, room_type: str) -> list[bool]:
    """Which food options this room type actually offers. Usually just one."""
    rows = db.query_all(
        "SELECT DISTINCT food_included FROM rate_plans "
        "WHERE room_type = %s AND active ORDER BY food_included",
        (room_type,),
    )
    return [r["food_included"] for r in rows]


def resolve_food_option(db: Database, room_type: str, requested: bool | None) -> bool:
    """Settle the food question for a room type.

    Most room types offer only one food option, so there is nothing to decide and
    an unspecified request resolves to it. The dormitory offers both, with and
    without food, so there the caller must say which. When the caller asked for a
    specific option that the room type does not offer, that is an error rather
    than something to silently change: quoting food a guest refused, or omitting
    food they expected, would both be wrong.
    """
    offered = food_options(db, room_type)
    if not offered:
        raise NotFound(f"no active rate plan for {room_type}; ask the owner to set one")
    if len(offered) == 1:
        only = offered[0]
        if requested is not None and requested != only:
            raise NotFound(
                f"{room_type} is only offered "
                f"{'with' if only else 'without'} food; ask the owner before changing that"
            )
        return only
    if requested is None:
        raise ValidationError(
            f"{room_type} offers both food options; say whether food is included"
        )
    if requested not in offered:
        raise NotFound(f"{room_type} does not offer that food option")
    return requested


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
            "stay_mode": plan.stay_mode,
        },
    )


def quote(db: Database, *, room_type: str, food_included: bool | None,
          start: datetime, end: datetime, mode: str = "daily",
          stay_mode: str = "NIGHT",
          extras: Decimal | float | int = 0,
          discount: Decimal | float | int = 0) -> Quote:
    """Look up the owner's rate plan and compute a quote.

    `stay_mode` picks the night rate or the day rate. It defaults to NIGHT,
    which is what a guest asking for "a room" almost always means, but the
    caller can be explicit when the customer said "day".

    `food_included` may be None, in which case the room type's only food option
    is used. If the room type offers both, the caller must say which.
    """
    food = resolve_food_option(db, room_type, food_included)
    plan = _load_plan(db, room_type, food, stay_mode)
    return compute_charge(plan, start, end, mode=mode, extras=extras, discount=discount)


def set_rate(db: Database, *, name: str, room_type: str, food_included: bool,
             daily_rate: Decimal | float | int | None = None,
             hourly_rate: Decimal | float | int | None = None,
             min_hours: int = 1, stay_mode: str = "NIGHT") -> str:
    """Add a new rate plan. Older plans for the same slot are deactivated."""
    if stay_mode not in ("NIGHT", "DAY"):
        raise ValidationError("stay_mode must be NIGHT or DAY")
    if daily_rate is None and hourly_rate is None:
        raise ValidationError("a rate plan needs a daily rate, an hourly rate, or both")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE rate_plans SET active = false "
            "WHERE room_type = %s AND food_included = %s AND stay_mode = %s AND active",
            (room_type, food_included, stay_mode),
        )
        cur.execute(
            """
            INSERT INTO rate_plans (name, room_type, food_included, stay_mode,
                                    daily_rate, hourly_rate, min_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING rate_plan_id
            """,
            (name, room_type, food_included, stay_mode, daily_rate, hourly_rate, min_hours),
        )
        return str(cur.fetchone()["rate_plan_id"])


def list_extras(db: Database) -> list[dict]:
    """Every active chargeable extra, with its price."""
    return db.query_all(
        "SELECT extra_id, name, price, unit FROM extras WHERE active ORDER BY name"
    )


def set_extra(db: Database, *, name: str, price: Decimal | float | int,
              unit: str = "PER_ITEM", active: bool = True) -> str:
    """Add an extra, or update the price of one that already exists."""
    if unit not in ("PER_ITEM", "PER_STAY", "PER_MONTH", "PER_NIGHT"):
        raise ValidationError(f"unknown extra unit {unit!r}")
    if money(price) < 0:
        raise ValidationError("an extra price cannot be negative")
    row = db.query_one(
        """
        INSERT INTO extras (name, price, unit, active) VALUES (%s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE
            SET price = EXCLUDED.price, unit = EXCLUDED.unit, active = EXCLUDED.active
        RETURNING extra_id
        """,
        (name, money(price), unit, active),
    )
    return str(row["extra_id"])


def extras_total(db: Database, items: dict[str, int]) -> tuple[Decimal, list[dict]]:
    """Price a basket of extras, e.g. {"New towel": 2}. Never guesses a price."""
    if not items:
        return money(0), []
    known = {r["name"].lower(): r for r in list_extras(db)}
    total = money(0)
    lines: list[dict] = []
    for name, qty in items.items():
        row = known.get(str(name).lower())
        if not row:
            raise NotFound(f"no such extra {name!r}; ask the owner to add it")
        if qty < 0:
            raise ValidationError("an extra quantity cannot be negative")
        amount = money(Decimal(str(row["price"])) * qty)
        total = money(total + amount)
        lines.append({"name": row["name"], "quantity": qty,
                      "unit_price": str(money(row["price"])),
                      "unit": row["unit"], "amount": str(amount)})
    return total, lines
