"""Resolves a phone number to its applicable jurisdictions and runs every
jurisdiction's pre-call checks. Fail-closed: any unknown jurisdiction, any
missing rule, or any single failing check blocks the call. There is no
default-allow path anywhere in this module.
"""

from __future__ import annotations

from .jurisdictions import eu_common, fr, us_federal
from .models import CheckResult, PreCallContext, PreCallDecision

# Country-code prefix -> ordered jurisdiction chain (broad to narrow).
# State-level US variation is intentionally not wired in yet; adding a
# state layer later means adding entries here, not changing this module's
# interface or the dispatch logic below.
#
# KNOWN LIMITATION: "+1" is the shared NANP calling code for the United
# States, Canada, and over twenty Caribbean territories - it does not
# uniquely identify the United States. Disambiguating them requires an
# area-code lookup table this app does not have yet, so every "+1" number
# is currently routed to us_federal. A Canadian or Caribbean NANP number
# will incorrectly be evaluated against US federal rules until this is
# addressed. "+33" (France) has no such ambiguity: EU country calling
# codes are one-to-one with a single country.
_COUNTRY_CODE_CHAINS: dict[str, tuple[str, ...]] = {
    "+1": ("us_federal",),
    "+33": ("eu_common", "fr"),
}

_MODULES = {
    "us_federal": us_federal,
    "eu_common": eu_common,
    "fr": fr,
}


class UnknownJurisdictionError(Exception):
    """Raised when a phone number's country code has no mapped ruleset."""


def resolve_jurisdiction_chain(phone_e164: str) -> tuple[str, ...]:
    for prefix, chain in _COUNTRY_CODE_CHAINS.items():
        if phone_e164.startswith(prefix):
            return chain
    raise UnknownJurisdictionError(
        f"no jurisdiction mapped for {phone_e164!r}; fail-closed, refusing to call"
    )


def resolve_locale_and_region(jurisdiction_chain: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Locale/region for the CALL-E recipient, derived from a resolved
    jurisdiction chain - never a caller-supplied override.

    Locale comes from the narrowest (last) jurisdiction in the chain.
    Region comes from the narrowest jurisdiction that actually defines a
    country-level region_code, scanning from the end of the chain
    backward (a bloc-wide entry like eu_common has none and is skipped).
    """
    if not jurisdiction_chain:
        return None, None
    locale = _MODULES[jurisdiction_chain[-1]].RULES.default_locale
    region = None
    for jurisdiction_id in reversed(jurisdiction_chain):
        candidate = _MODULES[jurisdiction_id].RULES.region_code
        if candidate is not None:
            region = candidate
            break
    return locale, region


def run_precall_checks(context: PreCallContext) -> PreCallDecision:
    try:
        chain = resolve_jurisdiction_chain(context.phone_e164)
    except UnknownJurisdictionError as exc:
        blocked_result = CheckResult(
            check_name="jurisdiction_resolved",
            passed=False,
            reason=str(exc),
        )
        return PreCallDecision(allowed=False, jurisdiction_chain=(), results=(blocked_result,))

    all_results: list[CheckResult] = []
    for jurisdiction_id in chain:
        module = _MODULES[jurisdiction_id]
        all_results.extend(module.check(context))

    # Fail-closed even if a jurisdiction's check() returns an empty list:
    # zero results means nothing was actually verified, so allowed stays
    # False rather than vacuously True.
    allowed = len(all_results) > 0 and all(result.passed for result in all_results)
    return PreCallDecision(allowed=allowed, jurisdiction_chain=chain, results=tuple(all_results))
