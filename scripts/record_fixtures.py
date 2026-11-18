"""Record real Yale Connect responses into tests/data/fixtures/ (operator-run only).

This is the only code in the repository that is meant to touch the live site outside the
running app. Tests never run it. It is sequential, waits ``--delay`` seconds between
requests, honours robots.txt, and never sends credentials.

    uv run python scripts/record_fixtures.py --out tests/data/fixtures
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import robotparser

import httpx

from app.sources import (
    BASE_URL,
    EVENT_DETAIL_QUERY,
    EVENTS_QUERY,
    GRAPHQL_URL,
    ICS_HELPER_URL,
    ICS_SCHOOL_URL,
    ROBOTS_URL,
    VIEWER_PROBE_QUERY,
    encode_event_global_id,
    events_variables,
    graphql_body,
)

KEEP_HEADERS = ("content-type", "etag", "last-modified", "location", "cache-control", "content-length")
# Groups whose per-group ICS feed is recorded: LGBTQ Center (busy), plus two others found on /ics_helper.
GROUP_FEEDS = (38473, 30725, 33342)


class Recorder:
    def __init__(self, out: Path, delay: float, user_agent: str) -> None:
        self.out = out
        self.delay = delay
        self.client = httpx.Client(headers={"User-Agent": user_agent}, timeout=60, follow_redirects=False)
        self.robots = robotparser.RobotFileParser()
        self.manifest: dict = {"files": {}}
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if url != ROBOTS_URL and url.startswith(BASE_URL) and not self.robots.can_fetch("*", url):
            raise RuntimeError(f"robots.txt disallows {url}")
        self._wait()
        return self.client.request(method, url, **kwargs)

    def save(self, rel: str, content: bytes, meta: dict, compress: bool = False) -> None:
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if compress:
            path = path.with_name(path.name + ".gz")
            rel = rel + ".gz"
            path.write_bytes(gzip.compress(content, mtime=0))
        else:
            path.write_bytes(content)
        self.manifest["files"][rel] = meta

    @staticmethod
    def meta(resp: httpx.Response, **extra) -> dict:
        headers = {k: v for k, v in resp.headers.items() if k.lower() in KEEP_HEADERS}
        return {"method": resp.request.method, "url": str(resp.request.url), "status": resp.status_code,
                "headers": headers, **extra}

    def robots_txt(self) -> None:
        resp = self.request("GET", ROBOTS_URL)
        self.robots.parse(resp.text.splitlines())
        self.robots.modified()
        self.save("robots/yaleconnect_robots.txt", resp.content, self.meta(resp))

    def graphql(self, rel: str, operation: str, query: str, variables: dict) -> dict:
        body = graphql_body(operation, query, variables)
        resp = self.request("POST", GRAPHQL_URL, json=body, headers={"Accept": "application/json"})
        resp.raise_for_status()
        self.save(rel, resp.content, self.meta(resp, request_json=body))
        return resp.json()

    def events_window(self, name: str, start: datetime, end: datetime) -> list[dict]:
        nodes: list[dict] = []
        after = None
        page = 1
        while True:
            variables = events_variables(iso(start), iso(end), after)
            data = self.graphql(f"graphql/list_{name}_p{page:02d}.json", "Events", EVENTS_QUERY, variables)
            conn = data["data"]["events"]
            nodes.extend(edge["node"] for edge in conn["edges"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
            page += 1
        self.manifest.setdefault("windows", {})[name] = {
            "from": iso(start), "to": iso(end), "pages": page, "events": len(nodes)}
        return nodes

    def ics(self) -> None:
        first = self.request("GET", ICS_SCHOOL_URL)
        self.save("ics/ical_yale_redirect.txt", first.content, self.meta(first))
        target = first.headers["location"]
        resp = self.request("GET", target)
        resp.raise_for_status()
        self.save("ics/ical_yale.ics", resp.content, self.meta(resp), compress=True)
        etag = resp.headers.get("etag")
        if etag:
            again = self.request("GET", target, headers={"If-None-Match": etag})
            self.save("ics/ical_yale_conditional.txt", again.content, self.meta(again, sent_if_none_match=etag))
        for gid in GROUP_FEEDS:
            url = f"{BASE_URL}/ical/yale/ical_club_{gid}.ics"
            hop = self.request("GET", url)
            resp = self.request("GET", hop.headers["location"]) if hop.status_code in (301, 302) else hop
            self.save(f"ics/ical_club_{gid}.ics", resp.content, self.meta(resp, requested=url))

    def html(self) -> None:
        resp = self.request("GET", ICS_HELPER_URL)
        self.save("html/ics_helper.html", resp.content, self.meta(resp), compress=True)
        url = f"{BASE_URL}/rsvp?id=2326515"
        chain = []
        for _ in range(6):
            resp = self.request("GET", url)
            chain.append(self.meta(resp))
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            url = httpx.URL(url).join(resp.headers["location"]).__str__()
        self.save("html/rsvp_spa_shell.html", resp.content, {"redirect_chain": chain})


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/data/fixtures"))
    parser.add_argument("--delay", type=float, default=0.6)
    parser.add_argument("--details", type=int, default=24, help="number of event(id) detail responses to record")
    parser.add_argument("--user-agent", default="yale-bites/0.1 (+fixture recorder; contact: operator)")
    args = parser.parse_args()

    rec = Recorder(args.out, args.delay, args.user_agent)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    rec.manifest["captured_at"] = iso(now)
    rec.robots_txt()

    upcoming = rec.events_window("upcoming14d", now, now + timedelta(days=14))
    past = rec.events_window("past14d", now - timedelta(days=14), now)
    rec.events_window("hot6h", now, now + timedelta(hours=6))

    # Detail responses: a spread of food-flagged, hidden-location, visible-location and multi-day events.
    chosen: list[dict] = []
    pools = (
        [n for n in upcoming + past if n.get("isFoodIncluded")],
        [n for n in upcoming if (n.get("location") or {}).get("eventLocationPrivacy") == "SIGN_IN_REQUIRED"],
        [n for n in upcoming if (n.get("location") or {}).get("eventLocationPrivacy") == "VISIBLE"],
        [n for n in upcoming if n.get("isMultiDay")],
    )
    per_pool = max(1, args.details // len(pools))
    for pool in pools:
        for node in pool[:per_pool]:
            if node not in chosen:
                chosen.append(node)
    for node in chosen:
        rec.graphql(f"graphql/detail_{node['id']}.json", "Event", EVENT_DETAIL_QUERY, {"id": node["id"]})
    rec.graphql("graphql/detail_not_found.json", "Event", EVENT_DETAIL_QUERY,
                {"id": encode_event_global_id(1)})
    rec.graphql("graphql/viewer_anonymous.json", "SessionProbe", VIEWER_PROBE_QUERY, {})

    rec.ics()
    rec.html()

    (args.out / "manifest.json").write_text(json.dumps(rec.manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(rec.manifest["windows"], indent=2))
    print(f"recorded {len(rec.manifest['files'])} files into {args.out}")


if __name__ == "__main__":
    main()
