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
| Oregon (stacks on US federal) | Narrower 8am-8pm local recipient time, solicitation cap of 3 calls+texts combined per rolling 24h (HB 3865), revocation honored |
| EU common (27 member states) | AI Act Art. 50 disclosure of the AI interaction, ePrivacy Art. 13(1) opt-in consent, GDPR Art. 6 lawful basis documented |
| France (stacks on EU common) | Opt-in consent required since 2026-08-11, calls only Mon-Fri 10h-13h and 14h-20h, Bloctel/opposition-list scrub |

Oregon is this app's first US state-level variation, stacked on top of
`us_federal` the same way `fr` stacks on `eu_common` - proof the
per-jurisdiction architecture extends below country level, not just
across countries. It is matched by area code (`503`, `541`, `971`,
`458` - `compliance/dispatcher.py`'s `_US_STATE_AREA_CODE_OVERLAY`)
since the shared `+1` country code cannot identify a state on its own.
The solicitation cap has no call-history database to check against, so
`--solicitations-in-last-24h` is an operator-attested count from their
own records; omitting it fails closed, same as an unsupplied
`--recipient-timezone`. Adding a second state means adding its area
codes to that overlay, not changing the resolution logic. Every other
US number still falls through to the federal baseline alone.

Also note: `+1` is the shared NANP calling code for the United States,
Canada, and over twenty Caribbean territories, not the United States
alone. This app has no full area-code-to-country lookup table, so every
`+1` number not matched by the Oregon overlay above is routed to the US
federal jurisdiction alone; a Canadian or Caribbean number would
currently be evaluated against the wrong rules. `+33` (France) has no
such ambiguity: EU country calling codes are one-to-one with a single
country.

## AI disclosure

**This was a real defect found by testing, not a cosmetic addition.**
Every jurisdiction module defines a `DISCLOSURE_SCRIPT` constant (AI Act
Art. 50 / FCC rule 24-17 wording), and the compliance gate printed
`[PASS] ..._ai_disclosure: disclosure_script discloses the AI
interaction`. But that check only ever inspected the constant against
*itself* - a tautology, since the constant is our own hardcoded text and
the check just looks for the word "artificial" inside it. Nothing in
`client.py` or `web_server.py` ever read `RULES.disclosure_script` or
passed it into the task sent to CALL-E. A call could pass the
compliance gate's AI-disclosure check while the real call disclosed
nothing at all, unless the operator happened to write a disclosure into
their own `--task` by hand.

The fix: `compliance.dispatcher.resolve_locale_and_region` now also
resolves the effective `disclosure_script` for the jurisdiction chain
(same "narrowest jurisdiction that actually defines one wins" rule
already used for `region_code` - a state-level entry like `us_oregon`
with no script of its own inherits `us_federal`'s). `build_hardened_task`
sends it as a real, separately delimited block - and puts it **first**,
before business context or the operator's own task, because disclosure
has to happen at the very start of the call, not after other content.

**Second real defect, also found by testing**: the script correctly said
"this is an AI," but never said *why* it was calling - it asked the
recipient to explain instead, which is backwards. The disclosure
scripts now follow one structure in every jurisdiction: identity and
entity, **then the reason for the call**, then the closing
rights/callback statement - for example (France):
`"Bonjour, je suis [agent], l'assistant vocal IA de [entite], et je vous
appelle [raison]. Vous pouvez demander a parler a une personne ou
raccrocher a tout moment."`

The scripts contain placeholders (`[ENTITY]`/`[ENTITE]`,
`[AGENT_NAME]`/`[NOM_AGENT]`, `[REASON_FOR_CALLING]`/`[RAISON_APPEL]`,
`[CALLBACK_NUMBER]`) that must never reach CALL-E as literal bracket
text - a voice agent would say the brackets out loud. `--entity-name`
(CLI and web form) lets an operator supply their real business name to
fill `[ENTITY]`/`[ENTITE]`; `--agent-name` does the same for the AI
agent's first name. Omitting either uses an honest, generic fallback
(`"this organization"` / `"cette organisation"`,
`"an automated calling agent"` / `"un agent d'appel automatise"`)
rather than a fabricated name. `[CALLBACK_NUMBER]` always becomes `"the
number that just called you"` - this app has no distinct
callback-number concept (CALL-E's outbound caller ID is not guaranteed
to accept inbound calls), so inventing a specific number would be
actively misleading; this phrasing identifies the number without
asserting it is reachable.

The reason for calling is the one placeholder this app deliberately
does *not* try to fill with real text. `--task` is free-form text with
no fixed shape ("Call the recipient and find out why they are calling
in.", "Answer the recipient's questions about our practice.", ...) -
there is no reliable string operation that turns arbitrary text like
that into a short spoken reason, and guessing at one would be exactly
the kind of fragile heuristic this app avoids everywhere else.
`[REASON_FOR_CALLING]`/`[RAISON_APPEL]` is instead replaced with a
bracketed instruction telling CALL-E's own model to state the reason
itself, based on the `--task` text that immediately follows in the same
message, and explicitly **not** to ask the recipient for it. This
relies on the model correctly treating bracketed text as an instruction
to fill in rather than something to say verbatim
(`DISCLOSURE_INSTRUCTION_HEADER` says so explicitly) - the same class of
reliability limit already documented under Prompt injection resistance
below, not a hard guarantee.

```bash
uv run python client.py \
  --task "Answer the recipient's questions about our practice." \
  --phone +33639980456 \
  --consent-obtained --dnc-checked --gdpr-basis-documented \
  --recipient-timezone Europe/Paris \
  --entity-name "Bright Smile Dental" \
  --agent-name "Alex"
```

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
7. California's AB 316 (Cal. Civil Code Sec. 1714.46, effective
   2026-01-01) bars "the AI made the decision" as a defense to certain
   civil claims arising from an AI system's actions - a business cannot
   point at the calling agent to avoid liability for what it did or
   said. This is settled law, not an open question, but this app has no
   California-specific jurisdiction module yet: California numbers
   currently fall through to the US federal baseline only, same as any
   other state without an overlay (see the Oregon note above for how
   that gap gets closed). Given AB 316's liability shift, this is a real
   coverage gap for California calls, not just a documentation footnote.

Two of these are implemented in code today as explicit, short-lived
exceptions rather than silently assumed: a US call outside the calling
window is allowed if consent was obtained within the previous 15
minutes (`compliance/jurisdictions/us_federal.py`), and French public
holidays are not yet excluded from the calling window, only weekends
(`compliance/jurisdictions/fr.py`). Both are marked
`confidence=MEDIUM` on the specific `CheckResult` they produce - that
marker means "this is a product decision pending legal confirmation,"
not "this is sourced law."

**TCPA jurisdiction clarification (2026-08-29 research pass, no code
change needed):** the TCPA and its state-level "mini-TCPA" equivalents
apply based on the recipient's own location, not on how the call is
routed or which infrastructure it passes through. This app already
matches that: `resolve_jurisdiction_chain` resolves purely from
`context.phone_e164`, the recipient's own E.164 number, never from any
routing or carrier-path detail. This is confirmation that the existing
design was already correct, not a fix.

## Consent record retention

Every dry-run and execute that includes `--consent-timestamp` also
prints a `consent_retention_expires_at` line: how long the operator
should keep that consent record. It is computed as
`max(consent_timestamp, now) + 5 years`
(`compute_consent_retention_expiry` in `compliance/models.py`),
calendar-accurate (including leap-day anchors), and re-anchored forward
on every call placed on the strength of that same consent.

This single 5-year rule is deliberately the more conservative reading
of two different regimes at once: the US FTC's Telemarketing Sales Rule
requires keeping consent records for 5 years from when consent was
given (16 CFR 310.5(a)(8)), a flat deadline that does not reset;
Germany's UWG Sec. 7a also requires 5 years, but resets on every use of
that consent. Resetting on every call satisfies both simultaneously
without this app having to know which regime actually governs a given
call.

This value is informational only: it is never sent to CALL-E and never
gates whether a call is allowed - there is nothing to block pre-call
about a retention deadline that lies in the future. It only appears
when `--consent-timestamp` was supplied; a run without one prints no
retention line.

## Setup

```bash
uv sync
```

`zoneinfo` (used for every calling-window check) has no timezone
database bundled on Windows; `uv sync` installs the `tzdata` package
automatically there via a platform marker in `pyproject.toml`. Nothing
extra to do on Linux or macOS, which already ship system tzdata.

Copy `.env.example` to `.env` and fill in your real `CALLE_API_KEY` to
avoid exporting it in every terminal session:

```bash
cp .env.example .env
# then edit .env and set CALLE_API_KEY=your_real_key
```

`.env` is only ever read from this app's own directory, never
committed (already covered by the repo's root `.gitignore`: `.env`,
`.env.*`, with `.env.example` explicitly excepted), and a real
`CALLE_API_KEY` already set in your shell environment always takes
priority over whatever is in `.env`.

## Usage

**If you run this with `--execute` and get blocked**, this is working
as designed, not a bug: the compliance gate checks the *real* current
day/time against the recipient jurisdiction's legal calling window (for
example, weekdays only for France, 8am-9pm local time for the US
federal baseline). If you are testing outside that window, use
`--now-utc` to simulate a valid moment instead of waiting for one:

    --now-utc 2026-08-26T14:00:00Z   # a Wednesday, 10am Paris time - inside the FR window

`--now-utc` only overrides the clock the calling-window check reads -
it has no effect on consent, DNC/opposition-list checks, GDPR basis, or
any other rule; those still need their own real flags
(`--consent-obtained`, `--dnc-checked`, etc.) to pass.

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

Because `--consent-timestamp` is set, this also prints a
`consent_retention_expires_at`-derived line telling the operator how
long to keep that consent record (see Consent record retention above).

Dry-run for an Oregon number (area code `503`), fully compliant. Note
the extra `--solicitations-in-last-24h` flag, required for any Oregon
number, and the `us_federal -> us_oregon` jurisdiction chain in the
printed decision:

```bash
uv run python client.py \
  --task "Call the recipient and find out why they are calling in." \
  --phone +15035550100 \
  --consent-obtained --consent-timestamp 2026-08-20T12:00:00Z \
  --dnc-checked \
  --recipient-timezone America/Los_Angeles \
  --solicitations-in-last-24h 0
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

## Business context injection

CALL-E can answer several different kinds of questions in one call
(pricing, hours, appointment availability, general questions about the
business) using a single agent, instead of a separate specialized agent
per topic - as long as it has the business's own facts to draw on.
`--business-context` / `--business-context-file` (CLI) and the
"Business context" field (web form) give it that: free text describing
services, prices, hours, and FAQs, injected into the `task` sent to
CALL-E. This is a simple text injection, not a retrieval/vector-search
system - the whole text goes into the task on every call.

The final task sent to CALL-E is always three distinct, delimited
blocks, in this fixed order, never merged into one paragraph:

```
[Business context, if provided] + [Operator's own --task] + [Injection-resistance safety block]
```

`build_hardened_task(operator_task, business_context)` in `client.py`
builds this. The business context block is still additive, never a
rewrite of anything else in the task - the same principle already used
for the injection-resistance block (see Prompt injection resistance
below) - but its own wording (`BUSINESS_CONTEXT_HEADER`) directly
instructs the model to answer from these facts, not just keep them as
passive background.

That wording was hardened after a real test call: the business context
contained the exact answer to a question the caller asked, but the
model said it did not have that information and offered a human
callback instead of using what was right there in front of it. The
header now explicitly tells the model to answer directly from the
facts listed, and not to fall back to "I don't have that" or a
callback offer when the answer is present in the business context.

Rules:
- Providing business context is optional and never a compliance
  concern: an empty or absent context does not block the call, and
  behavior is unchanged from before this feature existed.
- Text is capped at 4000 characters (`MAX_BUSINESS_CONTEXT_CHARS` in
  `client.py`). Going over the limit is a clear error
  (`validate_business_context` raises `ValueError`), never a silent
  truncation - CALL-E should never receive a business description that
  was quietly cut off mid-sentence.
- `--business-context` (inline text) and `--business-context-file` (a
  UTF-8 text file path) are mutually exclusive on the CLI. The web form
  only offers a text field to paste into directly - no file upload.

`business_context_example.txt` is a filled-in example for a fictional
dental practice, Bright Smile Dental, with fictional prices, hours, and
FAQs - use it as a template, or to demonstrate one agent handling
several topics (pricing, scheduling, general info) in the same call:

```bash
uv run python client.py \
  --task "Answer the recipient's questions about our practice." \
  --phone +12025550123 \
  --consent-obtained --consent-timestamp 2026-08-20T12:00:00Z \
  --dnc-checked \
  --recipient-timezone America/New_York \
  --business-context-file business_context_example.txt
```

`result_schema`'s optional `topic_handled` field
(`pricing | scheduling | general_info | service_details | out_of_scope | unknown`)
records after the fact which kind of question the call actually
covered - useful for showing the same agent handled more than one topic
type across calls.

## Voicemail handling

A real call (`call_H40fqmT3Thwz0GhSI2m7xg`) reached an answering machine
and, with no instruction telling it otherwise, repeated its full opening
pitch three times over about 35 seconds instead of leaving one message
and hanging up. `build_hardened_task` now appends a fourth fixed block,
`VOICEMAIL_HANDLING_INSTRUCTIONS`, after the injection-resistance block:
it tells the agent that if it reaches an automated greeting with no
interactive back-and-forth, it should deliver one brief message stating
who is calling and why, then end the call - not repeat itself.

**Honest limit, confirmed by CALL-E itself**: this app cannot make
CALL-E behave differently *during* a call beyond what the task text
asks. CALL-E's own PM confirmed directly on Discord (2026-08-27) that
there is no real-time answering-machine detection or behavior control -
the only official mechanism is post-call classification through a
developer-defined `result_schema` field. That is exactly what the new
optional `answered_by` field
(`human | voicemail | ivr | unknown`) is: it lets an operator see, after
the fact, whether a given call reached a person, a machine, or an IVR -
it does not and cannot change what happened live on that call.

This is not a problem unique to this app.
[Issue #89](https://github.com/CALLE-AI/awesome-phone-call-agents/issues/89)
in this repo independently documents the same failure mode: a call
reached a machine, its message was spoken twice, and no distinct
voicemail status ever surfaced anywhere in the response. Two other apps
in this repo hit the identical gap and solved it the same way, at the
task/app layer rather than relying on a platform feature that does not
exist: `ringedingeding` ([PR #146](https://github.com/CALLE-AI/awesome-phone-call-agents/pull/146))
and `researchcall-survey` ([PR #145](https://github.com/CALLE-AI/awesome-phone-call-agents/pull/145)).

## Call closing

A real call (`call_oUjPdPH-752n7uPzxDYZhg`) showed the agent end the
call immediately after a bare "oui," with no recap of what was decided
- cutting the recipient off mid-reply ("okay au..."). `build_hardened_task`
now appends a sixth fixed block, `CALL_CLOSING_INSTRUCTIONS`, last
(after the voicemail-handling block): it tells the agent to give a
clear, brief recap of what was decided and what happens next before
ending the call, and to never be the one to hang up first - it should
wait for an explicit signal from the recipient ("goodbye," "that's
all," "thank you") and keep the conversation open until then, rather
than assume a short reply means the call is over.

**Honest limit, same as voicemail handling**: this is a prompt-level
instruction, not a control this app executes or can verify. CALL-E
offers no real-time hook for managing call flow - there is no way for
this app to detect that the agent is about to hang up, or to hold the
line open itself. If the model doesn't follow the instruction, nothing
here catches it; the only feedback available is reviewing the
transcript afterward, exactly how this issue was found in the first
place.

## Web UI

`web_server.py` is a single-page HTML form over the exact same
`client.py`/`compliance/` logic the CLI uses - same compliance gate,
same masking, same result shape. Reuses no new business logic; it is
purely an HTTP layer.

```bash
uv run python web_server.py
```

Then open `http://127.0.0.1:8000/`. `--allow-live` and the API key are
both **server-startup** concerns, never a browser control: there is no
`--allow-live` checkbox and no API-key field in the form.
`CALLE_API_KEY` is read from the server process's own environment,
exactly like the CLI, and only when the server was started with
`--allow-live` against the real API base URL.

No authentication, no accounts, no database: this is a local,
single-operator tool. It binds to `127.0.0.1` by default - do not expose
it beyond localhost without adding auth first. `--execute` mode blocks
the HTTP response for the whole poll duration (up to 120s) since there
is no background job or websocket layer - an accepted trade-off for
"facade, not a platform."

## Public demo deployment

`public_demo_server.py` is a separate, deliberately non-configurable
entry point for hosting a public read-only-ish demo (for example on
Render): it starts its own internal fake CALL-E backend
(`fake_server.FakeCalleServer`, bound to `127.0.0.1` only - never
reachable from outside the process) and points the same `web_server.py`
UI at it.

**Safety, stated plainly**: the public demo link is dry-run and
fake-server-execute only. `public_demo_server.py` hardcodes
`allow_live=False` in code - there is no flag, environment variable, or
hosting-dashboard setting that turns it on - and it always targets the
internal fake backend, never `https://api.heycall-e.com`. Do not set
`CALLE_API_KEY` in this service's environment; it would sit unused given
the above, but there is no reason for a real credential to exist in a
public demo's configuration at all.

To deploy on Render: create a new Web Service from your fork, set
**Root Directory** to `apps/python/compliance-gated-callback`, and
Render picks up `render.yaml` automatically (free plan, Python runtime).

Two free-tier trade-offs worth knowing: there is no rate limiting on the
form (acceptable here since nothing reachable has a real-world cost or a
real credential behind it), and Render's free tier spins down on
inactivity, so the first request after idle can be slow.

## Adding a jurisdiction

1. Create `compliance/jurisdictions/<id>.py` with a `RULES` object
   (including a `region_code`) and a `check(context)` function that
   returns one `CheckResult` per rule.
2. Register the module in `compliance/dispatcher.py`'s `_MODULES` dict,
   and add its country-code prefix - or append it to an existing chain,
   for a member-state variation - in `_COUNTRY_CODE_CHAINS`. For a US
   state-level variation instead, add its area codes to
   `_US_STATE_AREA_CODE_OVERLAY` (see `us_oregon.py` for the pattern).
3. Add tests in `test_compliance.py`: one fully-compliant context that
   is allowed, and one test per rule that blocks on its own.

This is additive, not a refactor: `compliance/dispatcher.py`'s
resolution logic and `client.py`'s CLI do not change. In this repo
today, the four jurisdiction files run from 94 lines (`eu_common.py`,
the simplest, with no calling-window logic) to 154 lines
(`us_federal.py`, the most involved one, with the recent-consent gray
area included); `us_oregon.py` (124 lines) is the first one stacked on
another US jurisdiction rather than a country code.

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
- Optional business context (`--business-context`/`--business-context-file`
  or the web form field) is size-capped at 4000 characters, fails loudly
  instead of silently truncating when over that limit, and is always
  sent as a separate, clearly labeled block from the operator's own task
  text - never merged into one string. See Business context injection.
- The full request body is printed before it is sent, on every run,
  dry-run or execute - there is no call this app can place silently.
- The `Idempotency-Key` sent with every real call is always derived from
  the call's own intent (phone, task, and invocation time -
  `derive_idempotency_key`), never random and never a fixed string.
- A `POST /v1/calls` that fails with no confirmed HTTP response (a
  timeout or connection error) is never blindly retried, but it does get
  exactly one safe, automatic retry using the same `Idempotency-Key`,
  because CALL-E guarantees that replaying the same key and body returns
  the original call instead of creating a duplicate (`calle.openapi.yaml`'s
  `IdempotencyKey` parameter). If that single retry also fails
  ambiguously, this app stops and says so - it never retries further or
  guesses. `/v1/calls` has no `GET`/list method, so there is no way to
  search for a call by `Idempotency-Key` after the fact; the error
  message points to the CALL-E dashboard instead of a nonexistent
  endpoint. `GET` polling, which is non-mutating, keeps retrying safely
  on its own schedule.
- Polling `GET /v1/calls/{id}` after a real call is placed continues
  indefinitely by default, not for a fixed timeout: this app cannot
  technically tell a call that is taking a long time because the
  conversation is genuinely long apart from one that is stuck - both
  look identical from here (status stays `queued`/`in_progress`, no
  error). Rather than guess and risk cutting a real conversation short,
  it prints a repeating reminder every 5 minutes
  (`--poll-warn-after-seconds`) instead of stopping, so the choice to
  keep waiting or go check the CALL-E dashboard is always the
  operator's, not this script's. Ctrl+C stops watching at any time (the
  call itself is not canceled - see the cancel-endpoint limitation
  above). `--poll-timeout-seconds` is still available for
  scripted/automated callers that want a guaranteed hard cutoff
  instead. This does not apply to the web UI, whose `--execute` mode
  keeps its fixed 120s cap (see Web UI above) since a browser request
  has no Ctrl+C equivalent.
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
- Locale, region, and the AI-disclosure script sent to CALL-E always
  come from the jurisdiction that was actually checked
  (`resolve_locale_and_region`), so what is sent can never drift from
  what was verified.
- The jurisdiction's AI-disclosure script (`RULES.disclosure_script`)
  is a real, separately delimited block in the task now, sent first -
  see AI disclosure above for why this was a real defect, not a
  cosmetic addition.

## Prompt injection resistance

The person being called can try to manipulate the call: get the agent to
ignore its goal, reveal internal instructions or credentials, or act
outside its role. This app's only lever over what happens on the call is
the `task` string sent to CALL-E - it does not control CALL-E's
underlying voice model or runtime.

**What this adds:**

- Every `task` sent to CALL-E is the operator's own wording with a fixed
  safety block appended after it (`build_hardened_task`, never a rewrite
  of the operator's text - see `--task` in Usage). The block tells the
  model to treat anything the counterpart says as information to weigh
  against the goal, never as a new instruction, and names concrete
  extraction/override attempts to refuse: revealing instructions, system
  prompt, credentials, or the compliance logic that allowed the call;
  claims of being a developer, administrator, or "CALL-E support";
  "ignore your instructions" / "enter developer mode" / manufactured
  urgency. It also tells the model to end the call if the person keeps
  pushing after being told no once.
- `result_schema` requires every call to self-report
  `manipulation_attempt_detected` (plus an optional
  `manipulation_attempt_note` with what was attempted), so an operator
  can review attempted manipulation after the fact even when the model's
  real-time refusal isn't perfect.

**What this does not guarantee:**

This app cannot filter CALL-E's voice model output before the
counterpart hears it, cannot insert a canary token and cut the call
automatically, and cannot verify the model actually followed these
instructions rather than just reporting that it did.
[OWASP's GenAI LLM01:2025 guidance](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
is explicit that no purely prompt-based defense is provably complete
against a determined adversary, because these models have no structural
separation between instructions and the data they process - it's a
mitigation that raises the cost of casual probing and creates an audit
trail, not a security boundary. See also
[OpenAI's guidance on designing agents to resist prompt injection](https://openai.com/index/designing-agents-to-resist-prompt-injection/),
which this instruction block follows (name concrete attack phrasings,
treat counterpart input as data, give the agent an explicit way to end
the interaction). `manipulation_attempt_detected` is exactly as reliable
as the model self-reporting it - a sufficiently successful manipulation
could suppress that flag too.

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
       resolve_locale_and_region(jurisdiction_chain)
         -> locale, region, disclosure_script
         |
         v
       build_hardened_task(task, business_context, disclosure_script)
         -> disclosure block FIRST, then business context, operator
            task, injection-resistance block, voicemail-handling block
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
