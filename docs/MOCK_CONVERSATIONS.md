# Sagar PG Hostel — Sample WhatsApp Conversations

This file shows what the guest actually sees on WhatsApp, so the owner can check
the wording before it goes live. Every rupee figure below is computed by the
pricing engine from the rate card; the wording is the only thing here that a
person writes.

Legend: **Guest** = what the visitor sends. **Bot** = the automatic reply.
**Owner** = a message only you see, on your own number.

---

## 1. A new enquiry, then a booking

> **Guest:** hi

> **Bot:** Hello! What would you like to know about Sagar PG Hostel?

> **Bot:** Offer: 10% off the total when you book in the next 3 days (3 days
> left, till 08 November). Not on monthly stays or counter services.
> _[List: Rooms & Prices · Check Availability · Location & Directions · Food &
> Amenities · Guest Reviews · Book a Room · My Bookings · Fresh-up or Rest ·
> Talk to Staff]_

> **Guest:** _[taps Book a Room]_

> **Bot:** Happy to help you book. When would you like to check in?
> _[Today · Tomorrow · Day after tomorrow]_

> **Guest:** _[taps Tomorrow]_

> **Bot:** When would you like to check out?
> _[1 night · 2 nights · 3 nights · 1 week · 1 month]_

> **Guest:** _[taps 2 nights]_

> **Bot:** A night includes dinner only.
> A day is 24 hours and includes tiffin, lunch and dinner.
> Which would you like?
> _[Night (dinner) · Day (all meals)]_

> **Guest:** _[taps Night (dinner)]_

> **Bot:** We are open 24/7. What time would you like to check in? A night covers
> dinner and the night.
> _[Skip]_

> **Guest:** _[taps Skip]_

> **Bot:** How many guests? Tap a choice, or type the number (1 to 5).
> _[1 guest · 2 guests · …]_

> **Guest:** _[taps 1 guest]_

> **Bot:** Which room type would you like?
> _[AC · Non-AC · Dormitory]_

> **Guest:** _[taps Non-AC]_

> **Bot:** Would you like food included with the room?
> _[With food · No food]_

> **Guest:** _[taps With food]_

> **Bot:** How would you like to pay?
> _[Reservation · Full amount]_

> **Guest:** _[taps Reservation]_

> **Bot:** Please check your booking:
> - Room: Non-AC
> - Food: included
> - Check-in: 2026-11-08 at 9:00 PM
> - Check-out: 2026-11-10 at 9:00 AM
> - Stay: 2 nights
> - Guests: 1
> - Rate: Rs 250 per bed per night
> - Total: Rs 500
> - Reservation (pay now): Rs 200
> - Balance at check-in: Rs 300
>
> The reservation of Rs 200 is NOT refundable. It is part of your total, not an
> extra charge.
>
> Tap Submit and I will send this to the owner. The owner confirms the booking;
> nothing is held until they do.
> _[Submit · Start over · Cancel]_

> **Guest:** _[taps Submit]_

> **Bot:** Request SPG-2026-000123 sent to the owner.
> Room: Non-AC
> Check-in: 2026-11-08 at 9:00 PM
> Check-out: 2026-11-10 at 9:00 AM
> Guests: 1
> Total: Rs 500
> Reservation (pay now): Rs 200
> Balance at check-in: Rs 300
>
> Pay to UPI: 8688397996@postbank
> Send your payment screenshot here after paying.
>
> The reservation of Rs 200 is NOT refundable.
>
> Please WAIT for the final confirmation. The owner will first confirm your
> reservation and the payment, and only then is your booking confirmed. Your bed
> is not held until they do.
> Until then, please do not send another booking request. You can still ask me
> about prices, directions or facilities while you wait.

> **Owner (your number):** New booking request SPG-2026-000123 from 90000 00555.
> Non-AC, 2 nights, 1 guest, with food. Total Rs 500, reservation Rs 200.
> Reply `approve SPG-2026-000123` to take it, or `reject SPG-2026-000123`.

---

## 2. The guest waits (and cannot double-book)

While a request is with the owner, the guest cannot start a second one. Ordinary
questions — prices, directions, facilities — are still answered, so a waiting
guest is never left with nothing but the same line over and over.

> **Guest:** is my room confirmed?

> **Bot:** Your booking request SPG-2026-000123 for 2026-11-08 to 2026-11-10 is
> with the owner. It is waiting for the owner to confirm your reservation and
> payment. Total Rs 500.
> Please wait for the owner's final confirmation. Your bed is not held until they
> confirm.
>
> If you have not paid yet, pay to UPI: 8688397996@postbank and send the
> screenshot here.
> I cannot start another booking until the owner decides on this one. You can
> still ask me about prices, directions or facilities.

> **Guest:** _[taps Book a Room again]_

> **Bot:** _(the same waiting reply — no second request is created)_

---

## 3. The owner accepts, then confirms

> **Owner:** approve SPG-2026-000123

> **Bot (to owner):** Booking SPG-2026-000123 is ACCEPTED. The guest has been
> told to pay the advance to the UPI ID, with the payee name to check. Once they
> send the payment screenshot, you will be asked to confirm.

> **Guest:** _[sends a payment screenshot]_

> **Bot (to guest):** Thank you — your payment screenshot for SPG-2026-000123 has
> been passed to the owner. Please wait for the final confirmation.

