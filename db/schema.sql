-- Sagar PG AI Manager — PostgreSQL schema (Phase 3a, initial slice)
-- Deterministic core. Availability, booking and payment truth live here,
-- never in the AI layer.

CREATE EXTENSION IF NOT EXISTS btree_gist;   -- needed for the no-overlap constraint
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Configuration
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Physical inventory: rooms and individual beds
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rooms (
    room_id     text PRIMARY KEY,
    room_type   text NOT NULL CHECK (room_type IN ('AC', 'NON_AC', 'DORM')),
    total_beds  integer NOT NULL CHECK (total_beds >= 0),
    floor       text,
    active      boolean NOT NULL DEFAULT true,
    notes       text
);

CREATE TABLE IF NOT EXISTS beds (
    bed_id      text PRIMARY KEY,
    room_id     text NOT NULL REFERENCES rooms(room_id) ON DELETE RESTRICT,
    bed_type    text NOT NULL CHECK (bed_type IN ('AC', 'NON_AC', 'MAT')),
    status      text NOT NULL DEFAULT 'AVAILABLE'
                CHECK (status IN ('AVAILABLE','TEMPORARILY_HELD','BOOKING_REQUESTED',
                                  'BOOKED','OCCUPIED','CHECKOUT_PENDING','CLEANING',
                                  'MAINTENANCE','UNAVAILABLE')),
    active      boolean NOT NULL DEFAULT true,
    notes       text
);
CREATE INDEX IF NOT EXISTS idx_beds_room ON beds(room_id);

-- ---------------------------------------------------------------------------
-- Guests and enquiries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guests (
    guest_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name   text,
    phone       text UNIQUE,               -- E.164, e.g. +919876543210
    gender      text CHECK (gender IN ('MALE', 'FEMALE', 'OTHER', 'UNKNOWN')),
    is_owner    boolean NOT NULL DEFAULT false,
    is_staff    boolean NOT NULL DEFAULT false,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS enquiries (
    enquiry_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    guest_id        uuid REFERENCES guests(guest_id) ON DELETE SET NULL,
    phone           text NOT NULL,
    channel         text NOT NULL DEFAULT 'WHATSAPP',
    first_message_at timestamptz NOT NULL DEFAULT now(),
    last_message_at  timestamptz NOT NULL DEFAULT now(),
    responded        boolean NOT NULL DEFAULT false,
    responded_at     timestamptz,
    status           text NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN','RESPONDED','AWAITING_OWNER','CLOSED','ESCALATED')),
    summary          text
);
CREATE INDEX IF NOT EXISTS idx_enquiries_open ON enquiries(status, last_message_at);

-- ---------------------------------------------------------------------------
-- Pricing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_plans (
    rate_plan_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    room_type     text NOT NULL CHECK (room_type IN ('AC', 'NON_AC', 'DORM')),
    food_included boolean NOT NULL DEFAULT false,
    -- A stay can be priced per night or per day; the owner quotes both.
    stay_mode     text NOT NULL DEFAULT 'NIGHT' CHECK (stay_mode IN ('NIGHT', 'DAY')),
    daily_rate    numeric(10,2) CHECK (daily_rate >= 0),
    hourly_rate   numeric(10,2) CHECK (hourly_rate >= 0),
    min_hours     integer NOT NULL DEFAULT 1,
    effective_from timestamptz NOT NULL DEFAULT now(),
    active        boolean NOT NULL DEFAULT true,
    notes         text
);

-- Existing databases predate stay_mode; add it without disturbing the data.
ALTER TABLE rate_plans
    ADD COLUMN IF NOT EXISTS stay_mode text NOT NULL DEFAULT 'NIGHT';
ALTER TABLE rate_plans DROP CONSTRAINT IF EXISTS rate_plans_stay_mode_check;
ALTER TABLE rate_plans ADD CONSTRAINT rate_plans_stay_mode_check
    CHECK (stay_mode IN ('NIGHT', 'DAY'));

-- Only one plan may be current for a given room type, food option and stay mode.
CREATE UNIQUE INDEX IF NOT EXISTS rate_plans_one_active
    ON rate_plans (room_type, food_included, stay_mode)
    WHERE active;

-- ---------------------------------------------------------------------------
-- Monthly (PG) plans — a separate product from nightly and daily stays.
--
-- A monthly stay is agreed in whole months. The owner charges a standard
-- monthly rate, and a lower concession rate for students with a valid
-- institution certificate and for anyone who negotiates. Whether a guest
-- qualifies is a human decision, so the plan kind is chosen explicitly and is
-- never inferred by the system.
--
-- room_type 'ANY' means the plan applies to every room type; a row for a
-- specific room type takes precedence over it.
--
-- stay_mode mirrors rate_plans: NIGHT is a night-only arrangement, DAY is a
-- full-day one. Most rooms charge the same for both; AC charges more for a
-- full day.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monthly_plans (
    monthly_plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    room_type       text NOT NULL DEFAULT 'ANY'
                    CHECK (room_type IN ('AC', 'NON_AC', 'DORM', 'ANY')),
    food_included   boolean NOT NULL DEFAULT true,
    plan_kind       text NOT NULL DEFAULT 'STANDARD'
                    CHECK (plan_kind IN ('STANDARD', 'CONCESSION')),
    stay_mode       text NOT NULL DEFAULT 'NIGHT' CHECK (stay_mode IN ('NIGHT', 'DAY')),
    monthly_rate    numeric(10,2) NOT NULL CHECK (monthly_rate >= 0),
    effective_from  timestamptz NOT NULL DEFAULT now(),
    active          boolean NOT NULL DEFAULT true,
    notes           text
);
-- Existing databases predate stay_mode; add it without disturbing the data.
ALTER TABLE monthly_plans
    ADD COLUMN IF NOT EXISTS stay_mode text NOT NULL DEFAULT 'NIGHT';
ALTER TABLE monthly_plans DROP CONSTRAINT IF EXISTS monthly_plans_stay_mode_check;
ALTER TABLE monthly_plans ADD CONSTRAINT monthly_plans_stay_mode_check
    CHECK (stay_mode IN ('NIGHT', 'DAY'));
-- Only one plan may be current for a given kind, room type, food option and stay mode.
DROP INDEX IF EXISTS monthly_plans_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS monthly_plans_one_active
    ON monthly_plans (plan_kind, room_type, food_included, stay_mode)
    WHERE active;

-- ---------------------------------------------------------------------------
-- Chargeable extras: items the owner sells or rents alongside a bed.
-- Prices are the owner's; nothing here is invented by the system.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extras (
    extra_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL UNIQUE,
    price      numeric(10,2) NOT NULL CHECK (price >= 0),
    -- What one charge covers: a single item, the whole stay, or a month.
    unit       text NOT NULL DEFAULT 'PER_ITEM'
               CHECK (unit IN ('PER_ITEM', 'PER_STAY', 'PER_MONTH', 'PER_NIGHT')),
    active     boolean NOT NULL DEFAULT true,
    notes      text
);

