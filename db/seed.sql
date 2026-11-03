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
-- Rates confirmed by the owner on 2026-11-03.
--
-- The owner quotes a night rate and a day rate for the same room. Both are
-- stored and both are shown to the customer. Non-AC and AC rooms include food;
-- the dormitory does not, which the owner stated explicitly.
-- ---------------------------------------------------------------------------
INSERT INTO rate_plans (name, room_type, food_included, stay_mode, daily_rate, hourly_rate, notes)
SELECT v.name, v.room_type, v.food_included, v.stay_mode, v.daily_rate, v.hourly_rate,
       'confirmed by the owner 2026-11-03'
FROM (VALUES
    ('Non-AC with food per night', 'NON_AC', true,  'NIGHT', 200.00, NULL::numeric),
    ('Non-AC with food per day',   'NON_AC', true,  'DAY',   250.00, NULL::numeric),
    ('AC with food per night',     'AC',     true,  'NIGHT', 300.00, NULL::numeric),
    ('AC with food per day',       'AC',     true,  'DAY',   350.00, NULL::numeric),
    ('Dormitory mat per night',    'DORM',   false, 'NIGHT', 100.00, NULL::numeric),
    ('Dormitory mat per day',      'DORM',   false, 'DAY',   150.00, NULL::numeric),
    ('Dormitory with food per night', 'DORM', true, 'NIGHT', 300.00, NULL::numeric)
) AS v(name, room_type, food_included, stay_mode, daily_rate, hourly_rate)
WHERE NOT EXISTS (SELECT 1 FROM rate_plans rp WHERE rp.name = v.name);

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
    ('configuration_source', '"rates confirmed by the owner 2026-11-03; rooms still historic"'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
