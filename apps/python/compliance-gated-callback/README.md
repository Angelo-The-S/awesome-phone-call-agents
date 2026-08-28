# compliance-gated-callback

Fail-closed, per-jurisdiction compliance gate for CALL-E outbound
callback agents.

## The problem

Speed-to-lead is the single biggest predictor of whether an online lead
converts into a customer: whoever calls back first usually wins the
deal. But a business running online ads gets leads from every country,
at every hour of the day, with no way to know which calling-hour law or
consent rule applies to the number that just came in. Calling a French
prospect back at 3am is illegal. The EU AI Act has required disclosing
that the caller is an AI system since August 2026. No off-the-shelf
calling tool checks any of this automatically before dialing.

## What this app does

- Takes in a prospect: a phone number plus a compliance context
- Resolves the applicable jurisdiction chain from the phone number
- Runs that jurisdiction's rules before the call, fail-closed
- Calls CALL-E's `POST /v1/calls` with the locale and region derived
  from the resolved jurisdiction, never hardcoded
- Returns a structured result: `intent` and `next_action`
- Extensible by design: one file per jurisdiction, no shared logic to
  untangle to add a new one

## Jurisdictions supported at launch

| Jurisdiction | Key rules |
|---|---|
| US federal | 8am-9pm local recipient time, documented prior express written consent, National DNC Registry scrub, FCC-required artificial-voice disclosure, revocation honored by any means |
| EU common (27 member states) | AI Act Art. 50 disclosure of the AI interaction, ePrivacy Art. 13(1) opt-in consent, GDPR Art. 6 lawful basis documented |
| France (stacks on EU common) | Opt-in consent required since 2026-08-11, calls only Mon-Fri 10h-13h and 14h-20h, Bloctel/opposition-list scrub |

State-level US variation is not implemented yet; every US number is
currently evaluated against the federal baseline only. Also note: `+1`
is the shared NANP calling code for the United States, Canada, and over
twenty Caribbean territories, not the United States alone. This app has
no area-code lookup table yet, so every `+1` number is routed to the US
federal jurisdiction today; a Canadian or Caribbean number would
currently be evaluated against the wrong rules. `+33` (France) has no
such ambiguity.

## Legal disclaimer and known gray areas

This app is not legal advice, and passing its compliance gate is not a
guarantee of legal compliance. It encodes a good-faith reading of a
legal research pass done for this project; it has not been reviewed by
a lawyer. Consult one before using this in production. The following
gray areas came out of that research and are not settled law:

1. Whether a live, two-way conversational AI agent counts as an
   "automatic calling machine" under ePrivacy Art. 13(1), which would
   force strict EU-wide opt-in, or falls under the softer per-country
   Art. 13(3) discretion for live calls. This code defaults to the
   stricter 13(1) reading.
2. A US Fifth Circuit ruling (Bradford v. Sovereign Pest Control, Feb.
   2026) held that simply providing a phone number can itself be
   "express consent," conflicting with the FCC's usual written-consent
   standard. This code does not rely on that reading.
3. Cross-border calls (a US number calling into the EU or vice versa)
   can trigger both regimes at once; how enforcement actually
   coordinates between them is untested.
4. The AI Act's Art. 50 disclosure duty says "no later than the first
   interaction," but does not specify exactly when that means for a
   live phone call; this code discloses at the start of the call.
5. Whether real-time transcription of a call counts as "recording" in
   US two-party consent states is not settled.
6. New US state "mini-TCPA" laws keep appearing (Florida, Maryland, New
   Jersey, Oklahoma and others); the jurisdiction table above is a
   snapshot, not a permanently accurate one.

Two of these are implemented in code today as explicit, short-lived
exceptions rather than silently assumed: a US call outside the calling
window is allowed if consent was obtained within the previous 15
minutes (`compliance/jurisdictions/us_federal.py`), and French public
holidays are not yet excluded from the calling window, only weekends
(`compliance/jurisdictions/fr.py`). Both are marked
`confidence=MEDIUM` on the specific `CheckResult` they produce - that
marker means "this is a product decision pending legal confirmation,"
not "this is sourced law."

