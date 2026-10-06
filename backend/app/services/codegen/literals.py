"""DB-free exact-literal contract shared by platform and external runners.

The reference runner image intentionally excludes SQLAlchemy/FastAPI. Keep
this module stdlib-only so generation can consume B23 contracts without
importing the platform conformance orchestrator.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

MAX_CONTRACTS = 8
MAX_ITEMS_PER_CONTRACT = 12
CRITERION_CLIP = 240

_CRITERION_LINE_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+|\bSHALL\b|\bMUST\b", re.IGNORECASE
)
_LITERAL_RE = re.compile(r"[\"'`]([A-Za-z][A-Za-z0-9_.-]{1,39})[\"'`]")
_BRACE_LIST_RE = re.compile(
    r"\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)+)\s*\}"
)
_LITERAL_STOPWORDS = {"the", "and", "or", "not", "a", "an", "e.g", "i.e"}

# Headings whose sections hold assumptions, exclusions and open items rather
# than requirements. Bullets under them routinely quote values the build must
# NOT implement ("No notification is sent when classified as `needs-manager`"),
# so deriving presence contracts from them fails correct builds. Matched
# case-insensitively against the heading text; a match skips every line until
# the next heading at the same or a shallower level that does not match.
# `risks` is plural-only (plus "Risk(s) and/& Mitigations"): the singular shows
# up in normative headings such as "FR-3: Risk Scoring" whose band enumerations
# are exactly what the gate exists to check.
# Two heading shapes are exempt even when their text contains a vocabulary
# word: the level-1 heading (the skeleton's only `#` line is the document title
# "# Requirements — <App Name>", and an app called "Vendor Risks Dashboard"
# must not switch the whole gate off) and functional-requirement headings
# ("### FR-4: Deferred Settlement"), which are normative by construction.
NON_NORMATIVE_SECTIONS: tuple[str, ...] = (
    r"assumptions?",
    r"out[\s-]+of[\s-]+scope",
    r"non[\s-]?goals?",
    r"not\s+in\s+scope",
    r"exclusions?",
    r"limitations?",
    r"open\s+questions?",
    r"future\s+(?:work|considerations?|enhancements?)",
    r"deferred",
    r"risks",
    r"risks?\s*(?:&|and|/)\s*(?:mitigations?|issues)",
)
_NON_NORMATIVE_HEADING_RE = re.compile(
    r"\b(?:" + "|".join(NON_NORMATIVE_SECTIONS) + r")\b", re.IGNORECASE
)
# Functional-requirement headings ("### FR-4: <name>", "### NFR-2: <name>").
_FR_HEADING_RE = re.compile(r"^\W*N?FR-\d", re.IGNORECASE)
# ATX headings only; the spec generator emits no setext (underlined) headings.
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
# Bullet / numbered-list marker with an optional task checkbox.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")
# Leading bullet decorations: emphasis runs, one short parenthesised or
# bracketed tag ("**(ASSUMPTION)**", "[Out of scope]"), or a stray colon.
_LEAD_DECOR_RE = re.compile(r"^(?:[*_`~]+|\([^()]{1,40}\)|\[[^\[\]]{1,40}\]|:)\s*")
# Negated prose: sentence-initial negation or an in-sentence negation phrase
# (SHALL NOT / MUST NOT included). Runs on the bullet text AFTER list markers
# are stripped and quoted literals / brace lists are masked, so a literal such
# as `not-found` or `{not_before, not_after}` never negates its own line.
_NEGATION_RE = re.compile(
    r"^(?:no|not|never|without)\s"
    r"|\b(?:does|do|will|shall|must|should|is|are)\s+not\b"
    r"|\bwon['\u2019]t\b"
    r"|\bnot\s+(?:required|includes?|included|in\s+scope)\b"
    r"|\bout[\s-]+of[\s-]+scope\b"
    r"|\bexcluded\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Contract:
    kind: str  # enum_literals | field_contract
    items: tuple[str, ...]
    criterion: str


def heading_level(raw_line: str) -> tuple[int, str] | None:
    """ATX heading -> (level, heading text); None for any other line."""
    match = _HEADING_RE.match(raw_line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2)


def is_non_normative_heading(level: int, text: str) -> bool:
    """True when a heading names an assumptions / exclusions style section.
    The level-1 document title and FR-n headings never do, whatever they contain."""
    if level == 1 or _FR_HEADING_RE.match(text):
        return False
    return bool(_NON_NORMATIVE_HEADING_RE.search(text))


def criterion_prose(line: str) -> str | None:
    """Bullet prose with the list marker stripped and literals masked; None when
    a leading tag such as (ASSUMPTION) or (MUST NOT) marks the line itself
    non-normative or negated."""
    text = _LIST_MARKER_RE.sub("", line, count=1)
    text = _LITERAL_RE.sub(" LIT ", text)
    text = _BRACE_LIST_RE.sub(" LIT ", text).strip()
    while match := _LEAD_DECOR_RE.match(text):
        # A stripped tag never reaches the negation test below, so judge it
        # here: "(MUST NOT) accept {ssn, dob}" must stay skipped as before.
        # Literals are already masked, so a tag like ("not-found") is ( LIT ).
        tag = match.group(0)
        if _NON_NORMATIVE_HEADING_RE.search(tag) or _NEGATION_RE.search(tag):
            return None
        text = text[match.end():]
    return text.strip()


def is_negated_criterion(line: str) -> bool:
    """True when the line's prose is negated or tagged non-normative."""
    prose = criterion_prose(line)
    return prose is None or bool(_NEGATION_RE.search(prose))


