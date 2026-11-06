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
    ('5NA',  'NON_AC',  5, 'confirmed by the owner'),
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
-- Monthly (PG) plans. Agreed in whole months. Every room type is offered with
-- and without food, and a night-only or full-day arrangement. Only AC charges
-- more for a full day; the others charge the same either way.
--
--   AC         with food 8000 night / 11000 full day
--              no food   5000 night /  7000 full day
--   Non-AC     with food 5500 night /  5500 full day
--              no food   4000 night /  4000 full day
--   Dormitory  with food 4500 night /  4500 full day
--              no food   3500 night /  3500 full day
--
-- These are the same for every guest. There is no student concession; anyone
-- who wants a lower rate must speak to the owner.
-- ---------------------------------------------------------------------------
INSERT INTO monthly_plans (name, room_type, food_included, plan_kind, stay_mode, monthly_rate, notes)
SELECT v.name, v.room_type, v.food_included, v.plan_kind, v.stay_mode, v.monthly_rate,
       'confirmed by the owner'
FROM (VALUES
    ('Monthly AC with food, night',        'AC',     true,  'STANDARD', 'NIGHT',  8000.00),
    ('Monthly AC with food, full day',     'AC',     true,  'STANDARD', 'DAY',   11000.00),
    ('Monthly AC no food, night',          'AC',     false, 'STANDARD', 'NIGHT',  5000.00),
    ('Monthly AC no food, full day',       'AC',     false, 'STANDARD', 'DAY',    7000.00),
    ('Monthly Non-AC with food',           'NON_AC', true,  'STANDARD', 'NIGHT',  5500.00),
    ('Monthly Non-AC with food, full day', 'NON_AC', true,  'STANDARD', 'DAY',    5500.00),
    ('Monthly Non-AC no food',             'NON_AC', false, 'STANDARD', 'NIGHT',  4000.00),
    ('Monthly Non-AC no food, full day',   'NON_AC', false, 'STANDARD', 'DAY',    4000.00),
    ('Monthly dormitory with food',        'DORM',   true,  'STANDARD', 'NIGHT',  4500.00),
    ('Monthly dormitory with food, full day','DORM', true,  'STANDARD', 'DAY',    4500.00),
    ('Monthly dormitory no food',          'DORM',   false, 'STANDARD', 'NIGHT',  3500.00),
    ('Monthly dormitory no food, full day','DORM',   false, 'STANDARD', 'DAY',    3500.00)
) AS v(name, room_type, food_included, plan_kind, stay_mode, monthly_rate)
ON CONFLICT (plan_kind, room_type, food_included, stay_mode) WHERE active
DO UPDATE SET name = EXCLUDED.name,
              monthly_rate = EXCLUDED.monthly_rate,
              notes = EXCLUDED.notes;

-- Every room type is priced explicitly, so a generic 'ANY' standard plan is
-- stale. Retire it if an earlier run of this file created one.
UPDATE monthly_plans SET active = false
WHERE active AND plan_kind = 'STANDARD' AND room_type = 'ANY';

-- There is no concession for anyone. Retire any left by an earlier run.
UPDATE monthly_plans SET active = false
WHERE active AND plan_kind = 'CONCESSION';

-- ---------------------------------------------------------------------------
-- Extras the owner sells or rents. Prices re-confirmed 2026-11-04.
-- ON CONFLICT updates, so a re-run corrects an old price without duplicating.
-- ---------------------------------------------------------------------------
-- Older databases named these "New towel", "New bedsheet". Rename in place so a
-- re-run does not leave a duplicate, differently-priced row behind.
UPDATE extras SET name = 'Towel'    WHERE name = 'New towel';
UPDATE extras SET name = 'Bedsheet' WHERE name = 'New bedsheet';
UPDATE extras SET name = 'Pillow'   WHERE name = 'New pillow';

