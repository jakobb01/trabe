"""Sourced, whole-sentence presentation; no generation or network on reads."""

import time

from trabe.news_event_store import EventStore
from trabe.presentation import display_text


def units(value):
    return len(value.encode("utf-16-le")) // 2


def card(event, *, compact=False, now=None):
    source = event["sources"][0]
    title = ", ".join(event["issuers"]) + f" • {event['type']} • {event['materiality']} materiality"
    now = time.time() if now is None else now
    when = f"{max(0, int((now - event['published']) // 3600))}h ago"
    label = event["attribution"]
    label += (
        " • title only"
        if event["quality"] == "headline_only"
        else " • article extract"
        if event["quality"] == "article"
        else " • feed extract"
    )
    if source.get("publisher"):
        label += " • " + source["publisher"]
    facts = event["facts"][: 1 if compact else 2]
    lines = [title + " • " + when, label, *[f["text"] for f in facts], event["uncertainty"]]
    lines.insert(2, "Price direction: " + event["direction"])
    if event["status"] == "uncertain/proposed":
        lines.append("Unconfirmed/proposed; no completed event inferred.")
    if event.get("ambiguous"):
        lines.append("May overlap another reported event; no separate push.")
    if event.get("correction"):
        lines.append("Correction/retraction: see the source for the changed statement.")
    return display_text("\n".join(lines))


class NewsDigest:
    def __init__(self, store):
        self.store, self.repo = store, EventStore(store)

    def material(self, symbols, now, limit=3):
        return [
            e for e in self.repo.events(symbols, now) if e["materiality"] in ("high", "medium")
        ][:limit]

    def briefing(self, symbols, now):
        events = self.material(symbols, now)
        return [card(e, compact=True, now=now) for e in events] or [
            "No current material company developments in the collected evidence."
        ]

    def details(self, symbols, now):
        parts = []
        for event in self.repo.events(symbols, now)[:12]:
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
        events = self.repo.events(symbols, now)
        selected = events[page * 3 : page * 3 + 3]
        header = "📰 Company developments • cached\nMateriality and price direction are separate."
        parts = [header]
        for event in selected:
            section = card(event, now=now)
            if units("\n\n".join([*parts, section])) < 2900:
                parts.append(section)
        if not selected:
            parts.append("No current material company developments in the collected evidence.")
        elif not any(e["materiality"] in ("high", "medium") for e in events):
            parts.append(
                "No current material developments; only routine/uncertain coverage is available."
            )
        if len(events) > (page + 1) * 3:
            parts.append("More cached developments available via More or /briefing details.")
        if page == 0:
            context = [
                e
                for e in self.repo.events(symbols, now, context=True)
                if now - e["published"] > 72 * 3600 and e["materiality"] == "high"
            ]
            if context:
                section = "Earlier context (original date; not breaking)\n" + card(
                    context[0], compact=True, now=now
                )
                if units("\n\n".join([*parts, section])) < 3000:
                    parts.append(section)
        coverage = self.repo.coverage_lines(symbols, now)
        if sources:
            parts += coverage[:12]
        parts.append(
            "Research/paper signals retain the legacy baseline. /news details for legacy analysis."
        )
        return "\n\n".join(parts)
