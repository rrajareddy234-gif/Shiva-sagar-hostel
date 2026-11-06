"""Pricing engine. Every amount is computed here, never by an AI model.

Rates live in the `rate_plans` table and are editable by the owner. Nothing in
this module invents a price: if no active rate plan matches the request, it
refuses to quote and asks the owner to set a rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from core.db import Database
from core.errors import NotFound, ValidationError

TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Promotion:
    promotion_id: int
    name: str
    percent: Decimal
    starts_on: date
    ends_on: date
    scope: str


def active_promotion(db: Database, *, on: date, scope: str = "DAILY") -> Promotion | None:
    """The promotion running on this date, for this kind of product, or None.

    The date is the day the booking is made, not the day of the stay: the offer
    is "book in the next three days", so the booking date decides. A promotion
    whose scope is ALL applies to everything; DAILY covers nightly and daily
    stays only, which is what leaves monthly stays and the walk-in services out.
    """
    row = db.query_one(
        """
        SELECT promotion_id, name, percent, starts_on, ends_on, scope
        FROM promotions
        WHERE active
          AND %(on)s BETWEEN starts_on AND ends_on
          AND scope IN (%(scope)s, 'ALL')
        ORDER BY percent DESC, promotion_id DESC
        LIMIT 1
        """,
        {"on": on, "scope": scope},
    )
    return Promotion(**row) if row else None


def promotion_discount(base, promotion: Promotion | None) -> Decimal:
    """The money off a base amount, rounded to the paise. Zero when there is none."""
    if promotion is None:
        return money(0)
    amount = money(base)
    if amount <= 0:
        return money(0)
    return money(amount * money(promotion.percent) / Decimal(100))


def set_promotion(db: Database, *, name: str, percent, starts_on: date, ends_on: date,
                  scope: str = "DAILY", active: bool = True) -> int:
    """Add or replace the promotion with this name."""
    if scope not in ("DAILY", "MONTHLY", "ALL"):
        raise ValidationError("scope must be DAILY, MONTHLY or ALL")
    pct = money(percent)
    if pct <= 0 or pct > 100:
        raise ValidationError("a promotion percent must be above 0 and at most 100")
    if ends_on < starts_on:
        raise ValidationError("the promotion must end on or after it starts")
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO promotions (name, percent, starts_on, ends_on, scope, active)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING promotion_id
            """,
            (name, pct, starts_on, ends_on, scope, active),
        )
        row = cur.fetchone()
        if row:
            return int(row["promotion_id"])
        cur.execute(
            "UPDATE promotions SET percent = %s, starts_on = %s, ends_on = %s, "
            "scope = %s, active = %s WHERE name = %s RETURNING promotion_id",
            (pct, starts_on, ends_on, scope, active, name),
        )
        return int(cur.fetchone()["promotion_id"])


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
    units: int                # days or hours, per guest
    unit_rate: Decimal        # per bed, per unit
    base_amount: Decimal      # unit_rate * units * guests
    extras: Decimal
    discount: Decimal
    charges_total: Decimal
    rate_plan_id: str
    guests: int = 1           # beds charged for; 1 unless a party is booked
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
                   guests: int = 1,
                   extras: Decimal | float | int = 0,
                   discount: Decimal | float | int = 0,
                   promotion: Promotion | None = None) -> Quote:
    """Pure calculation. No database, no AI, no side effects.

    A bed is charged per night, and a party is charged per bed: the nightly rate
    is multiplied by the nights and then by the number of guests. So two guests
    for three nights is `rate * 3 * 2`. `guests` defaults to 1, which is the
    single-guest case every existing caller gets.
    """
    guests = int(guests or 1)
    if guests < 1:
        raise ValidationError("there must be at least one guest")

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

    base = money(unit_rate * units * guests)
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
        guests=guests,
        breakdown={
            "unit_rate": str(unit_rate),
            "units": units,
            "guests": guests,
            "base": str(base),
            "extras": str(extras_amt),
            "discount": str(discount_amt),
            "stay_mode": plan.stay_mode,
            "promotion": (promotion.name if promotion else None),
            "promotion_percent": (str(promotion.percent) if promotion else None),
        },
    )


def quote(db: Database, *, room_type: str, food_included: bool | None,
          start: datetime, end: datetime, mode: str = "daily",
          stay_mode: str = "NIGHT",
          guests: int = 1,
          extras: Decimal | float | int = 0,
          discount: Decimal | float | int = 0,
          booked_on: date | None = None,
          apply_promotion: bool = True) -> Quote:
    """Look up the owner's rate plan and compute a quote.

    `stay_mode` picks the night rate or the day rate. It defaults to NIGHT,
    which is what a guest asking for "a room" almost always means, but the
    caller can be explicit when the customer said "day".

    `food_included` may be None, in which case the room type's only food option
    is used. If the room type offers both, the caller must say which.

    `guests` is the size of the party: the nightly rate is charged once per
    guest. It defaults to 1.

    `booked_on` is the day the booking is made, and it is what a dated promotion
    is judged against -- the offer is "book in the next three days". It defaults
    to today. `apply_promotion` is False for a product a promotion does not
    cover, such as a monthly stay.
    """
    food = resolve_food_option(db, room_type, food_included)
    plan = _load_plan(db, room_type, food, stay_mode)
    promotion = None
    if apply_promotion:
        promotion = active_promotion(db, on=booked_on or date.today(), scope="DAILY")
    promo_discount = money(0)
    if promotion is not None:
        base = money(money(plan.daily_rate) * billable_units(start, end, mode) * int(guests or 1))
        promo_discount = promotion_discount(base, promotion)
    return compute_charge(plan, start, end, mode=mode, guests=guests,
                          extras=extras,
                          discount=money(discount) + promo_discount,
                          promotion=promotion)


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


