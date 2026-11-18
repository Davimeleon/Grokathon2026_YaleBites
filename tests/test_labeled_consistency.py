"""The labeled set is evaluated on the same inputs production feeds the classifier.

Exception, documented in docs/sources.md: for a few events Yale Connect's ``descriptionText`` and
``descriptionHtml`` disagree (the plain-text copy is stale). Production prefers the HTML; the gold
labels were made on the plain text, so those rows keep the text the annotators judged.
"""

from __future__ import annotations

import json
import re

from app.parse.graphql import parse_event_nodes
from app.schemas import Visibility
from tests.conftest import FIXTURES_DIR, fixture_pages

LABELED = FIXTURES_DIR.parent / "labeled.jsonl"


def _parsed_and_nodes():
    nodes = [e["node"] for name in ("upcoming14d", "past14d") for page in fixture_pages(name)
             for e in page["data"]["events"]["edges"]]
    parsed, errors = parse_event_nodes(nodes, visibility=Visibility.PUBLIC)
    assert not errors
    by_url = {p.event.source_url: p for p in parsed}
    raw_text = {p.event.source_url: n.get("descriptionText") for p, n in zip(parsed, nodes, strict=True)}
    return by_url, raw_text


def test_labeled_inputs_match_parser_output():
    by_url, raw_text = _parsed_and_nodes()
    rows = [json.loads(line) for line in LABELED.read_text().splitlines()]
    assert len(rows) == 250 and len({r["source_url"] for r in rows}) == 250
    stale = 0
    for row in rows:
        p = by_url[row["source_url"]]
        assert row["title"] == p.event.title
        assert row["food_details"] == p.extras.food_details
        assert row["is_food_included"] == p.extras.is_food_included
        assert row["cost_raw"] == p.event.cost_raw
        assert row["gold_food"] in {"yes", "likely", "no"} and row["gold_free"] in {"yes", "no", "unknown"}
        if row["description_source"] == "parser":
            assert row["description_text"] == p.event.description_text
        else:
            stale += 1
            plain = re.sub(r"\s+", " ", raw_text[row["source_url"]] or "").strip()
            assert row["description_text"] == plain != p.event.description_text
    assert stale <= 20
