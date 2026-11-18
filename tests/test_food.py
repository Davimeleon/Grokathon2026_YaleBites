"""Unit tests for the lexicon-driven free-food classifier and free-to-attend detector."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import REPO_ROOT
from app.enrich.food import FoodClassifier, quote
from app.schemas import FoodLabel

LEXICON = REPO_ROOT / "data" / "food_lexicon.yaml"


@pytest.fixture(scope="module")
def clf() -> FoodClassifier:
    return FoodClassifier.from_yaml(LEXICON)


def label(clf: FoodClassifier, description: str, *, title: str = "Weekly meeting", details: str | None = None,
          flag: bool | None = None) -> FoodLabel:
    return clf.classify(title=title, description_text=description, food_details=details,
                        is_food_included=flag).label


def rules(clf: FoodClassifier, description: str, **kw) -> set[str]:
    result = clf.classify(title=kw.get("title", "Weekly meeting"), description_text=description,
                          food_details=kw.get("details"), is_food_included=kw.get("flag"))
    return {e.rule for e in result.evidence}


@pytest.mark.parametrize("text", [
    "Free food for everyone who comes!",
    "Lunch will be served at noon.",
    "Light refreshments will be provided.",
    "Snacks will be served.",
    "Dinner provided!",
    "The event is catered.",
    "Food will be provided.",
    "We will be serving pizza after the talk.",
    "Grab some boba and bagels with us.",
    "Insomnia cookies and donuts at the meeting.",
    "Come for great food and conversation.",
])
def test_positive_phrases_are_yes(clf, text):
    assert label(clf, text) is FoodLabel.YES


def test_local_vendor_contributes(clf):
    assert "local_vendor" in rules(clf, "Dinner from Junzi for all attendees.")
    assert label(clf, "Dinner from Junzi for all attendees.") is FoodLabel.YES


@pytest.mark.parametrize("text,title", [
    ("Join us for a talk on foreign policy.", "NSG Lunch Talk"),
    ("Coffee will be provided before the session.", "Writing workshop"),
    ("Stop by for a cup of tea.", "Thursday Tea Time"),
    ("Cleary invites you to our 1L Reception.", "Firm reception"),
    ("We sometimes just have snacks and good company.", "Study commons"),
    ("Cookie decorating with the club!", "Cookie Decorating"),
])
def test_likely_cases(clf, text, title):
    assert label(clf, text, title=title) is FoodLabel.LIKELY


@pytest.mark.parametrize("text", [
    "Pizza will be available for purchase.",
    "Food trucks will be selling tacos outside.",
    "Our annual bake sale returns! Cookies and brownies.",
    "This is a ticketed event; dinner included with your ticket.",
    "Bring your own lunch and chat in Spanish.",
    "We meet at the restaurant; everyone will pay for their own meal.",
    "A panel on food insecurity in New Haven, dinner discussion to follow.",
    "Volunteer at the food bank sorting donations.",
    "Postgrad CV workshop, ZOOM ONLY. Dinner will be served!",
    "Pizza for volunteers only.",
    "There will be a cash bar.",
])
def test_negative_and_veto_families_are_no(clf, text):
    assert label(clf, text) is FoodLabel.NO


@pytest.mark.parametrize("text,expected", [
    ("Coffee chat at Starbucks.", FoodLabel.LIKELY),
    ("We discuss key provisions of the Act.", FoodLabel.NO),
    ("How the court treats precedent.", FoodLabel.NO),
])
def test_vendor_with_drink_and_polysemous_nouns_are_not_yes(clf, text, expected):
    assert label(clf, text) is expected


def test_food_topic_does_not_veto_explicitly_provided_lunch(clf):
    assert label(clf, "Careers in food systems. Lunch provided!") is FoodLabel.YES


def test_catering_count_is_positive(clf):
    text = "RSVP required for catering count."
    assert "catering_headcount" in rules(clf, text)
    assert label(clf, text) is FoodLabel.YES
    assert label(clf, "Please share any dietary restrictions when you register.") is FoodLabel.YES


def test_proximity_window_near_vs_far(clf):
    near = "Come for free lunch with the dean."
    far = ("Admission is free for everyone and the program runs all afternoon with talks from several "
           "speakers on many topics before we break for lunch.")
    assert "free_near_food" in rules(clf, near)
    assert label(clf, near) is FoodLabel.YES
    assert "free_near_food" not in rules(clf, far)
    assert label(clf, far) is FoodLabel.LIKELY


def test_proximity_ignores_feel_free_and_gluten_free(clf):
    assert "free_near_food" not in rules(clf, "Feel free to stop by; lunch talk on gluten-free lunch options.")


def test_dollar_near_food_vetoes(clf):
    result = clf.classify(title="Gala", description_text="$15 dinner tickets at the door.", food_details=None,
                          is_food_included=True)
    assert result.label is FoodLabel.NO
    assert result.evidence[0].rule in {"dollar_near_food", "ticketed"}
    assert label(clf, "Lunch is $12 per plate.") is FoodLabel.NO


def test_flag_alone_is_yes_and_flag_with_cash_bar_food_is_yes(clf):
    assert label(clf, "General meeting.", flag=True) is FoodLabel.YES
    assert label(clf, "Holiday party.", details="Pizza & salad, cash bar", flag=True) is FoodLabel.YES
    assert label(clf, "Holiday party at the pub.", details="Cash bar", flag=True) is FoodLabel.NO


def test_flag_with_drinks_only_details_is_likely(clf):
    assert label(clf, "Tabling on the quad.", details="apple cider", flag=True) is FoodLabel.LIKELY


def test_no_food_mention_is_no(clf):
    result = clf.classify(title="Team meeting", description_text="Weekly updates.")
    assert result.label is FoodLabel.NO
    assert result.evidence == ()


def test_evidence_is_verbatim_ordered_and_bounded(clf):
    title = "Pizza Night"
    description = ("Join us after the lecture. " * 8) + "Free pizza and drinks will be provided for everyone!"
    details = "Pizza!!"
    result = clf.classify(title=title, description_text=description, food_details=details, is_food_included=True)
    assert 0.0 <= result.score <= 1.0
    sources = {"title": title, "description": description, "food_details": details}
    for span in result.evidence:
        assert len(span.text) <= 120
        if span.field != "food_flag":
            assert span.text in sources[span.field]
    weights = [abs(e.weight) for e in result.evidence]
    assert weights == sorted(weights, reverse=True)


def test_title_weighs_more_than_description(clf):
    in_title = clf.classify(title="Pizza", description_text="Meeting.")
    in_body = clf.classify(title="Meeting", description_text="Pizza.")
    assert in_title.score > in_body.score


def test_quote_falls_back_to_match_when_sentence_too_long():
    text = "word " * 40 + "pizza " + "word " * 40
    start = text.index("pizza")
    assert quote(text, start, start + 5, 120) == "pizza"
    assert quote("Hi. Pizza here! Bye.", 4, 9, 120) == "Pizza here!"


def test_yaml_reload_changes_behavior(tmp_path: Path):
    config = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    base = FoodClassifier(config)
    assert base.classify(title="Meeting", description_text="Kale will be handed out.").label is FoodLabel.NO
    config["rules"].append({"id": "kale", "group": "kale", "kind": "positive", "weight": 5.0,
                            "pattern": r"\bkale\b"})
    config["scoring"]["thresholds"]["yes"] = 0.9
    path = tmp_path / "lexicon.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    reloaded = FoodClassifier.from_yaml(path)
    result = reloaded.classify(title="Meeting", description_text="Kale will be handed out.")
    assert result.label is FoodLabel.YES
    assert result.evidence[0].rule == "kale"


@pytest.mark.parametrize("cost_raw,description,expected", [
    ("RSVP: free ($0.00 USD); capacity: unlimited; external registration: https://example.org/x", "Talk.", True),
    ("RSVP: 0 USD", "Talk.", True),
    ("General: $12.50 USD", "Talk.", False),
    ("Thursday 6PM ($10): 0 USD", "Join us.", False),
    ("RSVP: 0 USD", "After you RSVP, please Venmo @club $10 within 24 hours.", False),
    (None, "$10 tickets at the door.", False),
    (None, "Suggested donation of $5.", False),
    (None, "This event is free and open to all.", True),
    (None, "Donations are welcome but not required.", None),
    (None, "A talk about birds.", None),
    ("External registration: https://luma.com/abc", "A talk about birds.", None),
])
def test_free_to_attend(clf, cost_raw, description, expected):
    result = clf.free_to_attend(cost_raw=cost_raw, title="Event", description_text=description)
    assert result.is_free_to_attend is expected
    if expected is not None:
        assert result.evidence
        assert all("http" not in e for e in result.evidence)