@dataclass(frozen=True)
class MonthlyPlan:
    monthly_plan_id: str
    name: str
    room_type: str
    food_included: bool
    plan_kind: str            # "STANDARD" or "CONCESSION"
    stay_mode: str            # "NIGHT" or "DAY"
    monthly_rate: Decimal


@dataclass(frozen=True)
class MonthlyQuote:
    room_type: str
    food_included: bool
    plan_kind: str
    stay_mode: str
    months: int
    monthly_rate: Decimal
    charges_total: Decimal
    rate_plan_id: str
    breakdown: dict = field(default_factory=dict)


MONTHLY_KINDS = ("STANDARD", "CONCESSION")


def _load_monthly_plan(db: Database, room_type: str, food_included: bool,
                       plan_kind: str, stay_mode: str = "NIGHT") -> MonthlyPlan:
    """The active monthly plan for this room type, food option, kind and mode.

    A plan for the exact room type wins over an 'ANY' plan.
    """
    row = db.query_one(
        """
        SELECT monthly_plan_id, name, room_type, food_included, plan_kind,
               stay_mode, monthly_rate
        FROM monthly_plans
        WHERE food_included = %(food)s AND plan_kind = %(kind)s
          AND stay_mode = %(stay_mode)s AND active
          AND room_type IN (%(room_type)s, 'ANY')
        ORDER BY (room_type = %(room_type)s) DESC, effective_from DESC
        LIMIT 1
        """,
        {"room_type": room_type, "food": food_included, "kind": plan_kind,
         "stay_mode": stay_mode},
    )
    if not row:
        raise NotFound(
            f"no active monthly {plan_kind.lower()} plan for {room_type} "
            f"{'with' if food_included else 'without'} food, {stay_mode.lower()}; "
            "ask the owner to set one"
        )
    return MonthlyPlan(**row)


def monthly_plan_kinds(db: Database, room_type: str, food_included: bool,
                       stay_mode: str = "NIGHT") -> list[str]:
    """Which monthly plan kinds exist for this room type and stay mode."""
    rows = db.query_all(
        "SELECT DISTINCT plan_kind FROM monthly_plans "
        "WHERE food_included = %s AND stay_mode = %s AND active "
        "AND room_type IN (%s, 'ANY') ORDER BY plan_kind",
        (food_included, stay_mode, room_type),
    )
    return [r["plan_kind"] for r in rows]


def resolve_monthly_plan_kind(db: Database, room_type: str, food_included: bool,
                              requested: str | None,
                              stay_mode: str = "NIGHT") -> str:
    """Settle which monthly plan applies.

    Most room types have a single monthly plan, so an unspecified request
    resolves to it. Where both a standard and a concession plan exist, the
    caller must say which: the concession is a human decision (a valid student
    certificate, or a negotiated rate), and the system must not grant it
    silently. Defaulting to the concession would undercharge; defaulting to the
    standard would overcharge a student who qualifies.
    """
    offered = monthly_plan_kinds(db, room_type, food_included, stay_mode)
    if not offered:
        raise NotFound(f"no active monthly plan for {room_type}; ask the owner to set one")
    if requested is not None:
        requested = requested.upper()
        if requested not in offered:
            raise NotFound(f"{room_type} has no monthly {requested.lower()} plan")
        return requested
    if len(offered) == 1:
        return offered[0]
    raise ValidationError(
        f"{room_type} offers both a standard and a concession monthly rate; "
        "say which applies"
    )


