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
-- Indicative rates. The owner must confirm these before any quote is sent.
-- ---------------------------------------------------------------------------
INSERT INTO rate_plans (name, room_type, food_included, daily_rate, hourly_rate, notes)
SELECT v.name, v.room_type, v.food_included, v.daily_rate, v.hourly_rate,
       'indicative rate, awaiting owner confirmation'
FROM (VALUES
    ('Non-AC with food',    'NON_AC', true,  350.00,  50.00),
    ('Non-AC without food', 'NON_AC', false, 300.00,  45.00),
    ('AC with food',        'AC',     true,  450.00,  60.00),
    ('AC without food',     'AC',     false, 400.00,  55.00),
    ('Dormitory mat',       'DORM',   false, 200.00,  30.00)
) AS v(name, room_type, food_included, daily_rate, hourly_rate)
WHERE NOT EXISTS (SELECT 1 FROM rate_plans rp WHERE rp.name = v.name);

-- ---------------------------------------------------------------------------
-- A flag the owner can read back, so nothing here is silently treated as live.
-- ---------------------------------------------------------------------------
INSERT INTO settings (key, value) VALUES
    ('configuration_confirmed', 'false'::jsonb),
    ('configuration_source', '"historic records supplied by the owner"'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
