"""
Terminal output for the sub-tools' results: text summaries, CSV and JSON
files, and the down chart as PNG. The analyses themselves return plain data.
"""
import csv
import dataclasses
import json
from collections import Counter
from datetime import timedelta

from .migration import CATEGORIES


def write_json(result, path):
    """Any sub-tool's result as JSON; dates become ISO strings."""
    data = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else [
        dataclasses.asdict(item) for item in result]
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# --- down -------------------------------------------------------------------

def _incomplete(missing):
    return f"INCOMPLETE: {len(missing)} pages failed to download" if missing else None


def down_summary(result):
    events = Counter(e.kind for e in result.events)
    period = f"{result.start} to {result.end}" if result.start else "all history"
    lines = [f"Down detector — {', '.join(result.sites)}, {period}",
             f"  {sum(w.captures for w in result.weeks)} captures, {events['down']} down and "
             f"{events['recovery']} recovery events"]
    busiest = sorted((w for w in result.weeks if w.down or w.recovery),
                     key=lambda w: -(w.down + w.recovery))[:5]
    if busiest:
        lines.append("  Busiest weeks:")
        lines += [f"    week of {w.start}: {w.down} down, {w.recovery} recovery"
                  for w in busiest]
    if result.missing:
        lines.append(f"  {_incomplete(result.missing)}: {', '.join(result.missing)}. "
                     f"Rerun to download only those.")
    return "\n".join(lines)


def down_events_csv(result, path):
    _write_csv(path, ["time", "kind", "status", "url"],
               [{"time": e.time.isoformat(sep=" "), "kind": e.kind, "status": e.status,
                 "url": e.url} for e in result.events])


def down_chart(result, path):
    """Two panels on one weekly time axis: down/recovery events, then captures."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    weeks = [w.start for w in result.weeks]
    middles = [start + timedelta(days=3.5) for start in weeks]  # a bar spans its whole week
    downs = [-w.down for w in result.weeks]           # below the axis
    recoveries = [w.recovery for w in result.weeks]   # above the axis
    fig, (ax, ax_captures) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                          gridspec_kw={"height_ratios": [3, 2]})
    ax.bar(middles, downs, width=5, color="#c0392b", label="Down events (200→down status)")
    ax.bar(middles, recoveries, width=5, color="#27ae60",
           label="Recovery events (down status→200)")
    ax.axhline(0, color="black", linewidth=0.8)
    if not any(downs) and not any(recoveries):
        ax.text(0.5, 0.5, "No down or recovery events in this period", transform=ax.transAxes,
                ha="center", va="center", color="#6b6b69")
    title = f"Wayback Machine crawl-observed availability transitions — {', '.join(result.sites)}"
    note = _incomplete(result.missing)
    ax.set_title(f"{title}\n{note}" if note else title)
    ax.set_ylabel("Events per week")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="upper left")

    ax_captures.bar(middles, [w.captures for w in result.weeks], width=5, color="#2a78d6")
    ax_captures.set_title("Captures per week (an unreachable site is not archived, so outages "
                          "can show up as dips)", fontsize=10, loc="left")
    partial = [w for w in result.weeks if w.days < 7]
    for week in partial:  # shaded: fewer than 7 days, so lower counts are expected
        for panel in (ax, ax_captures):
            panel.axvspan(week.start, week.start + timedelta(days=7),
                          color="#e6e6e3", zorder=0, lw=0)
    if partial:
        ax_captures.text(1, 1.02, "grey: weeks only partly inside the period",
                         transform=ax_captures.transAxes, ha="right", va="bottom",
                         fontsize=9, color="#6b6b69")
    ax_captures.set_ylabel("Captures per week")
    if weeks:  # whole weeks, Monday to Sunday, so charts line up with each other
        ax.set_xlim(weeks[0], weeks[-1] + timedelta(days=7))
    locator = mdates.AutoDateLocator(minticks=8, maxticks=20)
    ax_captures.xaxis.set_major_locator(locator)
    ax_captures.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    plt.setp(ax_captures.xaxis.get_majorticklabels(), rotation=90, ha="center")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --- migration ----------------------------------------------------------------

def migration_summary(result):
    checks = result.checks
    counts = Counter(c.category for c in checks)
    lines = [f"Migration check — {', '.join(result.sites)}, migration around {result.date}: "
             f"{len(checks)} URLs checked of {result.old_urls} that worked "
             f"from {result.start} to {result.end}"]
    for category, meaning in CATEGORIES.items():
        n = counts.get(category, 0)
        if not n:
            continue
        lines.append(f"  {n:6d}  {100 * n / len(checks):5.1f}%  {category}: {meaning}")
        for check in [c for c in checks if c.category == category][:3]:
            target = f" -> {check.hops[-1][0]}" if len(check.hops) > 1 else ""
            lines.append(f"          e.g. {check.url}{target}")
    other_host = sum("other host" in c.flags for c in checks)
    if other_host:
        lines.append(f"  ({other_host} of the redirects lead to a different host)")
    if result.missing:
        lines.append(f"  {_incomplete(result.missing)}: the URL list may be missing some.")
    return "\n".join(lines)


def migration_csv(result, path):
    _write_csv(path, ["url", "category", "flags", "first_status", "final_status", "final_url",
                      "redirects", "chain", "problem", "last_ok_in_wayback"],
               [{"url": c.url, "category": c.category, "flags": " ".join(c.flags),
                 "first_status": c.hops[0][1] if c.hops else "",
                 "final_status": c.hops[-1][1] if c.hops else "",
                 "final_url": c.hops[-1][0] if c.hops else "",
                 "redirects": max(len(c.hops) - 1, 0),
                 "chain": " -> ".join(f"{status} {url}" for url, status in c.hops),
                 "problem": c.problem, "last_ok_in_wayback": c.last_ok_in_wayback}
                for c in result.checks])


# --- robots -------------------------------------------------------------------

def robots_summary(histories):
    lines = []
    for history in histories:
        lines.append(f"robots.txt history — {history.robots_url} "
                     f"({history.archived} archived versions)")
        for i, version in enumerate(history.versions):
            if i == 0:
                lines.append(f"{version.date}  first archived version: {version.rules} rules")
            else:
                lines.append(str(version.date))
            lines += [f"            ! {alert}" for alert in version.alerts]
            for sign, rules in (("+", version.added), ("-", version.removed)):
                lines += [f"            {sign} [{agent}] {directive}: {value}"
                          for agent, directive, value in rules]
        if history.failed:
            lines.append(f"  {history.failed} versions could not be downloaded")
        lines.append("")
    return "\n".join(lines).rstrip()


def robots_csv(histories, path):
    rows = []
    for history in histories:
        for version in history.versions:
            base = {"robots_txt": history.robots_url, "date": version.date,
                    "capture": version.capture}
            rows += [{**base, "change": "alert", "value": alert} for alert in version.alerts]
            for change, rules in (("added", version.added), ("removed", version.removed)):
                rows += [{**base, "change": change, "user_agent": agent,
                          "directive": directive, "value": value}
                         for agent, directive, value in rules]
    _write_csv(path, ["robots_txt", "date", "change", "user_agent", "directive", "value",
                      "capture"], rows)
