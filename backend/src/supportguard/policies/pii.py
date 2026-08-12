import hashlib
import re
from dataclasses import dataclass

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b")
_CN_ID = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_CN_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PAYMENT_NUMBER = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_SCOPED_OPAQUE_RESOURCE = re.compile(r"\b(?:bill|key|sub)_[0-9a-f]{32}\b", re.IGNORECASE)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redaction_count: int
    secret_fingerprints: tuple[str, ...] = ()
    applied_rule_ids: tuple[str, ...] = ()


def redact_pii(text: str) -> RedactionResult:
    redacted, emails = _EMAIL.subn("[REDACTED_EMAIL]", text)
    fingerprints: list[str] = []

    def replace_secret(match: re.Match[str]) -> str:
        fingerprint = hashlib.sha256(match.group(0).encode()).hexdigest()[:16]
        fingerprints.append(fingerprint)
        return "[REDACTED_API_KEY]"

    redacted, secrets = _SECRET.subn(replace_secret, redacted)
    opaque_resources: list[tuple[str, str]] = []

    def protect_scoped_resource(match: re.Match[str]) -> str:
        marker = f"\ue000SUPPORTGUARD_OPAQUE_{len(opaque_resources)}\ue001"
        opaque_resources.append((marker, match.group(0)))
        return marker

    # Runtime-issued resource references are opaque identifiers, not payment
    # credentials. Protect only the exact UUID-shaped domain formats while the
    # broad numeric PII rules run; arbitrary bill_/key_/sub_ text receives no
    # exemption.
    redacted = _SCOPED_OPAQUE_RESOURCE.sub(protect_scoped_resource, redacted)
    redacted, identity_numbers = _CN_ID.subn("[REDACTED_ID]", redacted)
    redacted, phones = _CN_PHONE.subn("[REDACTED_PHONE]", redacted)
    redacted, payment_numbers = _PAYMENT_NUMBER.subn("[REDACTED_PAYMENT_NUMBER]", redacted)
    for marker, resource in opaque_resources:
        redacted = redacted.replace(marker, resource)
    rule_counts = {
        "pii.email.v1": emails,
        "secret.api_key.v1": secrets,
        "pii.cn_id.v1": identity_numbers,
        "pii.cn_phone.v1": phones,
        "payment.number.v1": payment_numbers,
    }
    return RedactionResult(
        redacted,
        emails + secrets + identity_numbers + phones + payment_numbers,
        tuple(fingerprints),
        tuple(rule for rule, count in rule_counts.items() if count),
    )
