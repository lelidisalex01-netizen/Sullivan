# Sullivan V17.2 — Real Google Sign-In

V17.2 keeps Sullivan's guest-first interface and connects the existing
"Continue with Google" button to Streamlit native OIDC authentication.

## What changed
- Real Google login with `st.login()`
- Google identity is read from `st.user`
- Google users are automatically linked/created in Sullivan's existing user table
- Existing company memberships are preserved by email
- Google users can still join employers using Sullivan invite codes
- Google sign-out uses `st.logout()`
- Existing email/password login remains available
- Apple remains a placeholder for the next authentication step
- Authlib added to requirements

## Required Streamlit Secrets
Do NOT commit these values to GitHub.

[auth]
redirect_uri = "https://sullivan-accounting.streamlit.app/oauth2callback"
cookie_secret = "YOUR_PRIVATE_COOKIE_SECRET"
client_id = "YOUR_GOOGLE_CLIENT_ID"
client_secret = "YOUR_GOOGLE_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

## Required Google redirect URI
https://sullivan-accounting.streamlit.app/oauth2callback

## GitHub update
Replace the live repository's `app.py` and `requirements.txt` with the V17.2
versions and commit them to `main`. Streamlit Community Cloud should redeploy
the existing Sullivan app automatically.
