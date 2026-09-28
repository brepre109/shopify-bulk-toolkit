"""Product rules shared by the tag and category planners.

A rule matches when *all* of its conditions match::

    {"match": {"title": "\\bvelvet\\b", "productType": "Ribbon", "vendor": "Acme",
               "tag": "sale", "handle": "^gift-"},
     "add": ["velvet"]}                      # tags planner
     "category": "gid://shopify/TaxonomyCategory/aa-1"   # categories planner

``title``, ``handle`` and ``sku`` are case-insensitive regular expressions
(use ``\\b`` for word boundaries: "rave" should not match "unraveling");
``productType``, ``vendor`` and ``tag`` are case-insensitive exact matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REGEX_FIELDS = {"title", "handle", "sku"}
EXACT_FIELDS = {"productType", "vendor", "tag", "status"}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    conditions: dict
    payload: dict
    index: int

    @classmethod
    def parse(cls, raw: dict, index: int) -> "Rule":
        match = raw.get("match")
        if not isinstance(match, dict) or not match:
            raise RuleError(f"rule #{index + 1}: 'match' must be a non-empty object")
        conditions = {}
        for key, value in match.items():
            if key in REGEX_FIELDS:
                try:
                    conditions[key] = re.compile(value, re.IGNORECASE)
                except re.error as exc:
                    raise RuleError(f"rule #{index + 1}: bad regex for {key}: {exc}") from exc
            elif key in EXACT_FIELDS:
                conditions[key] = str(value).casefold()
            else:
                raise RuleError(f"rule #{index + 1}: unknown match field '{key}'")
        payload = {k: v for k, v in raw.items() if k != "match"}
        return cls(conditions, payload, index)

    def matches(self, product: dict) -> bool:
        for key, cond in self.conditions.items():
            if key == "title" and not cond.search(product.get("title", "")):
                return False
            if key == "handle" and not cond.search(product.get("handle", "")):
                return False
            if key == "sku" and not any(cond.search(v.get("sku", "")) for v in product.get("variants", [])):
                return False
            if key == "productType" and product.get("productType", "").casefold() != cond:
                return False
            if key == "vendor" and product.get("vendor", "").casefold() != cond:
                return False
            if key == "status" and product.get("status", "").casefold() != cond:
                return False
            if key == "tag" and cond not in {t.casefold() for t in product.get("tags", [])}:
                return False
        return True

    def describe(self) -> str:
        parts = []
        for key, cond in self.conditions.items():
            parts.append(f"{key}~/{cond.pattern}/" if hasattr(cond, "pattern") else f"{key}={cond}")
        return f"rule #{self.index + 1} ({', '.join(parts)})"


def load_rules(raw_rules: list) -> list[Rule]:
    if not isinstance(raw_rules, list):
        raise RuleError("rules file must contain a JSON array")
    return [Rule.parse(r, i) for i, r in enumerate(raw_rules)]