> **Owner (your number):** SPG-2026-000123 now has a payment screenshot. Check it,
> then reply `approve SPG-2026-000123` again to confirm, or `reject SPG-2026-000123`.

> **Owner:** approve SPG-2026-000123

> **Bot (to guest):** Your booking is CONFIRMED.
> Reference: SPG-2026-000123
> Room: Non-AC · Bed 1NA-B1
> Check-in: 2026-11-08 at 9:00 PM
> Check-out: 2026-11-10 at 9:00 AM
> Guests: 1 · With food
> Total: Rs 500 · Paid: Rs 200 · Balance: Rs 300
>
> Please show this message at the counter. We look forward to seeing you.

---

## 4. Fresh-up (Rs 50) — a wash, no bed

> **Guest:** _[taps Fresh-up or Rest]_

> **Bot:** Here is what we offer at the counter:
> - Fresh-up: Rs 50
> - Three-hour rest: Rs 100
>
> Tap one to request it. The owner confirms it.
> _[Fresh-up · Three-hour rest]_

> **Guest:** _[taps Fresh-up]_

> **Bot:** Please send your full name as you would like it on the booking.

> **Guest:** Suresh

> **Bot:** Please check:
> - Service: Fresh-up
> - Price: Rs 50
>
> Tap Submit and I will send this to the owner. The owner confirms it; nothing is
> booked until they do.
> _[Submit · Cancel]_

> **Guest:** _[taps Submit]_

> **Bot:** Request SRV-2026-000045 sent to the owner.
> Service: Fresh-up
> Amount: Rs 50
>
> Pay to UPI: 8688397996@postbank
> Send your payment screenshot here after paying.
>
> Please WAIT for the final confirmation. The owner will first confirm your
> reservation and the payment, and only then is it confirmed.
> Until then, please do not send another request. You can still ask me about
> prices, directions or facilities while you wait.

---

## 5. Three-hour rest (Rs 100) — holds a bed, then releases it

> **Guest:** _[taps Fresh-up or Rest]_

> **Bot:** Here is what we offer at the counter:
> - Fresh-up: Rs 50
> - Three-hour rest: Rs 100
> _[Fresh-up · Three-hour rest]_

> **Guest:** _[taps Three-hour rest]_

> **Bot:** How long would you like the bed for? A three-hour rest is the usual.
> Tap below, or type the number of hours.
> _[1 hour · 2 hours · 3 hours]_

> **Guest:** _[taps 3 hours]_

> **Bot:** Which bed would you like? Tap one below.
> _[1NA-B1 · 1NA-B2 · 2AC-B1]_

> **Guest:** _[taps 1NA-B1]_

> **Bot:** Please send your full name as you would like it on the booking.

> **Guest:** Ravi

> **Bot:** Please check:
> - Service: Three-hour rest
> - Price: Rs 100
> - Bed: 1NA-B1
> - From: 08 Nov, 10:00 AM
> - Until: 08 Nov, 01:00 PM
>
> Tap Submit and I will send this to the owner. The owner confirms it; nothing is
> booked until they do.
> _[Submit · Cancel]_

> **Guest:** _[taps Submit]_

> **Bot:** Request SRV-2026-000046 sent to the owner.
> Service: Three-hour rest
> Amount: Rs 100
> Bed: 1NA-B1
>
> Pay to UPI: 8688397996@postbank
> Send your payment screenshot here after paying.
>
> Please WAIT for the final confirmation. The owner will first confirm your
> reservation and the payment, and only then is it confirmed.
> Until then, please do not send another request. You can still ask me about
> prices, directions or facilities while you wait.

Note: the bed is shown as busy only for those three hours. After 1:00 PM the same
bed can be sold for the night.

---

## 6. The 10% early-booking offer

Any normal booking **made within the next three days** gets **10% off the total**.
Monthly stays, fresh-ups and rests never get it.

> **Bot:** … Total: Rs 500
> Early-booking offer (10%): − Rs 50
> Payable total: Rs 450
> - Reservation (pay now): Rs 200
> - Balance at check-in: Rs 250
> …

Monthly example — no discount line, because the offer does not apply:

> **Bot:** … Monthly (PG) stay: Rs 8,000 for one month. No discount applies to
> monthly stays. …

---

## 7. A question the AI answers (free text)

> **Guest:** do you allow guests to keep a pet cat?

> **Bot:** _[answered by the assistant, using the facts stored in the system;
> it never quotes a price unless the pricing engine gave it one]_

If the AI is ever unsure, it passes the message to you rather than guessing.

---

## 8. Owner's own commands (your number only)

| You type | You get back |
| --- | --- |
| `bookings` | The live bookings, one line each, with the amount |
| `customers` | The guest list |
| `pending` | Requests and payments waiting for your decision |
| `approve SPG-…` | Take the booking and ask the guest to pay (first time); confirm it after the screenshot (second time) |
| `reject SPG-…` | Decline it; the guest is told and the bed is freed |
| `dues` | Who still owes money |
| `pause replies` | Stop auto-replies for a while; messages are still saved |
| `resume replies` | Start auto-replies again |

---

### What is deliberately NOT in these conversations

- No price is ever typed by a person or guessed by the AI. Every figure comes
  from the rate card, so two guests asking the same question get the same answer.
- The bot never confirms a booking. Only you do, twice: first to accept, then to
  confirm after the payment screenshot.
- No second booking request is accepted while one is live, so you never have to
  untangle duplicate holds.