-- ---------------------------------------------------------------------------
-- Bookings — the core table. Double-booking is prevented by a database
-- exclusion constraint, not by application logic.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bookings (
    booking_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference      text UNIQUE NOT NULL,       -- human-readable, e.g. SPG-2026-0001
    guest_id       uuid NOT NULL REFERENCES guests(guest_id) ON DELETE RESTRICT,
    bed_id         text NOT NULL REFERENCES beds(bed_id) ON DELETE RESTRICT,
    check_in_at    timestamptz NOT NULL,
    check_out_at   timestamptz NOT NULL,
    status         text NOT NULL DEFAULT 'REQUESTED'
                   CHECK (status IN ('REQUESTED','CONFIRMED','CHECKED_IN','CHECKED_OUT',
                                     'CANCELLED','REJECTED','NO_SHOW','EXPIRED')),
    food_included  boolean NOT NULL DEFAULT false,
    agreed_daily_rate numeric(10,2),
    source         text NOT NULL DEFAULT 'WHATSAPP',
    created_at     timestamptz NOT NULL DEFAULT now(),
    approved_by    text,
    approved_at    timestamptz,
    notes          text,
    CONSTRAINT bookings_dates_ordered CHECK (check_out_at > check_in_at)
);

-- The single most important guarantee in the system: no two confirmed or
-- in-house bookings may occupy the same bed over overlapping time ranges.
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_no_overlap;
ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap
    EXCLUDE USING gist (
        bed_id WITH =,
        tstzrange(check_in_at, check_out_at, '[)') WITH &&
    )
    WHERE (status IN ('CONFIRMED', 'CHECKED_IN'));

CREATE INDEX IF NOT EXISTS idx_bookings_bed_time ON bookings(bed_id, check_in_at, check_out_at);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);

