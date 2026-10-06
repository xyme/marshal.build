"""In-process PII/secret redaction for EXTERNAL model egress (S17-05).

Bedrock calls get the managed guardrail (S14-04); calls leaving AWS for a
custom endpoint cannot, so this pass applies the same POLICY — mask values
that must never enter a model conversation — with an honestly narrower
MECHANISM: pattern matching + checksums, not ML entity detection. The entity
set mirrors the S14-04 guardrail config (cards, bank details, government IDs,
credentials); names/emails/addresses stay untouched for the same reason they
do there: they appear legitimately in specifications.

Deliberately dependency-free and synchronous: this sits on the hot path in
front of every external call, so it must be microseconds, not a service hop.
"""

import re

__all__ = ["redact_text", "redact_messages"]


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, ch in enumerate(digits):
        d = int(ch)
        if index % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _aba_ok(digits: str) -> bool:
    """US bank routing checksum (3-7-1 weights)."""
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(w * int(d) for w, d in zip(weights, digits, strict=True)) % 10 == 0


# Order matters: context-anchored patterns run before bare-number patterns so
# a "password: 4111..." masks as PASSWORD, not as a card fragment.
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|bearer)\b\s*[:=]\s*(\S+)"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_ROUTING_RE = re.compile(r"\b\d{9}\b")
# SWIFT/BIC constrained by a real country code — bare [A-Z]{8} would mass-match
# ordinary capitalised words (PASSWORD, SECURITY, DATABASE…).
_SWIFT_RE = re.compile(
    r"\b[A-Z]{4}(?:US|GB|DE|FR|SG|HK|JP|CN|IN|AU|CA|CH|NL|ES|IT|SE|NO|DK|FI|BE|AT|IE|NZ|KR|TW|MY|TH|ID|PH|VN|AE|SA|ZA|BR|MX|LU|PT|PL|CZ)[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"
)


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Mask high-risk values; returns (masked_text, {entity_type: count})."""
    counts: dict[str, int] = {}

    def bump(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"[REDACTED:{kind}]"

    def credential(match: re.Match) -> str:
        return f"{match.group(1)}: {bump('CREDENTIAL')}"

    text = _CREDENTIAL_RE.sub(credential, text)
    text = _AWS_ACCESS_KEY_RE.sub(lambda _m: bump("AWS_ACCESS_KEY"), text)

    def card(match: re.Match) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return bump("CARD_NUMBER")
        return match.group(0)

    text = _CARD_RE.sub(card, text)
    text = _SSN_RE.sub(lambda _m: bump("US_SSN"), text)
    text = _IBAN_RE.sub(lambda _m: bump("IBAN"), text)

    def routing(match: re.Match) -> str:
        if _aba_ok(match.group(0)):
            return bump("US_BANK_ROUTING")
        return match.group(0)

    text = _ROUTING_RE.sub(routing, text)
    text = _SWIFT_RE.sub(lambda _m: bump("SWIFT_BIC"), text)
    return text, counts


def redact_messages(
    messages: list[dict], system: str
) -> tuple[list[dict], str, dict[str, int]]:
    """Apply redact_text across a Converse-shaped message list + system prompt."""
    totals: dict[str, int] = {}

    def merge(counts: dict[str, int]) -> None:
        for kind, n in counts.items():
            totals[kind] = totals.get(kind, 0) + n

    system_out, counts = redact_text(system or "")
    merge(counts)
    messages_out: list[dict] = []
    for message in messages:
        blocks_out = []
        for block in message.get("content", []):
            if "text" in block:
                masked, counts = redact_text(block["text"])
                merge(counts)
                blocks_out.append({**block, "text": masked})
            else:
                blocks_out.append(block)
        messages_out.append({**message, "content": blocks_out})
    return messages_out, system_out, totals
