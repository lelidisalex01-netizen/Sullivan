# Sullivan V17.2.2 — Account card + workspace manager

This patch keeps the working Google login from V17.2.1 and improves the signed-in experience.

Changes:
- Fixes the signed-in user's name/account text contrast in the left sidebar.
- Renames `Switch workspace` to `Manage workspace`.
- Adds a real workspace manager directly in the sidebar.
- Lets a signed-in user choose Personal or any company they belong to.
- Lets employees join a company using a one-time employer invite code.
- Lets a signed-in user create a company workspace.
- Does not change Google Cloud OAuth, Streamlit Secrets, or requirements.

Important production note:
The current Sullivan accounting database still needs full tenant/company data isolation
before multiple unrelated companies should use the same production instance.
