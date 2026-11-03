-- Sagar PG Hostel — initial configuration from the owner's historic records.
--
-- NOT YET CONFIRMED. The owner must check the room list, the bed count per room
-- and the rates before this is treated as live. Prices here are the indicative
-- figures the owner supplied; they are editable and no quotation goes out until
-- the owner confirms them.
--
-- This file is safe to run more than once: it inserts missing rows and leaves
-- existing ones alone.

BEGIN;

-- ---------------------------------------------------------------------------
-- Rooms, as supplied historically.
-- ---------------------------------------------------------------------------
INSERT INTO rooms (room_id, room_type, total_beds, notes) VALUES
    ('1NA',  'NON_AC',  5, 'historic configuration, unconfirmed'),
    ('2AC',  'AC',      7, 'historic configuration, unconfirmed'),
    ('3AC',  'AC',     10, 'historic configuration, unconfirmed'),
    ('4NA',  'NON_AC',  5, 'historic configuration, unconfirmed'),
    ('6NA',  'NON_AC', 12, 'historic configuration, unconfirmed'),
    ('7NA',  'NON_AC',  4, 'historic configuration, unconfirmed'),
    ('DORM', 'DORM',   20, 'dormitory mat spaces, no cots; unconfirmed')
ON CONFLICT (room_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- One bed row per bed. Mat spaces in the dormitory are labelled M1..M20.
-- ---------------------------------------------------------------------------
INSERT INTO beds (bed_id, room_id, bed_type)
SELECT r.room_id || '-B' || g.n,
       r.room_id,
       CASE r.room_type WHEN 'AC' THEN 'AC' ELSE 'NON_AC' END
FROM rooms r
JOIN generate_series(1, r.total_beds) AS g(n) ON true
WHERE r.room_type <> 'DORM'
ON CONFLICT (bed_id) DO NOTHING;

INSERT INTO beds (bed_id, room_id, bed_type)
SELECT 'DORM-M' || g.n, 'DORM', 'MAT'
FROM generate_series(1, 20) AS g(n)
ON CONFLICT (bed_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Rates confirmed by the owner. Every room type is offered both with and
-- without food, and the owner quotes a night rate and a day rate for each, so
-- there are twelve figures in all.
--
-- This is an upsert keyed on the slot (room type, food option, stay mode):
-- re-running updates the price for a slot rather than leaving a stale rate
-- behind, which is what a rate change needs.
-- ---------------------------------------------------------------------------
INSERT INTO rate_plans (name, room_type, food_included, stay_mode, daily_rate, hourly_rate, notes)
SELECT v.name, v.room_type, v.food_included, v.stay_mode, v.daily_rate, v.hourly_rate,
       'confirmed by the owner'
FROM (VALUES
    ('AC with food per night',       'AC',     true,  'NIGHT', 350.00, NULL::numeric),
    ('AC with food per day',         'AC',     true,  'DAY',   450.00, NULL::numeric),
    ('AC without food per night',    'AC',     false, 'NIGHT', 300.00, NULL::numeric),
    ('AC without food per day',      'AC',     false, 'DAY',   350.00, NULL::numeric),
    ('Non-AC with food per night',   'NON_AC', true,  'NIGHT', 250.00, NULL::numeric),
    ('Non-AC with food per day',     'NON_AC', true,  'DAY',   350.00, NULL::numeric),
    ('Non-AC without food per night','NON_AC', false, 'NIGHT', 200.00, NULL::numeric),
    ('Non-AC without food per day',  'NON_AC', false, 'DAY',   250.00, NULL::numeric),
    ('Dormitory with food per night','DORM',   true,  'NIGHT', 200.00, NULL::numeric),
    ('Dormitory with food per day',  'DORM',   true,  'DAY',   300.00, NULL::numeric),
    ('Dormitory mat per night',      'DORM',   false, 'NIGHT', 150.00, NULL::numeric),
    ('Dormitory mat per day',        'DORM',   false, 'DAY',   200.00, NULL::numeric)
) AS v(name, room_type, food_included, stay_mode, daily_rate, hourly_rate)
ON CONFLICT (room_type, food_included, stay_mode) WHERE active
DO UPDATE SET name = EXCLUDED.name,
              daily_rate = EXCLUDED.daily_rate,
              hourly_rate = EXCLUDED.hourly_rate,
              notes = EXCLUDED.notes;

-- ---------------------------------------------------------------------------
-- Monthly (PG) plans. The standard month is Rs 5,500. A student with a valid
-- institution certificate, or anyone who negotiates, pays the concession rate
-- of Rs 5,000. Whether a guest qualifies is the owner's decision.
-- ---------------------------------------------------------------------------
INSERT INTO monthly_plans (name, room_type, food_included, plan_kind, monthly_rate, notes)
SELECT v.name, v.room_type, v.food_included, v.plan_kind, v.monthly_rate,
       'confirmed by the owner'
FROM (VALUES
    ('Monthly standard',           'ANY', true, 'STANDARD',   5500.00),
    ('Monthly concession',         'ANY', true, 'CONCESSION', 5000.00)
) AS v(name, room_type, food_included, plan_kind, monthly_rate)
ON CONFLICT (plan_kind, room_type, food_included) WHERE active
DO UPDATE SET name = EXCLUDED.name,
              monthly_rate = EXCLUDED.monthly_rate,
              notes = EXCLUDED.notes;

-- ---------------------------------------------------------------------------
-- Extras the owner sells or rents. Confirmed 2026-11-03.
-- ---------------------------------------------------------------------------
INSERT INTO extras (name, price, unit, notes) VALUES
    ('New bedsheet', 50.00, 'PER_ITEM', 'confirmed by the owner 2026-11-03'),
    ('New towel',   100.00, 'PER_ITEM', 'confirmed by the owner 2026-11-03'),
    ('New pillow',   50.00, 'PER_ITEM', 'confirmed by the owner 2026-11-03'),
    ('Locker',       30.00, 'PER_MONTH','confirmed by the owner 2026-11-03')
ON CONFLICT (name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- A flag the owner can read back, so nothing here is silently treated as live.
-- The rates above are confirmed. The room list still needs the owner's word.
-- ---------------------------------------------------------------------------
INSERT INTO settings (key, value) VALUES
    ('rates_confirmed', 'true'::jsonb),
    ('rates_confirmed_on', '"2026-11-03"'::jsonb),
    ('rooms_confirmed', 'false'::jsonb),
    ('configuration_source', '"rates confirmed by the owner; rooms still historic"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
