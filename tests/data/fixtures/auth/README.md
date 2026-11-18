# Authenticated fixtures (hand-constructed)

Built by `scripts/build_auth_fixtures.py` from the real anonymous recordings. No real session
or credential was used and none appears here. Dummy credentials used by tests:

- `YALECONNECT_COOKIE=CG.SessionID=FIXTURE-SESSION-COOKIE-7f3a9c2e5b1d; AWSALB=FIXTURE-ALB-STICKY`
- `YALECONNECT_ICS_FEEDS=https://yaleconnect.yale.edu/ical/yale/ical_personal_FIXTURE-ICS-TOKEN-4e8b1a6c9d2f.ics`

- 371 anonymous upcoming events; 273 SIGN_IN_REQUIRED locations filled from the pool of
  real public ICS LOCATION strings; 8 members-only events added (ids 2399001+).
- `list_upcoming14d_auth_collapsed.json`: a 3-event response, used to test count-collapse detection.
- `viewer_authenticated.json` / `viewer_expired.json`: session-probe outcomes.
- `cas_redirect.json` / `cas_login_page.html`: what an expired session looks like on cookie-auth pages.
- `personal_feed.ics`: a per-user ICS feed (favorites), locations included.