-- Human-readable booking references, generated in the database so they are
-- unique under concurrency. Format: SPG-YYYY-000123
CREATE SEQUENCE IF NOT EXISTS booking_ref_seq;
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS charges_total numeric(12,2);
ALTER TABLE bookings
    ALTER COLUMN reference SET DEFAULT
    ('SPG-' || to_char(now(), 'YYYY') || '-' || lpad(nextval('booking_ref_seq')::text, 6, '0'));

-- The enquiry that led to this booking, so the chain from question to money is
-- traceable. Nullable: a booking may be taken at the desk with no enquiry first.
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS enquiry_id uuid REFERENCES enquiries(enquiry_id) ON DELETE SET NULL;

-- How the guest agreed to pay: the whole bill up front, or an advance to hold
-- the bed. The advance is a minimum per person and is never refunded.
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS payment_terms text NOT NULL DEFAULT 'ADVANCE';
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_payment_terms_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_payment_terms_check
    CHECK (payment_terms IN ('FULL', 'ADVANCE'));
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS guests_count integer NOT NULL DEFAULT 1;
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_guests_count_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_guests_count_check
    CHECK (guests_count > 0);
-- The advance owed to hold the bed: the per-person minimum times the party.
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS advance_due numeric(12,2) NOT NULL DEFAULT 0;
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_advance_due_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_advance_due_check
    CHECK (advance_due >= 0);


-- ---------------------------------------------------------------------------
-- Temporary bed holds (expire automatically so beds are never blocked forever)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bed_holds (
    hold_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bed_id      text NOT NULL REFERENCES beds(bed_id) ON DELETE CASCADE,
    guest_id    uuid REFERENCES guests(guest_id) ON DELETE SET NULL,
    enquiry_id  uuid REFERENCES enquiries(enquiry_id) ON DELETE SET NULL,
    start_at    timestamptz NOT NULL,
    end_at      timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','RELEASED','EXPIRED','CONVERTED')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT holds_dates_ordered CHECK (end_at > start_at),
    -- A hold for a future stay legitimately expires before that stay begins,
    -- so expiry is checked against creation time, not against the stay start.
    CONSTRAINT holds_expiry_after_creation CHECK (expires_at > created_at)
);
-- Two live holds may not overlap on the same bed.
ALTER TABLE bed_holds DROP CONSTRAINT IF EXISTS holds_no_overlap;
ALTER TABLE bed_holds ADD CONSTRAINT holds_no_overlap
    EXCLUDE USING gist (
        bed_id WITH =,
        tstzrange(start_at, end_at, '[)') WITH &&
    )
    WHERE (status = 'ACTIVE');
CREATE INDEX IF NOT EXISTS idx_holds_active ON bed_holds(status, expires_at);

-- ---------------------------------------------------------------------------
-- Payments — a screenshot is never proof. Only an authorised verifier or a
-- trusted gateway sets verified = true.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    payment_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id    uuid REFERENCES bookings(booking_id) ON DELETE RESTRICT,
    guest_id      uuid REFERENCES guests(guest_id) ON DELETE SET NULL,
    amount        numeric(10,2) NOT NULL CHECK (amount <> 0),
    kind          text NOT NULL DEFAULT 'PAYMENT' CHECK (kind IN ('PAYMENT','REFUND','ADJUSTMENT')),
    method        text CHECK (method IN ('UPI','CASH','BANK_TRANSFER','GATEWAY','OTHER')),
    txn_ref       text,
    paid_at       timestamptz NOT NULL DEFAULT now(),
    verified      boolean NOT NULL DEFAULT false,
    verified_by   text,
    verified_at   timestamptz,
    -- Money the owner has promised not to return, e.g. the booking advance.
    -- Kept on the payment itself so a refund run can never sweep it up.
    refundable    boolean NOT NULL DEFAULT true,
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
-- A real transaction reference may not be recorded twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_txn_ref
    ON payments(txn_ref) WHERE txn_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id);
CREATE INDEX IF NOT EXISTS idx_payments_unverified ON payments(verified) WHERE verified = false;

