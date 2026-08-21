# Sullivan V17.1 — Guest-first access

V17.1 changes the account model so Sullivan is browseable without signing in.

## Guest mode
- Sullivan opens normally with no login wall.
- Guests can explore Home, Bank, Taxes, Reports and the rest of the interface.
- Real write actions are protected.
- Import/upload controls are disabled until sign-in.
- When a guest tries to save, create, post, import, record, reconcile, or otherwise change accounting data, Sullivan opens the sign-in panel.

## Sign-in panel
- Continue with Google (OAuth hook ready for production credentials)
- Continue with Apple (OAuth hook ready for production credentials)
- Continue with Email (local email/password works now)
- Clean light form styling

## Company employees
- Separate "Are you a company employee?" section.
- Employee signs into their personal Sullivan identity first.
- Then employee joins using the one-time employer invite code.
- Company names/Company IDs alone are not sufficient to gain access.

## Existing functionality
- V16 Easy Import retained
- V16 reconciliation retained
- V15/V16 UI retained
- V17 company/team database retained

Note: Google and Apple OAuth require provider credentials in the deployed production environment. The buttons are wired as provider entry points but do not fake authentication locally.
