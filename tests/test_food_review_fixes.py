"""Regression tests for the false-positive / miss review run on the 1,007 unlabeled real events."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DATA_DIR
from app.enrich.food import FoodClassifier
from app.schemas import FoodLabel


@pytest.fixture(scope="module")
def clf() -> FoodClassifier:
    return FoodClassifier.from_yaml(Path(DATA_DIR / "food_lexicon.yaml"))


def test_food_awarded_as_prize_is_not_free_food(clf):
    r = clf.classify(title="Women's Basketball vs. New Hampshire",
                     description_text="Come cheer on the Bulldogs! The ResCo with the highest attendance receives "
                                      "free pizza at halftime!")
    assert r.label != FoodLabel.YES


def test_cooking_as_the_activity_is_capped(clf):
    r = clf.classify(title="Turon Night @ AACC Kitchen",
                     description_text="Our chefs will guide attendees through the process of making turon, a "
                                      "banana-based Filipino snack.")
    assert r.label != FoodLabel.YES


def test_hedge_does_not_cross_sentence_boundary(clf):
    r = clf.classify(title="A Conversation: Queer Black Life at Yale", is_food_included=True,
                     food_details="Dominoes pizza",
                     description_text="We will discuss topics not often talked about on campus. Food will be provided.")
    assert r.label == FoodLabel.YES


def test_ticket_prose_does_not_veto_flagged_lunch(clf):
    r = clf.classify(title="Schwarzman Session: Accessibility as a Core Value", is_food_included=True,
                     description_text="He works with families and friends who buy tickets with them. "
                                      "Lunch will be provided!")
    assert r.label == FoodLabel.YES


def test_paid_ticket_near_food_still_vetoes_flagged_event(clf):
    r = clf.classify(title="The Game Watch Party", is_food_included=True, food_details="Pizza & Salad, Cash Bar",
                     description_text="Tickets are $25 and include pizza and salad.")
    assert r.label != FoodLabel.YES
