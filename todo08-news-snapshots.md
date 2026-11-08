# 0.8 implementation: news quality and daily snapshots

Status: **implemented and locally verified; deployment is separate**. Read the
[design/research plan](docs/news-and-snapshots-plan.md) for rationale and boundaries.
The follow-up [news API research](docs/research-news-providers.md) adds Massive
as the first extra feed/assessment and Marketaux as an optional numeric check.
The original order was news quality → snapshots → paper portfolio → advisor;
the owner subsequently moved insider coverage and the advisor before paper
trading. This completed checklist covers news quality and snapshots. Follow the
[current roadmap](docs/next-features.md) and
[next checklist](todo-market-briefing.md) for the proposed market briefing work.

## Constraints

- [x] Preserve all current 0.7.2 features and unrelated worktree changes. Keep
  Telegram as the configuration interface and all Pi network use outbound.
- [x] Follow `AGENTS.md`/`CONTRIBUTING.md`: relevant unit cases and changed-file
  lint/format only; no routine broad suites, ARM/QEMU, builds or soaks.
- [x] No live development poller, real Telegram messages, model downloads, paid
  provider selection, key requests or Pi deployment as a planning/development side effect.
- [x] No heavyweight model dependency on the ARMv7 Pi. The rules release must
  work independently; optional remote inference stays off by default.

## A1 — source inputs and persistence, schema 5 → 6

- [x] Add `news_store.py` and the six bounded tables specified in the design:
  articles, revisions, provider observations, targets, analyses and event members.
- [x] `observe(article, now)` appends only changed content; A→B→A creates three
  observed occurrences within a provider/item stream even when A can reuse a
  cached analysis. Different vendors' snippets are separate variants, not
  corrections caused by alternating fetches; select a representative consistently.
- [x] Preserve all vendors' identities, publishers and timestamped entity
  annotations when URLs merge. Changed annotations append an occurrence even
  if text is unchanged; unchanged polling updates only last-seen metadata.
- [x] `associate(revision_id, ticker, evidence, now)` records availability for
  this ticker, independently of earlier other-ticker associations.
- [x] `save_analysis(revision_id, ticker, result, now)` validates ranges/enums/
  evidence and binds exact analyser/model/prompt/schema/input identities. Vendor
  results also bind a provider observation, raw scale/label, input basis and
  annotation availability; undisclosed vendor model versions stay unknown.
- [x] Import old cached news as legacy extracts observed at migration, with no
  invented raw snippets, old classification history or retroactive snapshots.
- [x] Switch the existing `News` facade to the new authority without maintaining
  two competing current news/sentiment implementations. Leave legacy daily
  aggregates read-only until expiry; do not repurpose them as decision snapshots.
- [x] Preserve issuer names in profile cache and add a bounded news context/alias
  resolver independent of `/monitor on`; reuse the shared transport/budgets.

## A2 — quality pipeline and compact messages

- [x] Add pure `news_quality.py` functions for target relevance, conservative
  duplicate groups, event tagging, importance, ranking and aggregation.
- [x] Implement `RulesAnalyzer` in `news_analysis.py` behind the common analyser
  contract. Preserve exact bounded input and supporting spans.
- [x] Distinguish direct/contextual/unrelated/unknown relevance; handle ambiguous
  tickers and brand aliases without treating arbitrary ordinary words as symbols.
- [x] Group copies before scoring. Do not merge opposite guidance, changed
  material numbers/dates, separate fiscal periods or corrections into unchanged news.
  Copies do not reset event freshness; explicitly related corrections supersede
  current evidence without rewriting earlier snapshots.
- [x] Aggregate unique events with versioned recency/relevance/importance weights.
  Keep mixed signals, unknown counts and coverage caps; no evidence means null
  bias, not Neutral (0.0). Position size affects display priority, not news tone.
- [x] `/news` gives compact per-company bias/reason/count/age. `/news TICKER`
  shows up to three distinct contributors. Preserve no-link default formatting,
  brief source/fallback warnings and no per-headline alert stream.
