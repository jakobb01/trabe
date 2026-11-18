"""Read-only, same-cohort comparison of frozen five-session directions."""

from collections import Counter
from dataclasses import dataclass
from itertools import combinations

from trabe.advisor import COMPARISON, CONSENSUS, DIRECTIONAL, MEAN_BIAS
from trabe.advisor_results import AdvisorResults
from trabe.price_endpoints import QUALITY

METHODS = (
    ("Saved rules", "baseline"),
    ("Non-neutral consensus", CONSENSUS),
    ("Mean story bias", MEAN_BIAS),
)


@dataclass(frozen=True, slots=True)
class ScoredForecast:
    window: tuple[str, ...]
    ticker: str
    calls: dict[str, str]
    actual: str


def disjoint(eligible):
    """Earliest finishing windows first, keeping every company in each chosen window."""
    windows = {row.window for row in eligible}
    selected = set()
    last_close = ""
    for window in sorted(windows, key=lambda dates: (dates[-1], dates)):
        if window[0] > last_close:
            selected.add(window)
            last_close = window[-1]
    return selected, [row for row in eligible if row.window in selected]


def cohort_lines(results, db, ticker):
    counts = Counter()
    gaps = Counter()
    calls = {name: Counter() for _, name in METHODS}
    changed = Counter()
    eligible, windows = [], set()
    conflicts = opposing = different = 0
    for packet, result in results.saved(db, ticker):
        comparison = packet.get("comparison")
        if comparison is None:
            counts["legacy"] += 1
            continue
        if comparison["version"] != COMPARISON:
            counts["other version"] += 1
            continue
        baseline = packet["forecast"]
        directions = comparison["directions"] | {"baseline": baseline["direction"]}
        window = tuple(packet["sessions"])
        windows.add(window)
        for name, direction in directions.items():
            calls[name][direction] += 1
            changed[name] += direction != baseline["direction"]
        conflicts += comparison["story_conflict"]
        opposing += comparison["opposing_votes"]
        if baseline["direction"] != "insufficient":
            different += len(set(directions.values())) > 1
        gaps.update(baseline["gaps"])
        status = (
            "unsupported"
            if not results.supported(packet)
            else result["status"]
            if result
            else "abstained"
            if baseline["direction"] == "insufficient"
            else "pending"
        )
        counts[status] += 1
        if status == "scored":
            eligible.append(
                ScoredForecast(window, packet["ticker"], directions, result["realized"])
            )

    issued = sum(calls["baseline"].values())
    overlapping = sum(
        left[0] <= right[-1] and right[0] <= left[-1] for left, right in combinations(windows, 2)
    )
    selected, reduced = disjoint(eligible)
    scored_windows = {row.window for row in eligible}
    lines = [
        "Automatic shadow cohort" if results.shadow else "Owner-issued cohort",
        f"Frozen {issued} • "
        + " • ".join(
            f"{name} {counts[name]}"
            for name in ("pending", "abstained", "unscorable", "unsupported", "scored")
        ),
        f"{len(windows)} distinct target windows • {overlapping} overlapping window pairs.",
        f"{len(scored_windows)} scored windows • disjoint subset {len(selected)} windows / {len(reduced)} records.",
    ]
    if scored_windows:
        exposure = Counter(row.ticker for row in eligible)
        date_range = f"{min(w[0] for w in scored_windows)} to {max(w[-1] for w in scored_windows)}"
        lines.append(
            f"Scored {date_range} • {sum(n > 1 for n in exposure.values())}/{len(exposure)} companies repeated."
        )
    if counts["legacy"] or counts["other version"]:
        lines.append(
            f"Outside this comparison: {counts['legacy']} legacy and {counts['other version']} other-version records."
        )
    if not issued:
        return lines
    if gaps:
        lines.append(
            "Frozen input gaps (a record can have several): "
            + " ".join(f"{message} [{count}]" for message, count in gaps.most_common())
        )
    lines.append(
        f"Story-group conflicts {conflicts} • opposing directional votes {opposing} • "
        f"methods disagreed on {different}/{issued - calls['baseline']['insufficient']} non-abstaining records."
    )
    for label, name in METHODS:
        directional = sum(calls[name][call] for call in DIRECTIONAL)
        lines.append(
            f"{label}: directional {directional}/{issued} frozen, "
            f"{sum(row.calls[name] in DIRECTIONAL for row in eligible)}/{len(eligible)} scored. "
            f"Matched {sum(row.calls[name] == row.actual for row in eligible)}/{len(eligible)} scored, "
            f"{sum(row.calls[name] == row.actual for row in reduced)}/{len(reduced)} disjoint."
        )
        if name != "baseline":
            disagreement = [row for row in eligible if row.calls[name] != row.calls["baseline"]]
            wins = sum(row.calls[name] == row.actual for row in disagreement)
            losses = sum(row.calls["baseline"] == row.actual for row in disagreement)
            lines.append(
                f"Different from baseline {changed[name]}/{issued - calls['baseline']['insufficient']} frozen. "
                f"On {len(disagreement)} scored disagreements: {wins} wins, {losses} losses, "
                f"{len(disagreement) - wins - losses} both missed."
            )
    return lines


def comparison_report(store, calendar, ticker=None):
    with store.connection() as db:
        owner = cohort_lines(AdvisorResults(store, calendar), db, ticker)
        shadow = cohort_lines(AdvisorResults(store, calendar, shadow=True), db, ticker)
    return "\n".join(
        [
            f"Company forecast comparison{(' • ' + ticker) if ticker else ''} • {COMPARISON}",
            "Prospective observations • first regular-minute open to fifth close • ±1%.",
            "",
            *owner,
            "",
            *shadow,
            "",
            "Each set of scores uses the same eligible records for all three methods. "
            "The disjoint view takes the earliest-ending scored windows first and keeps all companies in each selected window.",
            "Overlapping windows and companies in one window share market exposure.",
            "Automatic collection: /advisor shadow",
            QUALITY,
        ]
    )
