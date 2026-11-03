# Rate card

Sagar PG Hostel, Macherla. All figures confirmed by the owner.

Every rate below is stored in `db/seed.sql` and computed by `core/pricing.py`.
The system never invents a figure. To change a price, edit `db/seed.sql`, apply
it, and the new figure is used everywhere.

## Nightly and daily rates, per bed

| Room | Food | Night | Day |
|---|---|---:|---:|
| AC | with food | Rs 350 | Rs 450 |
| AC | no food | Rs 300 | Rs 350 |
| Non-AC | with food | Rs 250 | Rs 350 |
| Non-AC | no food | Rs 200 | Rs 250 |
| Dormitory | with food | Rs 200 | Rs 300 |
| Dormitory | no food | Rs 150 | Rs 200 |

Every room type is offered with and without food. Because both are offered, the
food choice can never be assumed: a request that omits it is refused rather than
priced on the cheaper option.

## Monthly (PG) rates, per bed

A monthly stay is agreed in whole months and is not pro-rated.

| Room | Food | Night only | Full day |
|---|---|---:|---:|
| AC | with food | Rs 8,000 | Rs 11,000 |
| AC | no food | Rs 5,000 | Rs 7,000 |
| Non-AC | with food | Rs 5,500 | Rs 5,500 |
| Non-AC | no food | Rs 4,000 | Rs 4,000 |
| Dormitory | with food | Rs 4,500 | Rs 4,500 |
| Dormitory | no food | Rs 3,500 | Rs 3,500 |

Only AC charges more for a full day. These are the same for every guest: there
is no student concession, and the system will not offer one. A guest who wants a
lower rate must speak to the owner.

## Extras

| Item | Price | Unit |
|---|---:|---|
| Locker | Rs 30 | per month |
| New bedsheet | Rs 50 | each |
| New pillow | Rs 50 | each |
| New towel | Rs 100 | each |

## Rooms and beds

| Room | Type | Beds |
|---|---|---:|
| 1NA | Non-AC | 5 |
| 4NA | Non-AC | 5 |
| 5NA | Non-AC | 5 |
| 6NA | Non-AC | 12 |
| 7NA | Non-AC | 4 |
| 2AC | AC | 7 |
| 3AC | AC | 10 |
| DORM | Dormitory | 20 (mat spaces) |

Totals: Non-AC 31 beds across 5 rooms, AC 17 beds across 2 rooms, dormitory 20
mat spaces. 68 beds in all.

Room 5NA was confirmed by the owner. The other seven rooms are the historic
list and are still marked unconfirmed in `db/seed.sql`.
