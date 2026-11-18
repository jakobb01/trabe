"""Sourced, whole-sentence presentation; no generation or network on reads."""

import time

from trabe import __version__
from trabe.news_event_store import EventStore
from trabe.news_events import briefing_results_fact
from trabe.presentation import display_text

BRIEFING_SELECTOR_VERSION = "briefing-excerpts-v1"
PREVIOUS_BRIEFING_SELECTOR = "events-v3-first-frozen-fact"


def units(value):
    return len(value.encode("utf-16-le")) // 2


def _is_material(event):
    return event["materiality"] in ("high", "medium")


def _compact_facts(event):
    return event.get("compact_facts", event["facts"][:1])


def card(event, *, compact=False, now=None, topic=None):
    source = event["sources"][0]
    ticker = event.get("display_ticker") or event["ticker"]
    context = event.get("display_context", "")
    topic = topic or {
        "competition": "Technology and competition",
        "recap/opinion": "Commentary",
        "unknown": "News",
    }.get(event["type"], event["type"].capitalize())
    title = [ticker, context or topic]
    if _is_material(event) and not context:
        title.append(event["materiality"] + " materiality")
    now = time.time() if now is None else now
    title.append(f"{max(0, int((now - event['published']) // 3600))}h ago")
    facts = _compact_facts(event) if compact else event["facts"]
    scope = (
        "headline only"
        if event["quality"] == "headline_only" or all(f["id"] == "h" for f in facts)
        else "article extract"
        if event["quality"] == "article"
        else "feed extract"
    )
    attribution = " • company statement" if event["attribution"] == "company statement" else ""
    name = source.get("publisher") or source.get("provider") or "Unknown publisher"
    lines = [
        " • ".join(title),
        *[f["text"] for f in facts],
        f"Source: {name} ({scope}){attribution}",
    ]
    if event.get("relevance") == "indirect" and not context:
        lines.append("Indirect company context.")
    if event.get("news_role") in {"background", "recap"} and not context:
        lines.append("Background/recap.")
    if event.get("event_status") == "denied":
        lines.append("Source reports a denial.")
    elif event.get("event_status") == "proposed":
        lines.append("Proposed or unconfirmed.")
    if event["uncertainty"].startswith("Conflicting source statements"):
        lines.append("Conflicting source statements.")
    elif _is_material(event) and not context:
        lines.append(
            "Price impact unknown."
            if event["direction"] == "unknown"
            else f"Price direction: {event['direction']} (unvalidated rule estimate)."
        )
    if event.get("correction"):
        lines.append("Source correction or retraction.")
    return display_text("\n".join(lines))


def _briefing_sample(event, text):
    comparison = event["comparison"]
    packet = comparison["source"]["payload"]

    def in_source(fact):
        if fact["id"] == "h":
            return fact["text"] == packet["headline"]
        return fact["text"] in packet.get("snippet", "") or any(
            fact["text"] in span["text"]
            for span in packet.get("sentences", [])
            if span["id"] != "h"
        )

    return {
        "ticker": event.get("display_ticker") or event["ticker"],
        "app_version": __version__,
        "event_id": event["id"],
        "revision": event["revision"],
        "known_at": event["known_at"],
        "event_published": event["published"],
        "event_rules": event["rules"],
        "source": comparison["source"],
        "previous": {
            "selector": PREVIOUS_BRIEFING_SELECTOR,
            "materiality": comparison["previous_materiality"],
            "excerpts": [
                {"id": f["id"], "text": display_text(f["text"]), "in_credited_source": in_source(f)}
                for f in comparison["previous_facts"]
            ],
        },
        "current": {
            "selector": BRIEFING_SELECTOR_VERSION,
            "excerpts": [
                {"id": f["id"], "text": display_text(f["text"])} for f in _compact_facts(event)
            ],
            "card": text,
        },
    }


class NewsDigest:
    def __init__(self, store):
        self.store, self.repo = store, EventStore(store)

    def material(self, symbols, now, limit=3):
        return [e for e in self.repo.events(symbols, now, presentation=True) if _is_material(e)][
            :limit
        ]

    def briefing(self, symbols, now, *, samples=None):
        events = self.repo.events(symbols, now, presentation=True, comparison=samples is not None)
        material = [e for e in events if _is_material(e)][:3]
        cards = [(e, card(e, compact=True, now=now)) for e in material]
        if len(cards) < 3:
            for event in events:
                if _is_material(event) or (fact := briefing_results_fact(event)) is None:
                    continue
                event = event | {"compact_facts": [fact]}
                text = (
                    card(
                        event,
                        compact=True,
                        now=now,
                        topic="Results and guidance",
                    )
                    + "\nShown in briefing because the source reports results and a guidance change."
                )
                cards.append((event, text))
                break
        if samples is not None:
            # Keep a marker for every displayed card so a run can distinguish no
            # news from a card whose retained source could not be sampled.
            samples.extend(
                _briefing_sample(e, text) if "comparison" in e else {"current": {"card": text}}
                for e, text in cards
            )
        return [text for _, text in cards] or [
            "No current material company developments in the collected evidence."
        ]

    def details(self, symbols, now):
        parts = []
        for event in self.repo.events(symbols, now, presentation=True)[:12]:
            section = card(event, now=now)
            if units("\n\n".join([*parts, section])) < 8000:
                parts.append(section)
        if not parts:
            parts.append("No eligible current company developments in collected evidence.")
        parts.append(
            "Presentation coverage\n" + "\n".join(self.repo.coverage_lines(symbols, now)[:25])
        )
        parts.append("Additional company cards: /news cached TICKER.")
        return parts

    def report(self, symbols, now, *, page=0, sources=False):
        if not symbols:
            return "News: no held or watched symbols."
        events = self.repo.events(symbols, now, presentation=True)
        selected = events[page * 3 : page * 3 + 3]
        parts = ["📰 Company developments • cached"]
        for event in selected:
            section = card(event, now=now)
            if units("\n\n".join([*parts, section])) >= 2900:
                section = card(event, compact=True, now=now)
            if units("\n\n".join([*parts, section])) < 2900:
                parts.append(section)
        if not selected:
            parts.append("No current material company developments in the collected evidence.")
        elif not any(_is_material(e) for e in events):
            parts.append("No material company developments in this cache.")
        if len(events) > (page + 1) * 3:
            parts.append("More cached developments available via More or /briefing details.")
        if page == 0:
            context = [
                e
                for e in self.repo.events(symbols, now, context=True, presentation=True)
                if now - e["published"] > 72 * 3600 and e["materiality"] == "high"
            ]
            if context:
                section = "Earlier coverage\n" + card(context[0], compact=True, now=now)
                if units("\n\n".join([*parts, section])) < 3000:
                    parts.append(section)
        if sources:
            parts += self.repo.coverage_lines(symbols, now)[:12]
        return "\n\n".join(parts)