- [x] If optional HTTP inference is implemented in this stage, configure it off,
  validate bounded structured output, send only news/company inputs, cache by
  complete input/version identity and keep labelled rules fallback. Promotion
  requires the small offline benchmark; no unreviewed automatic model selection.
  **Not applicable in 0.8:** no HTTP inference adapter/model was selected or shipped;
  the analyser protocol and independently usable rules baseline are present.

## A3 — additional sources and a second sentiment view

- [x] Add small adapters in `news_sources.py` using the existing outbound worker
  and transport. Retain Finnhub and Yahoo fallback. Missing extra keys skip the
  adapter visibly without breaking the improved rules view; no new SDK is needed.
- [x] Implement Massive first: bounded ticker/time queries, optional per-ticker
  insights, one-hour cache, proposed four requests/minute and 200 attempts/day
  app limits. Keep label-only numeric strength null and explain missing insights.
- [x] Keep Marketaux optional: entity-specific numeric scores/highlights,
  three-hour cache, proposed 80 attempts/day and at most six scheduled checks per
  ticker/day. Three articles per free request do not establish complete coverage;
  multi-symbol batches must not claim that every ticker received news.
- [x] Before enabling a new adapter with an owner's key, perform one bounded
  read-only GOOGL/VST entitlement/field check. Record redacted evidence and actual
  score coverage. Do not enable Alpha Vantage news based on its generic free quota;
  it is currently documented as Premium. Other researched adapters stay backlog.
- [x] Persist separate provider budgets, pacing/cooldowns, freshness and coverage
  states; count retries, pagination and manual requests. Prioritise holdings with
  fair watchlist rotation. Budget deferral/failed/empty/truncated stay distinct;
  avoid sentiment-filtered queries and retry loops on 429s.
- [x] Apply one consistent local analyser across feeds. Keep vendor scores
  separate; never average incomparable scales or count duplicate vendor copies
  as extra events. Compare same-company matched stories with compatible evidence;
  record different-context comparisons when vendor full text is unavailable.
- [x] Show a compact optional second view with matched-story counts: agrees,
  differs, mixed, no matched stories or unavailable. Preserve signed local bias,
  no links and missing-as-unknown. Do not fabricate numeric vendor strength.
- [x] Add owner-only source enable/limit settings and proposed `/news sources`
  status. Bootstrap keys stay private; redact token-bearing URLs including
  `api_token`, and restrict pagination hosts. Keep all network traffic outbound.

## B1 — consistent readers and schema 6 → 7

- [x] Add `decision_snapshots` (immutable payloads) and `snapshot_runs` (mutable
  scheduler bookkeeping); one automatic claim per US session.
- [x] Add readers that accept one `db` connection. Reuse pure ledger replay and
  valuation arithmetic for frozen Decimal positions/FX; avoid cross-connection
  mixtures and the monitor's in-memory quote in a database snapshot.
- [x] Implement `SnapshotService.capture(kind, session_date, scheduled_for=None)`:
  short transaction, actual as-of time, persisted inputs, canonical JSON/hash,
  atomic immutable insert/run claim. No network, inference or clustering inside it.
- [x] Capture symbols including missing values, portfolio basis/value/FX, chosen
  price/feed/times, news aggregate/evidence/versions, earnings/insider facts and gaps.
- [x] Copy available vendor assessments, evidence basis, comparison state and
  matched/unmatched counts plus per-source failures/deferrals into snapshots.
  Bound to two vendor assessments per saved evidence item; retain byte limits
  and mark trimmed optional details. Late annotations cannot amend older packets.
- [x] Every included observation/association/analysis binding must already be
  available at capture. Future expected event dates are allowed; later actuals
  and later model results cannot be inserted into an old record.
- [x] Treat previous verified-session prices as labelled pre-open references,
  including coverage quality. Preserve the existing choice of valuation source;
  never call a prior close a live executable fill or silently mix FX/feed bases.
- [x] Add an update guard/trigger for finalized packet identity/payload. Currency
  changes, backdated trades, FX completion, article edits and estimate revisions
  affect new snapshots only. Copy compact evidence so ordinary cache expiry does
  not remove the explanation of a frozen record.

## B2 — scheduling, controls and bounds

- [x] Extend the existing maintenance worker's scheduling pass; no second poller.
  Default automatic capture off, time `preopen-30m`, owner-local HH:MM override.