## Setup

```bash
uv sync
```

`zoneinfo` (used for every calling-window check) has no timezone
database bundled on Windows; `uv sync` installs the `tzdata` package
automatically there via a platform marker in `pyproject.toml`. Nothing
extra to do on Linux or macOS, which already ship system tzdata.

## Usage

Windows/PowerShell users: replace `export VAR=value` below with
`$env:VAR = "value"`.

Dry-run for a US number, fully compliant. No `CALLE_API_KEY` needed or
read - dry-run never touches it:

```bash
uv run python client.py \
  --task "Call the recipient and find out why they are calling in." \
  --phone +12025550123 \
  --consent-obtained --consent-timestamp 2026-08-20T12:00:00Z \
  --dnc-checked \
  --recipient-timezone America/New_York
```

Dry-run for a France number, fully compliant. Note the extra
`--gdpr-basis-documented` flag (an EU-wide requirement the US flow does
not have), and that the resolved `locale`/`region` in the printed body
come out as `fr-FR`/`FR` instead of `en-US`/`US`:

```bash
uv run python client.py \
  --task "Call the recipient and find out why they are calling in." \
  --phone +33639980456 \
  --consent-obtained --dnc-checked --gdpr-basis-documented \
  --recipient-timezone Europe/Paris
```

Dry-run, blocked because it is outside the calling window
(`--now-utc` pinned to 22:00 Paris time on a Tuesday, outside both the
10h-13h and 14h-20h windows):

```bash
uv run python client.py \
  --task "Call the recipient and find out why they are calling in." \
  --phone +33639980456 \
  --consent-obtained --dnc-checked --gdpr-basis-documented \
  --recipient-timezone Europe/Paris \
  --now-utc 2026-08-25T20:00:00Z
```

Execute against the local fake server (no real call, no cost, and still
no `CALLE_API_KEY` needed - a hardcoded non-secret key is used whenever
`--base-url` is not the real API):

```bash
uv run python fake_server.py &
uv run python client.py --base-url http://127.0.0.1:PORT \
  --task "Call the recipient and find out why they are calling in." \
  --phone +33639980456 \
  --consent-obtained --dnc-checked --gdpr-basis-documented \
  --recipient-timezone Europe/Paris \
  --execute
```

Execute for real, only after explicit go-ahead. This is the *only* case
that reads `CALLE_API_KEY`:

```bash
export CALLE_API_KEY=iams_live_your_real_key
uv run python client.py \
  --task "Call the recipient and find out why they are calling in." \
  --phone +33639980456 \
  --consent-obtained --dnc-checked --gdpr-basis-documented \
  --recipient-timezone Europe/Paris \
  --execute --allow-live
```

## Adding a jurisdiction

1. Create `compliance/jurisdictions/<id>.py` with a `RULES` object
   (including a `region_code`) and a `check(context)` function that
   returns one `CheckResult` per rule.
2. Register the module in `compliance/dispatcher.py`'s `_MODULES` dict,
   and add its country-code prefix - or append it to an existing chain,
   for a member-state variation - in `_COUNTRY_CODE_CHAINS`.
3. Add tests in `test_compliance.py`: one fully-compliant context that
   is allowed, and one test per rule that blocks on its own.

This is additive, not a refactor: `compliance/dispatcher.py`'s
resolution logic and `client.py`'s CLI do not change. In this repo
today, the three jurisdiction files run from 94 lines (`eu_common.py`,
the simplest, with no calling-window logic) to 154 lines
(`us_federal.py`, the most involved one, with the recent-consent gray
area included).

## Safety

- A real call requires explicit intent at two independent points:
  `--execute` to attempt it at all, and `--allow-live` in addition
  before it can reach `https://api.heycall-e.com` - enforced in code
  (`CallEClient.__post_init__`), not just documented.
- Dry-run is the default: without `--execute`, the exact request body
  and the compliance decision are printed and nothing is sent.