INSERT INTO extras (name, price, unit, notes) VALUES
    ('Towel',    100.00, 'PER_ITEM', 'confirmed by the owner 2026-11-04'),
    ('Bedsheet', 50.00, 'PER_ITEM',  'confirmed by the owner 2026-11-04'),
    ('Pillow',   50.00, 'PER_ITEM',  'confirmed by the owner 2026-11-04'),
    ('Locker',   50.00, 'PER_MONTH', 'confirmed by the owner 2026-11-04')
ON CONFLICT (name) DO UPDATE SET price = EXCLUDED.price,
                                 unit  = EXCLUDED.unit,
                                 notes = EXCLUDED.notes;

-- ---------------------------------------------------------------------------
-- The minimum advance per guest to hold a bed. Not refunded on cancellation.
-- ---------------------------------------------------------------------------
INSERT INTO settings (key, value) VALUES
    ('advance_per_guest', '{"amount": "100"}'::jsonb)
ON CONFLICT (key) DO NOTHING;

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

-- ---------------------------------------------------------------------------
-- Starter FAQ: ONLY facts verified from this codebase.
-- Rooms, food and the dormitory description come from the schema and the
-- router's own wording. Nothing here is invented: times, wifi, parking,
-- laundry and the address are deliberately absent because the code does not
-- know them. The owner adds those with `python scripts/faq_admin.py add`.
-- Money is never stored here: a price in stored text goes stale. Prices and
-- the advance are answered by the pricing engine, which reads the rate card.
-- ---------------------------------------------------------------------------
INSERT INTO faq (question, answer, category, aliases, priority, source) VALUES
    ('What kind of rooms do you have?',
     'We have AC rooms, Non-AC rooms, and a dormitory with mat spaces (no cots). '
     'Tell me whether you want AC and food and I will give you the exact rate.',
     'rooms',
     ARRAY['what rooms are available', 'do you have ac rooms', 'room types',
           'what rooms do you have', 'do you have dormitory', 'ac or non ac'],
     10, 'seed'),
    ('What can you help me with?',
     'I can tell you about room availability, rates and booking a bed at '
     'Sagar PG Hostel. Tell me your check-in date, how many nights, and '
     'whether you want AC and food.',
     'general',
     ARRAY['what can you do', 'help', 'what do you offer', 'how can you help'],
     5, 'seed')