- [x] Reuse the exchange calendar/DST/holidays. Record actual capture time apart
  from scheduled time. Allow labelled lateness up to five minutes; mark later
  missed runs rather than backdating a capture using current values.
- [x] Queue one optional bounded news preparation refresh ten minutes beforehand;
  use ready cached inputs at capture and mark gaps if preparation is incomplete.
  Persist its automatic origin, gate and deadline; do not inherit the manual
  queue's pause bypass. Off/pause cancels automatic preparation, not manual jobs.
- [x] Add owner-only `/snapshot [latest|now|YYYY-MM-DD|on|off]`, validated
  `/set snapshot_time ...`, and concise status/briefing references. Automatic
  captures stay silent; manual capture uses persisted inputs without provider calls.
- [x] Respect automatic pause and storage pressure. Keep read-only saved views
  usable; reject new captures under the existing hard storage pause.
- [x] Enforce 256 KiB packets, three-month automatic retention and 7-day/32-row
  manual retention plus creation rate limit. Trim optional evidence visibly;
  never silently discard holdings to fit. Add the planned news row/age caps,
  including 10,000 provider observations with 4 KiB annotation bundles; caps are
  shared across sources rather than multiplied by provider count.
- [x] Preserve durable ledger data and existing bounded backups/rollback. When
  paper experiments are added later, explicitly agree retention of referenced inputs.

## Focused acceptance and handoff

- [x] Curate about 60 bounded examples grouped by event family; hold out examples
  for a fair rules/model comparison. Critical cases include ambiguity, contrasts,
  mixed outcomes, syndicated copies, numeric corrections and headline-only data.
- [x] Run targeted quality/persistence tests: correct unknown handling; grouping
  preserves changed facts; A→B→A; late ticker association; model timeout/invalid
  output; model/input version identity and grounded short reasons.
  Optional model timeout/output tests remain deferred with that optional adapter;
  shipped rules/vendor validation and exact input/version bindings are covered.
- [x] Add a few source fixtures: canonical duplicates retain both annotations
  but count once; opposite company scores bind correctly; vendor text variants
  do not flap revisions; label-only/missing/conflicting scores; thin-page coverage;
  budget/429 handling, redaction and late vendor annotations excluded from snapshots.
- [x] Run targeted snapshot tests: immutable later views; atomic duplicate claims;
  money/FX preservation and backdated edits; missing fields; late/missed capture;
  holidays/DST; owner authorization; pause, retention and payload bounds.
- [x] Reuse prior passing checks for unchanged code. A real deployment later uses
  its existing build/readiness/delivery checks and one relevant bot walkthrough.
- [x] Update roadmap, user command examples, migration/rollback notes and evidence.
  Report actual news-quality benefit, snapshot storage size and remaining model
  choices honestly. Prepare the commit message; do not claim that a snapshot is
  itself a prediction or that a classifier benchmark establishes trading skill.

Done: rules-based company news is more relevant and less repetitive, and a daily
record remains unchanged when later data arrives. Optional models, paper execution
and prediction/evaluation have clear interfaces without becoming hidden prerequisites.

## Verification evidence

- News rules/provenance/adapters: `tests/test_news*.py` — 29 passing cases,
  including 60 synthetic acceptance examples grouped by event family. These are
  behavioral fixtures, not an independent accuracy or trading benchmark.
- Snapshots: `tests/test_snapshots.py` — 14 passing cases for money, consistent
  reads, immutable updates/replacements, late data, scheduling, retention and owner controls.
- Shared controls: `tests/test_signals.py` — 12 passing cases. Five focused
  retention/migration/pause/metadata checks passed. Earlier passing portfolio,
  ledger and command/credential results were reused for unchanged behavior.
- Massive GOOGL/VST access passed with the current key: 62 / 3 articles with
  ticker sentiment. Marketaux remains off, has no configured key and requires
  its own check before enablement.
- Changed-file Ruff checks/formatting and deployment shell syntax passed.
  No app image, ARM/QEMU, soak, Pi deployment or live Telegram poller was used.
- [Release notes and commit message](docs/release-0.8.md),
  [validation/sizing](docs/validation.md#08-news-and-snapshots) and
  [roadmap](docs/next-features.md) describe the delivered scope and next stages.
