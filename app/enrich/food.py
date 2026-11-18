"""Free-food classifier: a lexicon-driven scoring model (no ML) over an event listing.

All phrases, weights, token windows and thresholds come from ``data/food_lexicon.yaml``; see the header of that
file for the scoring model. This module only knows how to match rules, combine their evidence and squash it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from app.schemas import EvidenceSpan, FoodLabel, FoodResult, FreeResult

Field = Literal["title", "description", "food_details"]
Kind = Literal["positive", "negative", "veto"]
FIELDS: tuple[Field, ...] = ("title", "food_details", "description")
_VOCAB_REF = re.compile(r"<<(\w+)>>")
_SENTENCE_END = re.compile(r"[.!?\n]")
_LABEL_RANK = {FoodLabel.NO: 0, FoodLabel.LIKELY: 1, FoodLabel.YES: 2}

Token = tuple[int, int, str]  # (start, end, lowercased text)


def _expand(pattern: str, vocab: Mapping[str, str]) -> str:
    """Replace ``<<name>>`` with the vocabulary alternation of that name."""
    return _VOCAB_REF.sub(lambda m: f"(?:{vocab[m.group(1)]})", pattern)


def _compile(pattern: str, vocab: Mapping[str, str]) -> re.Pattern[str]:
    return re.compile(_expand(pattern, vocab), re.IGNORECASE)


def _fullmatch_pattern(pattern: str, vocab: Mapping[str, str]) -> re.Pattern[str]:
    return re.compile(rf"(?:{_expand(pattern, vocab)})", re.IGNORECASE)


@dataclass(frozen=True)
class Proximity:
    """An anchor token within ``window`` tokens of a target token (e.g. "free" near "pizza")."""

    anchor: re.Pattern[str]
    target: re.Pattern[str]
    window: int
    exclude_before: frozenset[str]
    exclude_after: frozenset[str]

    def find(self, text: str, tokens: Sequence[Token]) -> tuple[int, int] | None:
        """Return the char span covering the closest anchor/target pair in one sentence, or None."""
        best: tuple[int, int, int] | None = None  # (distance, start, end)
        targets = [i for i, tok in enumerate(tokens) if self.target.fullmatch(tok[2])]
        if not targets:
            return None
        for i, tok in enumerate(tokens):
            if not self.anchor.fullmatch(tok[2]) or self._excluded(tokens, i):
                continue
            for j in targets:
                distance = abs(i - j)
                lo, hi = min(i, j), max(i, j)
                if (0 < distance <= self.window and (best is None or distance < best[0])
                        and not _SENTENCE_END.search(text, tokens[lo][1], tokens[hi][0])):
                    best = (distance, tokens[lo][0], tokens[hi][1])
        return None if best is None else (best[1], best[2])

    def _excluded(self, tokens: Sequence[Token], i: int) -> bool:
        before = tokens[i - 1][2] if i > 0 else ""
        after = tokens[i + 1][2] if i + 1 < len(tokens) else ""
        return before in self.exclude_before or after in self.exclude_after


@dataclass(frozen=True)
class Rule:
    id: str
    group: str
    kind: Kind
    weight: float
    fields: tuple[Field, ...]
    pattern: re.Pattern[str] | None = None
    proximity: Proximity | None = None
    cap: FoodLabel | None = None
    unless: frozenset[str] = frozenset()

    def find(self, text: str, tokens: Sequence[Token]) -> tuple[int, int] | None:
        if self.proximity is not None:
            return self.proximity.find(text, tokens)
        assert self.pattern is not None
        match = self.pattern.search(text)
        return None if match is None else match.span()


@dataclass(frozen=True)
class Hit:
    rule_id: str
    group: str
    kind: Literal["positive", "negative", "veto", "flag"]
    field: Literal["title", "description", "food_details", "food_flag"]
    weight: float
    text: str
    cap: FoodLabel | None
    unless: frozenset[str]

    def evidence(self) -> EvidenceSpan:
        return EvidenceSpan(text=self.text, field=self.field, kind=self.kind, weight=round(self.weight, 3),
                            rule=self.rule_id)


@dataclass(frozen=True)
class FreeLexicon:
    cost_amount: re.Pattern[str]
    cost_free_words: re.Pattern[str]
    paid: tuple[re.Pattern[str], ...]
    free: tuple[re.Pattern[str], ...]


def quote(text: str, start: int, end: int, max_chars: int) -> str:
    """Verbatim evidence: the enclosing sentence if it fits in ``max_chars``, else the match itself, truncated."""
    left = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.end() if right_match else len(text)
    sentence = text[left:right].strip()
    if len(sentence) <= max_chars:
        return sentence
    return text[start:end].strip()[:max_chars].strip()


class FoodClassifier:
    """Scores how likely an event listing gives attendees free food, with verbatim evidence."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        vocab: dict[str, str] = {k: re.sub(r"\s+", "", v) for k, v in config["vocab"].items()}
        scoring = config["scoring"]
        self.bias: float = float(scoring["bias"])
        self.yes_threshold: float = float(scoring["thresholds"]["yes"])
        self.likely_threshold: float = float(scoring["thresholds"]["likely"])
        self.ceilings: dict[FoodLabel, float] = {FoodLabel(k): float(v) for k, v in scoring["ceilings"].items()}
        self.max_chars: int = int(scoring["evidence_max_chars"])
        self.max_evidence: int = int(scoring["max_evidence"])
        self.field_weights: dict[str, float] = {k: float(v) for k, v in config["field_weights"].items()}
        self.flag_weight: float = float(config["flag"]["weight"])
        self.flag_text: str = str(config["flag"]["text"])
        self.tokenizer = re.compile(config["tokenizer"])
        self.rules: tuple[Rule, ...] = tuple(self._rule(raw, vocab) for raw in config["rules"])
        self.free_lexicon = self._free_lexicon(config["free_to_attend"])

    @classmethod
    def from_yaml(cls, path: Path) -> FoodClassifier:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def _rule(raw: Mapping[str, Any], vocab: Mapping[str, str]) -> Rule:
        prox = raw.get("proximity")
        proximity = None
        if prox is not None:
            proximity = Proximity(
                anchor=_fullmatch_pattern(prox["anchor"], vocab),
                target=_fullmatch_pattern(prox["target"], vocab),
                window=int(prox["window"]),
                exclude_before=frozenset(w.lower() for w in prox.get("exclude_before", ())),
                exclude_after=frozenset(w.lower() for w in prox.get("exclude_after", ())),
            )
        return Rule(
            id=raw["id"],
            group=raw["group"],
            kind=raw["kind"],
            weight=float(raw["weight"]),
            fields=tuple(raw.get("fields", FIELDS)),
            pattern=_compile(raw["pattern"], vocab) if "pattern" in raw else None,
            proximity=proximity,
            cap=FoodLabel(raw["cap"]) if raw.get("cap") else None,
            unless=frozenset(raw.get("unless", ())),
        )

    @staticmethod
    def _free_lexicon(raw: Mapping[str, Any]) -> FreeLexicon:
        return FreeLexicon(
            cost_amount=re.compile(raw["cost_amount"], re.IGNORECASE),
            cost_free_words=re.compile(raw["cost_free_words"], re.IGNORECASE),
            paid=tuple(re.compile(p, re.IGNORECASE) for p in raw["paid_patterns"]),
            free=tuple(re.compile(p, re.IGNORECASE) for p in raw["free_patterns"]),
        )

    # ------------------------------------------------------------------ food

    def classify(self, *, title: str, description_text: str | None, food_details: str | None = None,
                 is_food_included: bool | None = None) -> FoodResult:
        texts: dict[Field, str] = {"title": title or "", "food_details": food_details or "",
                                   "description": description_text or ""}
        hits = self._match(texts)
        if is_food_included:
            flag_text = (food_details or "").strip()[: self.max_chars].strip() or self.flag_text
            hits.append(Hit("organizer_flag", "flag", "flag", "food_flag", self.flag_weight, flag_text, None,
                            frozenset()))
        fired = {h.group for h in hits} | {h.rule_id for h in hits}
        active = [h for h in hits if not (h.unless & fired)]
        return self._decide(active)

    def _match(self, texts: Mapping[Field, str]) -> list[Hit]:
        tokens = {f: self._tokens(t) for f, t in texts.items()}
        hits: list[Hit] = []
        for rule in self.rules:
            best: Hit | None = None
            for field in rule.fields:
                text = texts[field]
                span = rule.find(text, tokens[field]) if text else None
                if span is None:
                    continue
                weight = rule.weight * self.field_weights[field]
                if best is None or abs(weight) > abs(best.weight):
                    best = Hit(rule.id, rule.group, rule.kind, field, weight,
                               quote(text, span[0], span[1], self.max_chars), rule.cap, rule.unless)
            if best is not None:
                hits.append(best)
        return hits

    def _tokens(self, text: str) -> list[Token]:
        return [(m.start(), m.end(), m.group(0).lower()) for m in self.tokenizer.finditer(text)]

    def _decide(self, hits: list[Hit]) -> FoodResult:
        strongest: dict[str, Hit] = {}
        for hit in hits:
            if hit.group not in strongest or abs(hit.weight) > abs(strongest[hit.group].weight):
                strongest[hit.group] = hit
        total = sum(h.weight for h in strongest.values())
        score = 1.0 / (1.0 + math.exp(-(self.bias + total)))
        label = self._label(score)
        for hit in strongest.values():
            ceiling = FoodLabel.NO if hit.kind == "veto" else hit.cap
            if ceiling is not None and _LABEL_RANK[ceiling] < _LABEL_RANK[label]:
                label = ceiling
                score = min(score, self.ceilings[ceiling])
        evidence = sorted(strongest.values(), key=lambda h: -abs(h.weight))[: self.max_evidence]
        return FoodResult(score=score, label=label, evidence=tuple(h.evidence() for h in evidence))

    def _label(self, score: float) -> FoodLabel:
        if score >= self.yes_threshold:
            return FoodLabel.YES
        if score >= self.likely_threshold:
            return FoodLabel.LIKELY
        return FoodLabel.NO

    # ------------------------------------------------------------------ free to attend

    def free_to_attend(self, *, cost_raw: str | None, title: str, description_text: str | None) -> FreeResult:
        """True / False / None (listing silent). A price in the text overrides a $0 registration option."""
        text = f"{title or ''}\n{description_text or ''}"
        paid = self._first_quote(self.free_lexicon.paid, text)
        if paid:
            return FreeResult(is_free_to_attend=False, evidence=(paid,))
        amounts = self._cost_amounts(cost_raw or "")
        if any(amount > 0 for amount, _ in amounts):
            option = next(segment for amount, segment in amounts if amount > 0)
            return FreeResult(is_free_to_attend=False, evidence=(option,))
        free = self._first_quote(self.free_lexicon.free, text)
        if free:
            return FreeResult(is_free_to_attend=True, evidence=(free,))
        if amounts or (cost_raw and self.free_lexicon.cost_free_words.search(cost_raw)):
            return FreeResult(is_free_to_attend=True, evidence=("Registration is free",))
        return FreeResult(is_free_to_attend=None)

    def _first_quote(self, patterns: Sequence[re.Pattern[str]], text: str) -> str | None:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return quote(text, match.start(), match.end(), self.max_chars)
        return None

    def _cost_amounts(self, cost_raw: str) -> list[tuple[float, str]]:
        """Each price in the registration options, paired with its option segment (URLs never quoted)."""
        found: list[tuple[float, str]] = []
        for segment in cost_raw.split(";"):
            if "://" in segment:
                continue
            for match in self.free_lexicon.cost_amount.finditer(segment):
                number = match.group(1) or match.group(2)
                found.append((float(number.replace(",", "")), segment.strip()[: self.max_chars]))
        return found