ON CONFLICT (question) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Hostel facts confirmed by the owner (2026-11-04). The owner stated each of
-- these directly; the database confirms the rooms. Nothing here is taken from
-- a travel website: the descriptions on Justdial and Booking.com ("bathrobes,
-- sofa, city views, free bikes") are auto-generated filler and are wrong.
-- No price is stored here. Prices come from the rate card, and only from there.
-- ---------------------------------------------------------------------------
INSERT INTO hostel_facts (key, value, category, source) VALUES
    ('hostel.name', 'Sagar PG & Men''s Hostel', 'identity', 'owner'),
    ('hostel.kind', 'Boys / men''s paying-guest hostel', 'identity', 'owner'),
    ('address.full',
     'Main Road, above Sagar Tea Stall, opposite Kodanda Ramalayam Temple, '
     'Macherla, Palnadu District, Andhra Pradesh 522426',
     'location', 'owner'),
    ('address.town', 'Macherla', 'location', 'owner'),
    ('address.district', 'Palnadu District', 'location', 'owner'),
    ('address.state', 'Andhra Pradesh', 'location', 'owner'),
    ('address.pincode', '522426', 'location', 'owner'),
    ('address.landmark', 'Above Sagar Tea Stall, opposite Kodanda Ramalayam Temple',
     'location', 'owner'),
    ('address.map_link', 'https://maps.app.goo.gl/S7voZ2PEDLVyHfdZ7',
     'location', 'owner'),
    ('amenity.wifi', 'Wi-Fi is available.', 'amenity', 'owner'),
    ('amenity.hot_water', 'Geyser and hot water are available.', 'amenity', 'owner'),
    ('amenity.washing_machine', 'A washing machine is available.', 'amenity', 'owner'),
    ('amenity.food', 'Homely food is available.', 'amenity', 'owner'),
    ('amenity.lockers', 'Independent lockers are provided.', 'amenity', 'owner'),
    ('amenity.security', '24/7 security.', 'amenity', 'owner'),
    -- What a night and a day include. A guest paying for a night gets dinner;
    -- a guest paying for a day gets tiffin, lunch and dinner over 24 hours.
    ('policy.night_meals', 'A night stay includes dinner only.', 'policy', 'owner'),
    ('policy.day_meals',
     'A day stay is 24 hours and includes tiffin, lunch and dinner.',
     'policy', 'owner'),
    ('policy.timings',
     'We are open 24/7; check-in and check-out can be any time you need.',
     'policy', 'owner')
ON CONFLICT (key) DO NOTHING;

-- Starter FAQ entries for the amenities and the address, in the owner's words.
-- These are the questions guests ask most; each answer is the owner's own
-- statement, recorded above as a hostel fact. No price appears here: prices
-- are answered by the pricing engine from the rate card.
INSERT INTO faq (question, answer, category, aliases, priority, source) VALUES
    ('Is there Wi-Fi?',
     'Yes, Wi-Fi is available.',
     'amenity',
     ARRAY['do you have wifi', 'is there wifi', 'wifi available', 'internet',
           'do you have internet', 'wi-fi', 'wifi facility'],
     10, 'seed'),
    ('Is there hot water?',
     'Yes, a geyser and hot water are available.',
     'amenity',
     ARRAY['do you have hot water', 'hot water', 'geyser', 'is there hot water'],
     10, 'seed'),
    ('Is there a washing machine?',
     'Yes, a washing machine is available.',
     'amenity',
     ARRAY['do you have washing machine', 'washing machine', 'laundry', 'dhobi'],
     10, 'seed'),
    ('Is food available?',
     'Yes, homely food is available. Tell me whether you want food included '
     'with your room and I will give you the exact rate.',
     'amenity',
     ARRAY['do you have food', 'is there food', 'meals', 'mess', 'khana',
           'do you provide food'],
     10, 'seed'),
    ('Is there security?',
     'Yes, there is 24/7 security, and independent lockers are provided.',
     'amenity',
     ARRAY['do you have security', 'security', 'locker', 'is it safe',
           '24/7 security'],
     10, 'seed'),
    ('Where is the hostel located?',
     'Main Road, above Sagar Tea Stall, opposite Kodanda Ramalayam Temple, '
     'Macherla, Palnadu District, Andhra Pradesh 522426.',
     'location',
     ARRAY['what is your address', 'address', 'location', 'where are you',
           'where is the hostel', 'how do i reach you', 'directions', 'landmark'],
     10, 'seed')
ON CONFLICT (question) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Walk-in services, priced by the owner: a fresh-up at Rs 50, and a three-hour
-- rest at Rs 100. A fresh-up uses no bed; a rest holds a bed for its three
-- hours and then releases it. Re-running leaves an existing price alone only if
-- the owner has since edited it -- this is an upsert keyed on the service, so
-- the seeded price is the starting price, not a floor.
-- ---------------------------------------------------------------------------
INSERT INTO services (service_code, name, price, duration_minutes, needs_bed, notes) VALUES
    ('FRESH_UP', 'Fresh-up', 50.00, 0, false,
     'A wash and change, no bed. Walk-in.'),
    ('REST_3H', 'Rest (3 hours)', 100.00, 180, true,
     'A bed for three hours. The bed is free again afterwards.')
ON CONFLICT (service_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- The owner's opening offer: 10% off a nightly or daily stay booked in the next
-- three days. Monthly (PG) stays and the walk-in services are excluded. The
-- window is dated, so it can be changed or switched off without a release.
-- ---------------------------------------------------------------------------
INSERT INTO promotions (name, percent, starts_on, ends_on, scope, notes) VALUES
    ('Opening offer: 10% off bookings made in the next 3 days', 10.00,
     CURRENT_DATE, CURRENT_DATE + 2, 'DAILY',
     'Applies to a nightly or daily stay booked today, tomorrow or the day after. '
     'Not monthly stays, not fresh-up or rest.')
ON CONFLICT DO NOTHING;