-- ---------------------------------------------------------------------------
-- Conversations and raw messages (dedupe on WhatsApp message id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone           text NOT NULL UNIQUE,
    state           jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_activity   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    wa_message_id   text UNIQUE,               -- inbound WhatsApp id; UNIQUE = idempotency
    conversation_id uuid REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    direction       text NOT NULL CHECK (direction IN ('IN','OUT')),
    body            text,
    payload         jsonb,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages(processed_at) WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Partner referrals (income tracked separately from hostel revenue)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS partners (
    partner_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    contact        text,
    location       text,
    accommodation_type text,
    rate_note      text,
    availability_confirmed_at timestamptz,
    commission_pct numeric(5,2) CHECK (commission_pct >= 0),
    active         boolean NOT NULL DEFAULT true,
    notes          text
);

CREATE TABLE IF NOT EXISTS referrals (
    referral_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id   uuid NOT NULL REFERENCES partners(partner_id) ON DELETE RESTRICT,
    guest_id     uuid REFERENCES guests(guest_id) ON DELETE SET NULL,
    status       text NOT NULL DEFAULT 'SUGGESTED'
                 CHECK (status IN ('SUGGESTED','CONFIRMED','DECLINED','COMPLETED','CANCELLED')),
    amount_earned numeric(10,2) NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Booking extras — items sold or rented alongside one guest's stay.
--
-- An extra belongs to a stay, not to a guest, so a towel bought on one visit
-- never follows the guest to the next. The price is copied from `extras` at the
-- moment it is added: a later change to the shop price must not rewrite an old
-- bill.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS booking_extras (
    booking_extra_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id  uuid NOT NULL REFERENCES bookings(booking_id) ON DELETE CASCADE,
    extra_id    uuid REFERENCES extras(extra_id) ON DELETE SET NULL,
    name        text NOT NULL,
    quantity    integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price  numeric(10,2) NOT NULL CHECK (unit_price >= 0),
    amount      numeric(10,2) NOT NULL CHECK (amount >= 0),
    added_by    text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_booking_extras_booking ON booking_extras(booking_id);

-- ---------------------------------------------------------------------------
-- Invoices — the bill for one booking, frozen when it is issued.
--
-- `charges_total` on a booking is a live number. An invoice is a document: its
-- lines are copied in and never recomputed, so a bill reprinted next year shows
-- exactly what the guest was given. Invoice numbers are generated by the
-- database so they stay unique under concurrency: INV-YYYY-000123.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS invoice_ref_seq;

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number       text UNIQUE NOT NULL DEFAULT
                 ('INV-' || to_char(now(), 'YYYY') || '-' ||
                  lpad(nextval('invoice_ref_seq')::text, 6, '0')),
    booking_id   uuid NOT NULL REFERENCES bookings(booking_id) ON DELETE RESTRICT,
    guest_id     uuid REFERENCES guests(guest_id) ON DELETE SET NULL,
    status       text NOT NULL DEFAULT 'ISSUED'
                 CHECK (status IN ('ISSUED', 'PAID', 'VOID')),
    subtotal     numeric(12,2) NOT NULL DEFAULT 0,
    extras_total numeric(12,2) NOT NULL DEFAULT 0,
    discount     numeric(12,2) NOT NULL DEFAULT 0,
    total        numeric(12,2) NOT NULL CHECK (total >= 0),
    -- Money taken before the guest arrived that is not returned on cancellation.
    non_refundable numeric(12,2) NOT NULL DEFAULT 0,
    issued_by    text,
    issued_at    timestamptz NOT NULL DEFAULT now(),
    paid_at      timestamptz,
    notes        text
);
CREATE INDEX IF NOT EXISTS idx_invoices_booking ON invoices(booking_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);

CREATE TABLE IF NOT EXISTS invoice_lines (
    line_id     bigserial PRIMARY KEY,
    invoice_id  uuid NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('ROOM', 'EXTRA', 'DISCOUNT')),
    description text NOT NULL,
    quantity    numeric(10,2) NOT NULL DEFAULT 1,
    unit_price  numeric(10,2) NOT NULL DEFAULT 0,
    amount      numeric(12,2) NOT NULL,
    -- A line the owner has promised not to refund, e.g. the booking advance.
    non_refundable boolean NOT NULL DEFAULT false,
    position    integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines(invoice_id);

-- ---------------------------------------------------------------------------
-- Audit trail for every sensitive action
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial PRIMARY KEY,
    actor      text NOT NULL,          -- phone or 'system'
    action     text NOT NULL,
    entity     text,
    entity_id  text,
    before     jsonb,
    after      jsonb,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);

-- ---------------------------------------------------------------------------
-- AI usage and estimated cost, so spend is visible and auditable
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_usage (
    usage_id     bigserial PRIMARY KEY,
    provider     text NOT NULL,
    model        text NOT NULL,
    purpose      text NOT NULL,        -- customer_reply | owner_command | summary | other
    input_tokens  integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_estimate numeric(10,6) NOT NULL DEFAULT 0,
    currency     text NOT NULL DEFAULT 'USD',
    conversation_id uuid REFERENCES conversations(conversation_id) ON DELETE SET NULL,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_at ON ai_usage(at);