def monthly_quote(db: Database, *, room_type: str, food_included: bool,
                  months: int = 1, plan_kind: str | None = None,
                  stay_mode: str = "NIGHT",
                  extras: Decimal | float | int = 0,
                  discount: Decimal | float | int = 0) -> MonthlyQuote:
    """Price a monthly (PG) stay. Whole months only; the rate is not pro-rated.

    `stay_mode` picks a night-only arrangement or a full-day one. Most rooms
    charge the same either way; AC charges more for a full day.
    """
    if months < 1:
        raise ValidationError("a monthly stay must be at least one month")
    if stay_mode not in ("NIGHT", "DAY"):
        raise ValidationError("stay_mode must be NIGHT or DAY")
    kind = resolve_monthly_plan_kind(db, room_type, food_included, plan_kind, stay_mode)
    plan = _load_monthly_plan(db, room_type, food_included, kind, stay_mode)
    monthly_rate = money(plan.monthly_rate)
    base = money(monthly_rate * months)
    extras_amt = money(extras)
    discount_amt = money(discount)
    if discount_amt < 0:
        raise ValidationError("a discount cannot be negative")
    total = money(base + extras_amt - discount_amt)
    if total < 0:
        raise ValidationError("the discount cannot exceed the total charge")
    return MonthlyQuote(
        room_type=room_type,
        food_included=food_included,
        plan_kind=kind,
        stay_mode=stay_mode,
        months=months,
        monthly_rate=monthly_rate,
        charges_total=total,
        rate_plan_id=plan.monthly_plan_id,
        breakdown={
            "monthly_rate": str(monthly_rate),
            "months": months,
            "base": str(base),
            "extras": str(extras_amt),
            "discount": str(discount_amt),
            "plan_kind": kind,
            "stay_mode": stay_mode,
        },
    )


def set_monthly_rate(db: Database, *, name: str, room_type: str, plan_kind: str,
                     monthly_rate: Decimal | float | int,
                     food_included: bool = True,
                     stay_mode: str = "NIGHT") -> str:
    """Add a new monthly plan. Older plans for the same slot are deactivated."""
    if plan_kind not in MONTHLY_KINDS:
        raise ValidationError("plan_kind must be STANDARD or CONCESSION")
    if room_type not in ("AC", "NON_AC", "DORM", "ANY"):
        raise ValidationError("room_type must be AC, NON_AC, DORM or ANY")
    if stay_mode not in ("NIGHT", "DAY"):
        raise ValidationError("stay_mode must be NIGHT or DAY")
    if money(monthly_rate) < 0:
        raise ValidationError("a monthly rate cannot be negative")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE monthly_plans SET active = false "
            "WHERE plan_kind = %s AND room_type = %s AND food_included = %s "
            "AND stay_mode = %s AND active",
            (plan_kind, room_type, food_included, stay_mode),
        )
        cur.execute(
            """
            INSERT INTO monthly_plans (name, room_type, food_included, plan_kind,
                                       stay_mode, monthly_rate)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING monthly_plan_id
            """,
            (name, room_type, food_included, plan_kind, stay_mode, monthly_rate),
        )
        return str(cur.fetchone()["monthly_plan_id"])


def list_monthly_plans(db: Database) -> list[dict]:
    """Every active monthly plan, dearest first."""
    return db.query_all(
        "SELECT name, room_type, food_included, plan_kind, stay_mode, monthly_rate "
        "FROM monthly_plans WHERE active ORDER BY room_type, plan_kind, stay_mode"
    )


@dataclass(frozen=True)
class Service:
    service_code: str
    name: str
    price: Decimal
    duration_minutes: int
    needs_bed: bool


def list_services(db: Database) -> list[dict]:
    """Every active walk-in service, with its price. Prices come from here only."""
    return db.query_all(
        "SELECT service_code, name, price, duration_minutes, needs_bed "
        "FROM services WHERE active ORDER BY price"
    )


def get_service(db: Database, service_code: str) -> Service:
    """One service by code. Refuses rather than inventing a price for an unknown one."""
    row = db.query_one(
        "SELECT service_code, name, price, duration_minutes, needs_bed "
        "FROM services WHERE service_code = %s AND active",
        (service_code,),
    )
    if not row:
        raise NotFound(f"no active service {service_code!r}; ask the owner to set one")
    return Service(**row)


def service_quote(db: Database, *, service_code: str) -> Decimal:
    """The price of one service. A fresh-up and a rest are never discounted."""
    return money(get_service(db, service_code).price)


def set_service(db: Database, *, service_code: str, name: str, price,
                duration_minutes: int = 0, needs_bed: bool = False,
                active: bool = True) -> str:
    """Add a service, or change its price. The owner's price, stored not guessed."""
    if money(price) < 0:
        raise ValidationError("a service price cannot be negative")
    if int(duration_minutes) < 0:
        raise ValidationError("a service duration cannot be negative")
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO services (service_code, name, price, duration_minutes,
                                  needs_bed, active)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (service_code) DO UPDATE
                SET name = EXCLUDED.name, price = EXCLUDED.price,
                    duration_minutes = EXCLUDED.duration_minutes,
                    needs_bed = EXCLUDED.needs_bed, active = EXCLUDED.active
            RETURNING service_code
            """,
            (service_code, name, money(price), int(duration_minutes), needs_bed, active),
        )
        return str(cur.fetchone()["service_code"])


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
    # This is a write, so it must run in a transaction. Reading it back with
    # query_one would open a connection that is closed without committing, and
    # the new price would silently never reach the database.
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO extras (name, price, unit, active) VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE
                SET price = EXCLUDED.price, unit = EXCLUDED.unit, active = EXCLUDED.active
            RETURNING extra_id
            """,
            (name, money(price), unit, active),
        )
        return str(cur.fetchone()["extra_id"])


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
