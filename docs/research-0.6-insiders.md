# 0.6 insider purchase sources and implementation recommendation

Research performed 2026-11-03 UTC (2026-11-04 local). Existing free credentials
and keyless endpoints were probed without changing app data or contacting the Pi.
Private response fixtures are in ignored `.local/insider-*.json` files.

## Decision

Use **Tracefour's keyless filing feed** for structured role, filing and purchase
context, with Finnhub transactions for cross-checking derivative status and its
filings endpoint for additional publication/source metadata. Implement a direct
SEC Form 4 XML parser as an optional enrichment path, with clear unavailable
status when SEC blocks the request. No new account, paid plan or user input is
required for the usable primary path.

The provider's own [API documentation](https://tracefour.com/api-docs/insider-trading-api)
and [OpenAPI specification](https://tracefour.com/v1/openapi.json) document an
anonymous allowance of 60 requests/hour/IP, hourly refresh and a rolling 365-day
window. `since` filters the earliest filing date. `limit` is capped at 500;
there is no documented cursor/offset. Attribution with a Tracefour link is
required under its compilation's CC BY 4.0 license. Link the original SEC filing
as well. Do not present this aggregator as the regulator itself.

## Observed access, not assumed entitlement

| Request | Result | Important fields/limitation |
| --- | --- | --- |
| Finnhub `stock/insider-transactions`, GOOGL, 2026-10-01–2026-11-03 | HTTP 200, 40 rows | Includes `id`, `isDerivative`, transaction code, dates, shares/change/price; no role/title. |
| Same Finnhub endpoint, VST, 2026-08-01–2026-11-03 | HTTP 200, 9 rows, including 3 P purchases | `currency` was empty; do not infer a transaction currency from that field. |
| Finnhub `stock/filings`, GOOGL, form 4, October–November | HTTP 200, 9 filings | Includes issuer `cik`, `accessNumber`, `acceptedDate`, `filedDate`, `filingUrl` and `reportUrl`. |
| Finnhub `stock/executive`, GOOGL | HTTP 403 | The existing free key cannot supply this role enrichment. |
| SEC submissions, ticker map, company Atom and a concrete ownership XML | HTTP 403 | Direct SEC access is not usable from this execution environment. |
| Tracefour VST purchase feed since 2026-08-01 | HTTP 200, 3 rows | Real CEO/director purchases with role flags, filing identity and source links. |
| Tracefour latest global purchases, limit 2 | HTTP 200 | Filings dated 2026-11-03 22:44/22:41 UTC, response fetched at 23:00 UTC: source is currently updating. |

Finnhub's official [OpenAPI](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/api/openapi.yaml)
describes insider transactions from ownership filings with at most 100 rows per
call. The live response contains useful additional fields absent from older
schema examples. Its `source` was the literal `sec`, **not a URL**. Obtain a real
source URL from the filings endpoint or Tracefour; never manufacture one using
an accession-number prefix as if it were the issuer CIK.

## Concrete primary request and response contract

```text
GET https://tracefour.com/v1/filings
    ?ticker=VST&direction=P&since=2026-08-01&limit=100
```

Read the envelope's `data`, `error` and `meta`. A successful empty array is a
valid no-events response; HTTP failures or `error` are unavailable, not no buys.
Observed row fields include:

- Identity: `filingId` (accession plus transaction ordinal), `issuerTicker`,
  `ownerName`, `cik`, `formType`, `originalSubmissionDate`, `periodOfReport`.
- Dates: `filedAt` UTC timestamp and `transactionDate` calendar date.
- Role: `ownerTitle`, `isDirector`, `isOfficer`, `isTenPercentOwner`, `isOther`.
- Economics: `transactionCode`, `shares`, `pricePerShare`, `totalValue`,
  `sharesOwnedAfter`, `ownershipType`.
- Context: `isPairedExercise`, `hasOfferingFootnote`,
  `hasTaxWithholdingFootnote`, `hasAdsFootnote`, `hasIndirectOwnershipFootnote`,
  `hasVwapFootnote`, `is10b5_1`, `otherText`.
- Links: `rawFilingUrl` for SEC and `url` for the attributed Tracefour page.

The live VST rows had `ownerTitle = President and CEO`, `isDirector = true`,
form `4` and both source links. The feed does **not** expose an `isDerivative`
or security-title field in the observed response. Its `cik` must not be assumed
to be the issuer CIK; use the explicit issuer information in filing metadata.

Persist `meta.fetchedAt`, `meta.source`, `meta.attribution.page` and the row's
attribution link. The documented ticker filter accepts only 1–5 ASCII letters;
unsupported symbols such as dotted share classes require explicit unavailable
status or the optional SEC adapter rather than a malformed API request.

## Qualification and accurate wording

The SEC's [Form 4 instructions](https://www.sec.gov/files/form4data.pdf) and
[investor explanation](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-69)
make an important distinction: **P includes both open-market and private
purchases, and may include derivatives**. A positive share change or a P code
alone is insufficient to call something a confirmed discretionary exchange buy.

Recommended conservative defaults:

1. Watch the union of held and watched symbols. Require P, positive finite shares
   and price/value, and a meaningful threshold such as USD 10,000, configurable
   through Telegram. Keep money arithmetic decimal.
2. Require a verified role: `isDirector = true`, or an officer title matching CEO,
   CFO, chief executive officer or chief financial officer. Ten-percent owners
   alone are not automatically CEOs/directors. Preserve unknown-role candidates
   for manual inspection; do not send a qualified-role alert for them.
3. Exclude paired exercises, tax-withholding and known offering/private-placement
   flags. Carry planned/10b5-1, indirect ownership, ADS and VWAP context into the
   event rather than hiding it.
4. Establish non-derivative status from a matching Finnhub transaction explicitly
   marked `isDerivative = false`, or a parsed SEC `nonDerivativeTransaction`.
   Match accession, ticker, normalized owner, transaction date, quantity and
   price; a name alone is not enough. Unknown derivative status remains an
   unqualified candidate, with the reason visible.
5. Label unmatched venue information honestly: “reported purchase (code P;
   open-market/private)” rather than asserting an exchange venue. A parsed
   filing can mark known private-placement footnotes ineligible. This still
   produces meaningful executive/director purchase alerts without inventing
   information that neither provider supplies.
6. Suppress a new proactive buy alert for `4/A` corrections. Retain the amendment,
   original date and source, update/reconcile an earlier event when identifiable,
   and never add amended value to the original as another purchase.

Finnhub and Tracefour both returned the same three VST P purchase rows in this
probe, so this combined qualification path is executable with current access.
If either required qualification is missing, show the specific missing detail;
do not silently downgrade the alert contract to “any positive change.”

## Minimal logic and storage

- Normalized event: provider/source IDs, accession/ordinal, ticker, owner and
  reported owner CIK, role/title and provenance, code, derivative qualification,
  transaction date, reported publication time/precision, first/last seen UTC,
  decimal shares/price/USD value, flags, eligibility/reason, original and
  attribution URLs, amendment/revision state.
- Persistent event key by canonical provider filing ID, with an economic
  fingerprint to avoid repeat alerts across refiled/amended/co-owned duplicates.
  Preserve distinct ordinals in the same filing; aggregate an owner's eligible
  lots into one filing notification when useful rather than dropping them.
- Bootstrap existing records into the cache without old-alert spam. Automatic
  delivery considers newly received eligible filings after activation, with a
  bounded recent publication window. Retain old/new dates for amendments.
- Keep transaction time, source publication time and ingestion time separate.
  For future prediction inputs, usable time cannot precede first ingestion;
  never treat transaction date as the publication timestamp.
- Claim an alert ID and enqueue `kind=insider` (or the shared proactive kind)
  atomically in one SQLite transaction. Recheck pause, enabled state, threshold
  and symbol coverage inside that transaction. Delivery retries fetch no data.
- Share the existing persistent Finnhub accounting/pacing when calling its
  endpoints. Do not use its current `automatic=True` price-monitor gate blindly:
  insider automation must work independently of `/monitor on` and source choice,
  while still obeying global pause/storage controls.
- Cache Tracefour by symbol for about six hours initially. Use a persistent
  rolling-hour budget below 60 (for example 40 automatic requests/hour, reserving
  headroom), bounded response/rows and timeout. Retry 429 using `Retry-After`;
  back off outages and rotate due symbols so failures cannot starve the rest.
- Use an overlap of recent filing dates; at the 500-row cap, surface partial
  coverage and split the query by ticker rather than claiming complete history.
- Retain event/alert metadata for three calendar months, with current dedup and
  correction references preserved where needed. Avoid storing raw full XML.

Suggested controls: `/insiders [TICKER]`, `/insiders on|off`,
`/set insider_min_value_usd 10000`, with cached local results, eligibility and
source health available without new network requests. A role/amount filter change
does not resend an already claimed filing event. New automation defaults off.

## Optional direct SEC path

The [SEC API description](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
documents keyless submissions JSON, and the [developer FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions)
asks automated clients to identify themselves and states a maximum 10 requests
per second. Use a stricter app limit of at most one request/second, caching and
bounded retries. Do not try to evade a 403.

The existing Git remote identifies a public repo, and GitHub's repository API
confirmed `private=false` and issues enabled. A truthful project identity/contact
URL is therefore available: `trabe/0.7 (https://github.com/jakobb01/trabe/issues)`.
No user email is documented; do not invent one. Allow an optional privately
configured `SEC_USER_AGENT` if a deployment needs a different real contact.
The project URL identity did not overcome this environment's SEC 403, so direct
SEC access must be optional rather than a prerequisite to the primary feed.

Use Finnhub's verified `reportUrl` or issuer CIK + accession + document metadata
to fetch bounded ownership XML. Parse relationship flags, officer title,
non-derivative transactions, code/acquired flag, shares/price and footnotes.
Reject oversized XML/DOCTYPE/entity constructs, unsupported hosts and redirects
away from an allowlisted SEC origin. A Form 4 parser can be tested using small
fixtures without a full SEC crawler or new dependency.

## Focused acceptance checks

- A real keyless response has current filing timestamps, required provenance and
  actual role fields; an empty result is distinguishable from failure.
- CEO/CFO/director P purchase above threshold qualifies only with verified
  non-derivative status; gift/grant/exercise/sale/private-offering and unknown
  role/derivative candidates do not become qualified alerts.
- Identical polls, restart, amended records and filter changes create no repeat
  alert; separate lots are retained and correctly grouped.
- Pause blocks the next request and atomically prevents enqueue. Queued proactive
  messages honor pause; manual cached inspection stays available.
- Source dates, receipt dates, reported role, links and attribution remain visible.
- One small normalization/qualification/dedup suite is enough; no ARM soak or
  real Telegram poller is needed for this feature's development.
