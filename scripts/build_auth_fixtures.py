"""Build sanitized, hand-constructed authenticated-response fixtures (offline).

No authenticated response was ever captured: these are derived deterministically from
the real anonymous recordings so the authenticated code path can be built and tested
without a session. What an authenticated session changes, per the anonymous evidence:

* ``location.eventLocationPrivacy == SIGN_IN_REQUIRED`` events get ``location.name`` (and
  sometimes an address) filled in. The privacy flag itself stays SIGN_IN_REQUIRED.
* ``REGISTRATION_REQUIRED`` locations stay hidden (they need a registration, not a login).
* Events restricted to signed-in users are added to the list (they are simply absent from
  anonymous responses). We add a handful with fresh ids above any real id.

    uv run python scripts/build_auth_fixtures.py
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import re
from pathlib import Path

FIX = Path(__file__).resolve().parent.parent / "tests" / "data" / "fixtures"
OUT = FIX / "auth"

DUMMY_COOKIE = "CG.SessionID=FIXTURE-SESSION-COOKIE-7f3a9c2e5b1d; AWSALB=FIXTURE-ALB-STICKY"
DUMMY_ICS_TOKEN = "FIXTURE-ICS-TOKEN-4e8b1a6c9d2f"
PERSONAL_FEED_URL = f"https://yaleconnect.yale.edu/ical/yale/ical_personal_{DUMMY_ICS_TOKEN}.ics"

MEMBERS_ONLY = [
    ("Pierson Senior Class Dinner", "Pierson College", "Pierson College Dining Hall, 261 Park St, New Haven, CT 06511",
     "Seniors only. Dinner will be provided by Pierson dining; please RSVP for the catering count.", True,
     "Catered dinner", 0, 23, 0, 25, 0),
    ("YSEA Members Study Break", "Yale Society of Engineering Alumni", "Dunham Laboratory, Room 220, 10 Hillhouse Ave",
     "Members-only study break. Free boba and dumplings while supplies last.", True, "Boba, dumplings", 1, 20, 30, 21, 30),
    ("Board Meeting (closed)", "Yale Undergraduate Consulting Group", "William L. Harkness Hall (WLH) 112",
     "Closed board meeting for elected officers.", False, None, 1, 22, 0, 23, 0),
    ("Ezra Stiles Fellows Lunch", "Ezra Stiles College", "Ezra Stiles College, Fellows Lounge, 19 Tower Pkwy",
     "Lunch with the Head of College for fellows. Sandwiches from Atticus.", True, "Atticus sandwiches", 2, 17, 0, 18, 0),
    ("Members Movie Night", "Yale Film Society", "Humanities Quadrangle, Room L02, 320 York Street",
     "Members only screening. Snacks will be served.", False, None, 2, 0, 0, 2, 30),
    ("Grad Writing Group Check-in", "Graduate Writing Lab", "Sterling Memorial Library, Room 218, 120 High St",
     "Weekly check-in for enrolled participants. Bring your own lunch.", False, None, 3, 17, 30, 18, 30),
    ("Dorm Council Pizza Social", "Davenport College Council", "Davenport College Common Room, 248 York St",
     "Pizza for Davenport residents. Free.", True, "Pizza", 3, 23, 0, 24, 0),
    ("Executive Committee Retreat Planning", "Yale College Council", "17 Hillhouse Ave, Room 101",
     "Planning session for the spring retreat. Dinner available for purchase at the cafe downstairs.", False,
     None, 4, 22, 0, 23, 30),
]


def gid(n: int) -> str:
    return base64.b64encode(f"yale:Event:{n}".encode()).decode()


def iso(day_offset: int, hour: int, minute: int, base: str) -> str:
    from datetime import datetime, timedelta

    start = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(hour=0, minute=0)
    return (start + timedelta(days=day_offset, hours=hour, minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def real_location_pool() -> list[str]:
    text = gzip.decompress((FIX / "ics" / "ical_yale.ics.gz").read_bytes()).decode()
    text = re.sub(r"\r?\n[ \t]", "", text)
    seen: dict[str, None] = {}
    for match in re.finditer(r"^LOCATION:(.*)$", text, re.M):
        value = match.group(1).replace("\\,", ",").strip()
        if not value or "Sign in to download" in value or value in {"-", "Online Event", "TBD"}:
            continue
        if re.search(r"\d", value) or "Hall" in value or "College" in value:
            seen.setdefault(value, None)
    return sorted(seen)


def pick(pool: list[str], key: str) -> str:
    digest = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    return pool[digest % len(pool)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((FIX / "manifest.json").read_text())
    window = manifest["windows"]["upcoming14d"]
    pool = real_location_pool()

    nodes: list[dict] = []
    for page in sorted((FIX / "graphql").glob("list_upcoming14d_p*.json")):
        nodes.extend(edge["node"] for edge in json.loads(page.read_text())["data"]["events"]["edges"])

    revealed = 0
    auth_nodes = []
    for node in nodes:
        node = copy.deepcopy(node)
        loc = node.get("location") or {}
        if loc.get("eventLocationPrivacy") == "SIGN_IN_REQUIRED" and not loc.get("name"):
            loc["name"] = pick(pool, node["id"])
            loc["kind"] = loc.get("kind") or "ON_CAMPUS"
            loc["isToBeDetermined"] = False
            node["location"] = loc
            revealed += 1
        auth_nodes.append(node)

    next_id = 2399001
    for i, (title, group, location, desc, food, food_details, day, h1, m1, h2, m2) in enumerate(MEMBERS_ONLY):
        start = iso(day, h1, m1, window["from"])
        end = iso(day + (1 if h2 >= 24 else 0), h2 % 24, m2, window["from"])
        auth_nodes.append({
            "id": gid(next_id + i), "name": title, "startDate": start, "endDate": end,
            "timezone": "America/New_York", "isMultiDay": False, "status": "APPROVED",
            "customTimeInstruction": None, "attendanceMode": "IN_PERSON", "isFoodIncluded": food,
            "foodDetails": food_details, "descriptionText": desc, "descriptionHtml": "",
            "shareUrl": f"https://cglink.me/2dA/r{next_id + i}", "type": {"value": "Meeting"}, "tags": [],
            "hostGroup": {"id": base64.b64encode(f"yale:Group:{90000 + i}".encode()).decode(), "name": group},
            "cohostGroups": [], "parent": None,
            "location": {"kind": "ON_CAMPUS", "name": location, "isToBeDetermined": False,
                         "eventLocationPrivacy": "SIGN_IN_REQUIRED", "address": None, "coordinates": None},
            "onlineMeeting": None, "externalRegistration": None,
            "registration": {"capacityMode": "LIMITED", "registrationUrl": None,
                             "options": [{"name": "RSVP", "price": {"amount": 0, "currency": "USD", "isFlexible": False}}]},
        })

    auth_nodes.sort(key=lambda n: (n["startDate"], n["endDate"] or "", n["name"] or ""))
    for old in OUT.glob("list_upcoming14d_auth_p*.json"):
        old.unlink()
    pages = [auth_nodes[i:i + 50] for i in range(0, len(auth_nodes), 50)]
    for idx, chunk in enumerate(pages, start=1):
        has_next = idx < len(pages)
        body = {"data": {"events": {
            "totalCount": len(auth_nodes),
            "pageInfo": {"endCursor": f"FIXTURE-AUTH-CURSOR-{idx:02d}" if has_next else None, "hasNextPage": has_next},
            "edges": [{"node": n} for n in chunk]}}}
        (OUT / f"list_upcoming14d_auth_p{idx:02d}.json").write_text(json.dumps(body, ensure_ascii=False))

    collapsed = {"data": {"events": {"totalCount": 3, "pageInfo": {"endCursor": None, "hasNextPage": False},
                                     "edges": [{"node": n} for n in auth_nodes[:3]]}}}
    (OUT / "list_upcoming14d_auth_collapsed.json").write_text(json.dumps(collapsed, ensure_ascii=False))

    (OUT / "viewer_authenticated.json").write_text(json.dumps({"data": {"viewer": {"latestHubId": base64.b64encode(b"yale:Hub:1").decode()}}}))
    anon = json.loads((FIX / "graphql" / "viewer_anonymous.json").read_text())
    anon["errors"][0]["extensions"]["traceId"] = "00-fixture-expired-session-trace-00"
    (OUT / "viewer_expired.json").write_text(json.dumps(anon))

    cas_location = ("https://secure.its.yale.edu/cas/login?service="
                    "https%3A%2F%2Fyaleconnect.yale.edu%2Flogin_only%3Fredirect%3D%252Fics_helper")
    (OUT / "cas_redirect.json").write_text(json.dumps(
        {"status": 302, "headers": {"location": cas_location, "content-length": "0"}, "body": ""}, indent=2))
    (OUT / "cas_login_page.html").write_text(
        "<!DOCTYPE html>\n<html lang=\"en\"><head><title>CAS - Central Authentication Service</title></head>\n"
        "<body><div id=\"login\"><h1>Yale University Central Authentication Service</h1>\n"
        "<form id=\"fm1\" method=\"post\" action=\"/cas/login?service=https%3A%2F%2Fyaleconnect.yale.edu%2Flogin_only\">\n"
        "<label for=\"username\">NetID</label><input id=\"username\" name=\"username\" type=\"text\"/>\n"
        "<label for=\"password\">Password</label><input id=\"password\" name=\"password\" type=\"password\"/>\n"
        "<input type=\"hidden\" name=\"execution\" value=\"FIXTURE\"/><input type=\"submit\" value=\"LOGIN\"/>\n"
        "</form></div></body></html>\n")

    personal = build_personal_feed(auth_nodes, pool)
    (OUT / "personal_feed.ics").write_text(personal)

    (OUT / "README.md").write_text(
        "# Authenticated fixtures (hand-constructed)\n\n"
        "Built by `scripts/build_auth_fixtures.py` from the real anonymous recordings. No real session\n"
        "or credential was used and none appears here. Dummy credentials used by tests:\n\n"
        f"- `YALECONNECT_COOKIE={DUMMY_COOKIE}`\n"
        f"- `YALECONNECT_ICS_FEEDS={PERSONAL_FEED_URL}`\n\n"
        f"- {len(nodes)} anonymous upcoming events; {revealed} SIGN_IN_REQUIRED locations filled from the pool of\n"
        f"  real public ICS LOCATION strings; {len(MEMBERS_ONLY)} members-only events added (ids 2399001+).\n"
        "- `list_upcoming14d_auth_collapsed.json`: a 3-event response, used to test count-collapse detection.\n"
        "- `viewer_authenticated.json` / `viewer_expired.json`: session-probe outcomes.\n"
        "- `cas_redirect.json` / `cas_login_page.html`: what an expired session looks like on cookie-auth pages.\n"
        "- `personal_feed.ics`: a per-user ICS feed (favorites), locations included.\n")
    (OUT / "auth_fixture_meta.json").write_text(json.dumps({
        "dummy_cookie": DUMMY_COOKIE, "dummy_ics_token": DUMMY_ICS_TOKEN, "personal_feed_url": PERSONAL_FEED_URL,
        "anonymous_events": len(nodes), "authenticated_events": len(auth_nodes), "revealed_locations": revealed,
        "members_only_ids": [next_id + i for i in range(len(MEMBERS_ONLY))], "pages": len(pages)}, indent=2))
    print(f"auth fixtures: {len(auth_nodes)} events over {len(pages)} pages; {revealed} locations revealed")


def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_personal_feed(nodes: list[dict], pool: list[str]) -> str:
    chosen = [n for n in nodes if (n.get("location") or {}).get("name")][:6]
    chosen += [n for n in nodes if base64.b64decode(n["id"]).decode().endswith(("2399001", "2399004"))]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:CAMPUSGROUPS", "X-WR-CALNAME:My Favorite Events",
             "X-PUBLISHED-TTL:PT1H"]
    for n in chosen:
        num = base64.b64decode(n["id"]).decode().rsplit(":", 1)[1]
        start = n["startDate"].replace("-", "").replace(":", "")
        end = (n["endDate"] or n["startDate"]).replace("-", "").replace(":", "")
        lines += [
            "BEGIN:VEVENT", "DTSTAMP:20261117T060000Z", "LAST-MODIFIED:20261116T120000Z",
            "CREATED:20261101T120000Z", "SEQUENCE:1",
            f"ORGANIZER;CN=\"{(n.get('hostGroup') or {}).get('name') or 'Yale'}\":mailto:noreply@yaleconnect.yale.edu",
            f"CONTACT:https://yaleconnect.yale.edu/group/rsvp_boot?id={num}#event_host",
            f"DTSTART:{start}", f"DTEND:{end}",
            f"UID:fixture-personal-{num}@campusgroups.com",
            f"SUMMARY;ENCODING=QUOTED-PRINTABLE:{ics_escape(n['name'])}",
            f"LOCATION:{ics_escape(n['location']['name'])}",
            f"URL:https://yaleconnect.yale.edu/rsvp?id={num}",
            f"DESCRIPTION:{ics_escape((n.get('descriptionText') or '')[:400])}\\n---\\nEvent Details: "
            f"https://yaleconnect.yale.edu/rsvp?id={num}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


if __name__ == "__main__":
    main()
