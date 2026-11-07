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

-- ---------------------------------------------------------------------------
-- Frequently asked questions, answered without an AI call
-- The cheapest AI call is the one never made. A question the owner has already
-- answered in their own words is served straight from here, so the model is
-- reserved for what is genuinely new. Aliases hold the phrasings guests
-- actually use, because a trigram match cannot know that "khana" means "food".
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS faq (
    faq_id      bigserial PRIMARY KEY,
    question    text NOT NULL,                  -- the canonical question
    answer      text NOT NULL,                  -- the owner's own words
    category    text NOT NULL DEFAULT 'general',
    aliases     text[] NOT NULL DEFAULT '{}',   -- other ways guests ask it
    priority    integer NOT NULL DEFAULT 0,     -- higher wins a tie
    active      boolean NOT NULL DEFAULT true,
    source      text NOT NULL DEFAULT 'owner',  -- owner | seed
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (question)
);
CREATE INDEX IF NOT EXISTS idx_faq_trgm ON faq USING gin (question gin_trgm_ops);

-- What guests actually asked, and whether the FAQ could answer it. A row with
-- no faq_id is a question we could not answer cheaply: the list the owner
-- should turn into new entries.
CREATE TABLE IF NOT EXISTS faq_hits (
    hit_id     bigserial PRIMARY KEY,
    asked      text NOT NULL,
    faq_id     bigint REFERENCES faq(faq_id) ON DELETE SET NULL,
    score      real,
    answered   boolean NOT NULL DEFAULT false,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_faq_hits_missed ON faq_hits(at) WHERE answered = false;


-- ---------------------------------------------------------------------------
-- One booking, many beds  (a party of guests shares one Booking ID)
--
-- `bookings.bed_id` remains the primary bed for a single-guest stay and for
-- backward compatibility. A booking that covers more than one guest records
-- every bed here. The row lives in THIS table because the no-double-booking
-- guarantee has to be per bed, and a single column cannot express two beds.
-- The same exclusion constraint is applied here as on `bookings`, so the
-- guarantee holds no matter which table a stay is recorded in. Both are kept:
-- defence in depth, and neither can be bypassed by application code.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS booking_beds (
    booking_bed_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id     uuid NOT NULL REFERENCES bookings(booking_id) ON DELETE CASCADE,
    bed_id         text NOT NULL REFERENCES beds(bed_id) ON DELETE RESTRICT,
    check_in_at    timestamptz NOT NULL,
    check_out_at   timestamptz NOT NULL,
    status         text NOT NULL DEFAULT 'REQUESTED'
                   CHECK (status IN ('REQUESTED','CONFIRMED','CHECKED_IN','CHECKED_OUT',
                                     'CANCELLED','REJECTED','NO_SHOW','EXPIRED')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT booking_beds_dates_ordered CHECK (check_out_at > check_in_at),
    -- A bed appears once per booking: no accidental double assignment.
    CONSTRAINT booking_beds_unique_bed UNIQUE (booking_id, bed_id)
);

-- No two confirmed or in-house stays may occupy the same bed over overlapping
-- time ranges. This is the guarantee that stops a bed being sold twice.
ALTER TABLE booking_beds DROP CONSTRAINT IF EXISTS booking_beds_no_overlap;
ALTER TABLE booking_beds ADD CONSTRAINT booking_beds_no_overlap
    EXCLUDE USING gist (
        bed_id WITH =,
        tstzrange(check_in_at, check_out_at, '[)') WITH &&
    )
    WHERE (status IN ('CONFIRMED', 'CHECKED_IN'));

CREATE INDEX IF NOT EXISTS idx_booking_beds_bed_time
    ON booking_beds(bed_id, check_in_at, check_out_at);
CREATE INDEX IF NOT EXISTS idx_booking_beds_booking ON booking_beds(booking_id);

-- ---------------------------------------------------------------------------
-- Hostel facts: the things only the owner knows, in the owner's own words.
-- Address, amenities, timings. The assistant reads these; it never invents
-- them. Kept apart from `faq` because these are single authoritative values,
-- not questions, and are quoted verbatim rather than matched by similarity.
-- A fact is never a price: prices live in the rate card and nowhere else.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hostel_facts (
    fact_id    bigserial PRIMARY KEY,
    key        text NOT NULL UNIQUE,           -- e.g. 'address', 'amenity.wifi'
    value      text NOT NULL,
    category   text NOT NULL DEFAULT 'general',
    source     text NOT NULL DEFAULT 'owner',  -- owner | seed
    verified   boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hostel_facts_category ON hostel_facts(category);

-- ---------------------------------------------------------------------------
-- Where a guest's name came from, so the assistant can trust a name the guest
-- typed over one Meta supplied, and never present a WhatsApp handle as a
-- verified legal name. The profile name is a convenience, not an identity.
-- ---------------------------------------------------------------------------
ALTER TABLE guests ADD COLUMN IF NOT EXISTS wa_profile_name text;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS name_source text NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE guests DROP CONSTRAINT IF EXISTS guests_name_source_check;
ALTER TABLE guests ADD CONSTRAINT guests_name_source_check
    CHECK (name_source IN ('UNKNOWN','WHATSAPP_PROFILE','GUEST_TYPED','OWNER','IMPORT'));

-- Which beds a booking holds, denormalised for fast reads. Kept in step with
-- booking_beds by the booking service, and used to render a party's rooms
-- without a join on the hot path.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS beds_count integer NOT NULL DEFAULT 1;
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_beds_count_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_beds_count_check
    CHECK (beds_count > 0);

-- Backfill: every existing booking holds exactly its one bed.
INSERT INTO booking_beds (booking_id, bed_id, check_in_at, check_out_at, status)
SELECT b.booking_id, b.bed_id, b.check_in_at, b.check_out_at, b.status
FROM bookings b
WHERE NOT EXISTS (
    SELECT 1 FROM booking_beds bb WHERE bb.booking_id = b.booking_id
);

-- ---------------------------------------------------------------------------
-- Guest reviews, in the guests' own words and the owner's keeping.
--
-- The menu offers "Guest Reviews", so the assistant needs somewhere to read
-- them from. A review is only ever one the owner entered: the assistant quotes
-- what is stored and never invents a rating or a sentence. When there are none,
-- the guest is told so plainly rather than shown a blank card or a made-up
-- compliment. A review carries no price: a figure here could be quoted as
-- truth, and prices come from the rate card and nowhere else.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guest_reviews (
    review_id  bigserial PRIMARY KEY,
    guest_name text NOT NULL,
    rating     smallint,                        -- 1..5, or NULL when not given
    body       text NOT NULL,
    stayed_on  date,                            -- when they stayed, if known
    source     text NOT NULL DEFAULT 'owner',   -- owner | guest
    published  boolean NOT NULL DEFAULT true,   -- false hides it from guests
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT guest_reviews_rating_check
        CHECK (rating IS NULL OR (rating BETWEEN 1 AND 5))
);
CREATE INDEX IF NOT EXISTS idx_guest_reviews_published
    ON guest_reviews(published, created_at DESC);

-- ---------------------------------------------------------------------------
-- Two more steps in the booking's life, so the owner approves twice.
--
-- A request used to jump straight from REQUESTED to CONFIRMED. In practice the
-- owner wants two decisions: first "yes, take this booking and ask them to
-- pay", then "yes, I have seen the payment". Those are different decisions
-- taken at different times, and the guest must hear about both. Encoding them
-- as statuses means the second approval can never be mistaken for the first,
-- and a booking can never be confirmed without the owner having looked at the
-- money.
--
--   REQUESTED   the guest asked; the owner has not decided
--   ACCEPTED    the owner said yes; the guest is asked to pay
--   PROOF_SENT  the guest says they paid; the owner must check
--   CONFIRMED   the owner checked the money and confirmed the booking
--
-- The no-double-booking exclusion still only counts CONFIRMED and CHECKED_IN:
-- an ACCEPTED booking holds its bed through `bed_holds`, exactly as a
-- REQUESTED one did, so widening the status list does not widen the guarantee.
-- ---------------------------------------------------------------------------
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_status_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_status_check
    CHECK (status IN ('REQUESTED','ACCEPTED','PROOF_SENT','CONFIRMED','CHECKED_IN',
                      'CHECKED_OUT','CANCELLED','REJECTED','NO_SHOW','EXPIRED'));

ALTER TABLE booking_beds DROP CONSTRAINT IF EXISTS booking_beds_status_check;
ALTER TABLE booking_beds ADD CONSTRAINT booking_beds_status_check
    CHECK (status IN ('REQUESTED','ACCEPTED','PROOF_SENT','CONFIRMED','CHECKED_IN',
                      'CHECKED_OUT','CANCELLED','REJECTED','NO_SHOW','EXPIRED'));

-- When the owner accepted, and when they finally confirmed after seeing the
-- money. Both are stamped so the two approvals can be told apart in the audit.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS accepted_by text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS accepted_at timestamptz;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS final_confirmed_at timestamptz;

-- Where the guest's payment screenshot was stored, when they sent one. The
-- image itself lives on disk under PAYMENT_PROOF_DIR; this is the path. The
-- bot never reads the image -- it cannot -- it only files it and hands it to
-- the owner, who is the one who checks the money.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_proof_path text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_proof_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_bookings_awaiting_owner
    ON bookings(status) WHERE status IN ('REQUESTED','PROOF_SENT');

-- ---------------------------------------------------------------------------
-- Walk-in services: a fresh-up and a short rest.
--
-- These are sold at the counter, not booked as a bed for the night. A fresh-up
-- uses no bed at all; a rest uses a bed for a few hours and then gives it back,
-- so the same bed can still be sold for the night. Keeping them in their own
-- table, rather than in `bookings`, is what keeps the occupancy counts honest:
-- a rest is not a guest staying the night, and counting it as one would make
-- every vacancy figure wrong.
--
-- The price is the owner's and lives here, like every other rate. Nothing in
-- the code invents it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS services (
    service_code     text PRIMARY KEY,          -- FRESHUP | REST_3H
    name             text NOT NULL,
    price            numeric(10,2) NOT NULL CHECK (price >= 0),
    duration_minutes integer NOT NULL DEFAULT 0 CHECK (duration_minutes >= 0),
    needs_bed        boolean NOT NULL DEFAULT false,
    active           boolean NOT NULL DEFAULT true,
    notes            text
);

-- One sale of a service. The same two-step approval as a booking: the owner
-- accepts, the guest pays, the owner confirms.
CREATE TABLE IF NOT EXISTS service_orders (
    service_order_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference        text UNIQUE NOT NULL DEFAULT
                     ('SRV-' || to_char(now(), 'YYYY') || '-' ||
                      lpad(nextval('booking_ref_seq')::text, 6, '0')),
    service_code     text NOT NULL REFERENCES services(service_code) ON DELETE RESTRICT,
    guest_id         uuid NOT NULL REFERENCES guests(guest_id) ON DELETE RESTRICT,
    bed_id           text REFERENCES beds(bed_id) ON DELETE RESTRICT,
    starts_at        timestamptz NOT NULL DEFAULT now(),
    ends_at          timestamptz NOT NULL,
    amount           numeric(10,2) NOT NULL CHECK (amount >= 0),
    status           text NOT NULL DEFAULT 'REQUESTED'
                     CHECK (status IN ('REQUESTED','ACCEPTED','PROOF_SENT','CONFIRMED',
                                       'COMPLETED','CANCELLED','REJECTED')),
    source           text NOT NULL DEFAULT 'WHATSAPP',
    accepted_by      text,
    accepted_at      timestamptz,
    final_confirmed_at timestamptz,
    payment_proof_path text,
    payment_proof_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    notes            text,
    CONSTRAINT service_orders_dates_ordered CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_service_orders_status ON service_orders(status);
CREATE INDEX IF NOT EXISTS idx_service_orders_bed_time
    ON service_orders(bed_id, starts_at, ends_at);

-- A confirmed rest may not overlap another confirmed rest on the same bed.
-- The guarantee is the same one bookings get, in the same form.
ALTER TABLE service_orders DROP CONSTRAINT IF EXISTS service_orders_no_overlap;
ALTER TABLE service_orders ADD CONSTRAINT service_orders_no_overlap
    EXCLUDE USING gist (
        bed_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    )
    WHERE (status IN ('CONFIRMED') AND bed_id IS NOT NULL);

-- ---------------------------------------------------------------------------
-- Dated promotions. A discount is data, not code: the owner sets a window and a
-- percentage, and the pricing engine applies it. It is never typed into a reply
-- and never invented.
--
-- scope says which products it covers. 'DAILY' covers nightly and daily stays
-- only: monthly (PG) stays and the walk-in services are excluded by design, so
-- a fixed PG rate and a Rs 50 fresh-up are never quietly reduced.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promotions (
    promotion_id bigserial PRIMARY KEY,
    name         text NOT NULL,
    percent      numeric(5,2) NOT NULL CHECK (percent > 0 AND percent <= 100),
    starts_on    date NOT NULL,
    ends_on      date NOT NULL,
    scope        text NOT NULL DEFAULT 'DAILY'
                 CHECK (scope IN ('DAILY', 'MONTHLY', 'ALL')),
    active       boolean NOT NULL DEFAULT true,
    notes        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT promotions_window_ordered CHECK (ends_on >= starts_on)
);
CREATE INDEX IF NOT EXISTS idx_promotions_active ON promotions(active, starts_on, ends_on);