- Nothing about the recipient is guessed: `recipient_timezone` must be
  supplied, and a missing or invalid IANA name fails the relevant check
  instead of falling back to a default.
- Every recipient phone number is validated against the E.164 pattern
  before any network call is made (`build_recipient`).
- `CALLE_API_KEY` is read from the environment only when `--execute`,
  `--allow-live`, and the real base URL are all true at once
  (`resolve_api_key`); dry-run and any non-real `--base-url`, including
  the local fake server, never read it and use a hardcoded non-secret
  placeholder key instead, so the fake server can never receive a real
  credential. When the real key is used, it is never printed in full
  (`mask_secret`).
- Every phone number is masked to its last 4 digits (`mask_phone`) in
  every preview, error message, and result this app prints; the
  unmasked number is still what is actually sent to the API.
- The full request body is printed before it is sent, on every run,
  dry-run or execute - there is no call this app can place silently.
- The `Idempotency-Key` sent with every real call is always derived from
  the call's own intent (phone, task, and invocation time -
  `derive_idempotency_key`), never random and never a fixed string.
- A `POST /v1/calls` that fails with no confirmed HTTP response (a
  timeout or connection error) is never retried automatically - a blind
  retry could place a second real call. `GET` polling, which is
  non-mutating, keeps retrying safely.
- Any unmapped jurisdiction, any missing rule, or any single failing
  check blocks the call; there is no default-allow path anywhere in
  `compliance/dispatcher.py`.
- A revoked recipient cannot be called through a flag: there is no
  `--do-not-call-requested` CLI argument, and revocation is checked as
  its own blocking rule inside every jurisdiction that has one.
- There is no cancellation instruction to give, and this app does not
  pretend otherwise: `calle.openapi.yaml` has no cancel/DELETE endpoint
  for an in-flight call once `POST /v1/calls` has accepted it (known
  API limitation, tracked internally as C31). `client.py` prints this
  limitation at the moment a real call is created, not just here.
- Every phone number in this README and the test suite is from an
  officially regulator-reserved block, not just "unlikely to be real":
  US examples use the NANP `NPA-555-01XX` block; France examples use
  ARCEP's mobile fiction block `06 39 98` (Numbering Plan Art. 2.5.12);
  the one non-US/non-FR test number uses Ofcom's reserved drama mobile
  block `07700 900xxx`. `fake_server.py`'s internal sentinel numbers
  (`+10000000001`, `+10000000002`) use area code `000`, which cannot be
  a real NANP number at all. No number in this app was ever a plausible
  real subscriber number.
- The full test suite (`uv run pytest`) runs entirely against
  `fake_server.py`; no test reaches `api.heycall-e.com` or requires a
  live credential.
- What is not yet settled is written down, not silently assumed: gray
  areas are marked `confidence=MEDIUM` in the code's own output and
  listed by name above, rather than treated as confirmed rules.
- `client.py` and `fake_server.py` depend on nothing but the Python
  standard library plus `tzdata`; there is no unpublished or private
  package dependency to audit.
- Locale and region sent to CALL-E always come from the jurisdiction
  that was actually checked (`resolve_locale_and_region`), so what is
  sent can never drift from what was verified.

## Architecture

```
CLI args
  |
  v
PreCallContext (phone, consent, dnc, gdpr basis, timezone, now)
  |
  v
compliance.dispatcher.run_precall_checks()
  |
  +--> resolve_jurisdiction_chain(phone)
  |      -> jurisdiction chain, e.g. (eu_common, fr)
  |
  +--> each jurisdiction's check(context)
  |      -> list of CheckResult (passed/failed, confidence, reason)
  |
  v
PreCallDecision (allowed or blocked, with reasons)
  |
  +--> blocked: print reasons, exit. No network call is made.
  |
  +--> allowed:
         |
         v
       resolve_locale_and_region(jurisdiction_chain) -> locale, region
         |
         v
       POST /v1/calls (task, recipient with resolved locale/region,
                        result_schema)
         |
         v
       poll GET /v1/calls/{id} until a terminal status
         |
         v
       structured_result (intent, next_action, confidence_note)
```
