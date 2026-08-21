# Sullivan V18.0 — Membership & AI Credit Foundation

V18 builds the internal membership system before Stripe is connected.

## Membership plans (placeholder pricing until validated)
- Trial: 1 free company AI demo, 1 seat
- Starter: $19/mo, 500 AI credits/month, 1 seat
- Business: $49/mo, 2,500 AI credits/month, 5 seats
- Pro: $99/mo, 10,000 AI credits/month, 15 seats

These are product settings, not active paid subscriptions yet.

## AI credits
- Transaction AI categorization: 1 credit
- Transaction ambiguity resolution: 1 credit
- Other future AI operations have configurable costs
- Normal accounting features do not consume AI credits
- Credits are checked before AI use and consumed only after a successful AI response
- Usage is logged per company

## Free AI demo
Each company gets one free transaction demo.
- User supplies transaction description + amount
- Sullivan AI suggests category/account and explains why
- Demo never posts to the ledger
- Demo can be used only once per company

## Team seats
- Each plan has a seat limit
- Invite creation checks available seats
- Invite redemption checks seats again
- Trial starts at one seat

## Billing foundation
Company records now include:
- subscription plan/status
- AI credit limit / used / billing period
- seat limit
- demo used flag
- future Stripe customer/subscription IDs

## Important
Stripe is intentionally NOT connected in V18.0. Plan buttons are disabled so Sullivan
cannot accidentally grant a paid plan without verified payment.

Production multi-tenant accounting-data isolation is still required before unrelated
real companies should share one Sullivan production database.