def iter_criterion_lines(markdown: str) -> Iterator[str]:
    """Yield the stripped lines contracts may be derived from: criterion lines
    (bullet or SHALL/MUST) outside non-normative sections whose prose is not
    negated. Headings only update the section state; outside a skipped
    section they face the same criterion test as any other line (unchanged)."""
    skip_level: int | None = None
    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = heading_level(raw_line)
        if heading is not None:
            level, text = heading
            if is_non_normative_heading(level, text):
                if skip_level is None or level <= skip_level:
                    skip_level = level
            elif skip_level is not None and level <= skip_level:
                skip_level = None
        if skip_level is not None:
            continue
        if not _CRITERION_LINE_RE.search(line) or is_negated_criterion(line):
            continue
        yield line


def extract_contracts(requirements_md: str) -> list[Contract]:
    """Conservative positive-presence extraction. Three kinds of line are
    skipped because presence cannot prove them: anything under a non-normative
    section (Assumptions, Out of scope, Non-goals, ...), prose that is negated
    ("No ...", "does not", "not in scope", ...), and SHALL NOT / MUST NOT."""
    contracts: list[Contract] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for line in iter_criterion_lines(requirements_md):
        criterion = line[:CRITERION_CLIP]
        literals: list[str] = []
        for match in _LITERAL_RE.finditer(line):
            token = match.group(1)
            if token.lower() not in _LITERAL_STOPWORDS and token not in literals:
                literals.append(token)
        if len(literals) >= 2:
            items = tuple(literals[:MAX_ITEMS_PER_CONTRACT])
            key = ("enum_literals", items)
            if key not in seen:
                seen.add(key)
                contracts.append(Contract("enum_literals", items, criterion))
        for match in _BRACE_LIST_RE.finditer(line):
            fields = tuple(
                dict.fromkeys(f.strip() for f in match.group(1).split(","))
            )[:MAX_ITEMS_PER_CONTRACT]
            if len(fields) >= 2:
                key = ("field_contract", fields)
                if key not in seen:
                    seen.add(key)
                    contracts.append(Contract("field_contract", fields, criterion))
        if len(contracts) >= MAX_CONTRACTS:
            break
    return contracts[:MAX_CONTRACTS]


def contracts_payload(contracts: list[Contract]) -> dict:
    return {
        "version": 1,
        "entries": [
            {"kind": c.kind, "items": list(c.items), "criterion": c.criterion}
            for c in contracts
        ],
    }


def contracts_from_payload(payload: dict | None) -> list[Contract]:
    out: list[Contract] = []
    for entry in ((payload or {}).get("entries") or [])[:MAX_CONTRACTS]:
        kind = str(entry.get("kind", ""))
        items = tuple(str(i) for i in (entry.get("items") or []))[
            :MAX_ITEMS_PER_CONTRACT
        ]
        criterion = str(entry.get("criterion", ""))[:CRITERION_CLIP]
        if (
            kind in ("enum_literals", "field_contract")
            and len(items) >= 2
            and criterion
        ):
            out.append(Contract(kind, items, criterion))
    return out


def literal_contract_block(payload: dict | None) -> str:
    contracts = contracts_from_payload(payload)
    if not contracts:
        return ""
    lines = [
        "\nHARD SPEC LITERAL CONTRACT (B23 — preserve exact spellings):",
        "Do not rename, substitute, omit, translate, or normalize any listed value.",
    ]
    for contract in contracts:
        label = (
            "exact enum/status values"
            if contract.kind == "enum_literals"
            else "exact field names"
        )
        lines.append(f"- {label}: {', '.join(contract.items)}")
        lines.append(f"  source criterion: {contract.criterion}")
    lines.append(
        "For a field criterion containing 'exactly', do not add request fields "
        "beyond the listed names."
    )
    return "\n".join(lines) + "\n"
