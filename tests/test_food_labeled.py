"""Measure the free-food classifier against the 250 adjudicated gold labels in tests/data/labeled.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT
from app.enrich.food import FoodClassifier

LABELED = Path(__file__).parent / "data" / "labeled.jsonl"
LABELS = ("yes", "likely", "no")
MIN_YES_PRECISION = 0.90
MIN_FREE_ACCURACY = 0.95  # 2 of ~227 decided rows are "no", so accuracy alone is weak; fee rules are unit-tested


@pytest.fixture(scope="module")
def classifier() -> FoodClassifier:
    return FoodClassifier.from_yaml(REPO_ROOT / "data" / "food_lexicon.yaml")


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return [json.loads(line) for line in LABELED.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def test_labeled_set_is_complete(rows):
    assert [r["pool_index"] for r in rows] == list(range(250))
    assert {r["gold_food"] for r in rows} <= set(LABELS)


def test_food_precision_on_labeled_set(classifier, rows, terminal_report):
    matrix = {(g, p): 0 for g in LABELS for p in LABELS}
    for r in rows:
        result = classifier.classify(title=r["title"], description_text=r["description_text"],
                                     food_details=r["food_details"], is_food_included=r["is_food_included"])
        assert 0.0 <= result.score <= 1.0
        matrix[(r["gold_food"], result.label.value)] += 1

    pred_yes = sum(matrix[(g, "yes")] for g in LABELS)
    gold_yes = sum(matrix[("yes", p)] for p in LABELS)
    tp_yes = matrix[("yes", "yes")]
    pos = ("yes", "likely")
    pred_pos = sum(matrix[(g, p)] for g in LABELS for p in pos)
    gold_pos = sum(matrix[(g, p)] for g in pos for p in LABELS)
    tp_pos = sum(matrix[(g, p)] for g in pos for p in pos)
    precision_yes = _ratio(tp_yes, pred_yes)

    lines = ["gold \\ pred      yes  likely      no"]
    lines += [f"{g:<12}{matrix[(g, 'yes')]:>7}{matrix[(g, 'likely')]:>8}{matrix[(g, 'no')]:>8}" for g in LABELS]
    lines.append(f"yes:        precision {precision_yes:.3f} ({tp_yes}/{pred_yes})  "
                 f"recall {_ratio(tp_yes, gold_yes):.3f} ({tp_yes}/{gold_yes})")
    lines.append(f"yes+likely: precision {_ratio(tp_pos, pred_pos):.3f} ({tp_pos}/{pred_pos})  "
                 f"recall {_ratio(tp_pos, gold_pos):.3f} ({tp_pos}/{gold_pos})")
    terminal_report("food classifier: labeled set", lines)
    print("\n".join(lines))

    assert precision_yes >= MIN_YES_PRECISION


def test_free_to_attend_accuracy_on_labeled_set(classifier, rows, terminal_report):
    decided = [r for r in rows if r["gold_free"] != "unknown"]
    correct = 0
    for r in decided:
        result = classifier.free_to_attend(cost_raw=r["cost_raw"], title=r["title"],
                                           description_text=r["description_text"])
        correct += result.is_free_to_attend is (r["gold_free"] == "yes")
    paid = [r for r in decided if r["gold_free"] == "no"]
    caught = sum(
        classifier.free_to_attend(cost_raw=r["cost_raw"], title=r["title"],
                                  description_text=r["description_text"]).is_free_to_attend is False
        for r in paid
    )
    accuracy = _ratio(correct, len(decided))
    lines = [f"free_to_attend accuracy (gold != unknown): {accuracy:.3f} ({correct}/{len(decided)}); "
             f"paid caught {caught}/{len(paid)}"]
    terminal_report("food classifier: free to attend", lines)
    print("\n".join(lines))
    assert accuracy >= MIN_FREE_ACCURACY
    assert caught == len(paid)
