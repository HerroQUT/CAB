"""Turn a load_test.py CSV into the scaling figure for the report.

    python scripts/plot_loadtest.py loadtest-1026-104050.csv            # -> loadtest-1026-104050.png
    python scripts/plot_loadtest.py loadtest-1026-104050.csv -o fig.png

Two panels on one time axis (never two y-scales on one plot):
  top     images waiting in the queue vs. images classified so far
  bottom  worker tasks: desired (what auto scaling asked for) vs. running
Event markers: first scale-out, queue empty, pool back at zero.
"""
from __future__ import annotations

import argparse
import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, INK_2, MUTED, GRID, SURFACE = "#1f1f1e", "#52514c", "#8a897f", "#e6e5e0", "#fcfcfb"


def load(path: str) -> dict[str, list[int]]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    cols = ["elapsed_s", "queue_visible", "in_flight", "desired_tasks", "running_tasks", "classified"]
    return {c: [int(r[c]) for r in rows] for c in cols}


def first(t, values, cond):
    return next((ti for ti, v in zip(t, values) if cond(v)), None)


def label_end(ax, t, values, text, color, dy=0):
    ax.annotate(text, xy=(t[-1], values[-1]), xytext=(6, dy), textcoords="offset points",
                va="center", fontsize=10, color=INK_2)
    ax.plot([t[-1]], [values[-1]], "o", ms=5, color=color, mec=SURFACE, mew=1.5, zorder=4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()
    d = load(args.csv)
    t = [s / 60 for s in d["elapsed_s"]]                       # minutes
    out = args.out or os.path.splitext(args.csv)[0] + ".png"
    total = max(d["classified"]) if d["classified"] else 0

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.edgecolor": GRID, "axes.labelcolor": INK_2,
                         "xtick.color": MUTED, "ytick.color": MUTED})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True,
                                      gridspec_kw={"height_ratios": [3, 2], "hspace": 0.12})
    fig.patch.set_facecolor(SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(length=0)

    top.plot(t, d["queue_visible"], color=BLUE, lw=2, label="waiting in queue")
    top.plot(t, d["classified"], color=ORANGE, lw=2, label="classified")
    top.legend(loc="center right", frameon=False, labelcolor=INK_2, bbox_to_anchor=(1.0, 0.45))
    label_end(top, t, d["queue_visible"], "waiting in queue", BLUE)
    label_end(top, t, d["classified"], f"classified ({total})", ORANGE)
    top.set_ylabel("images")
    top.set_ylim(0, max(max(d["queue_visible"]), total) * 1.1 or 1)

    bottom.step(t, d["desired_tasks"], where="post", color=AQUA, lw=2, ls=(0, (4, 2)),
                label="desired (set by auto scaling)")
    bottom.step(t, d["running_tasks"], where="post", color=YELLOW, lw=2, label="running")
    bottom.legend(loc="upper left", frameon=False, labelcolor=INK_2)
    peak = max(d["desired_tasks"] + d["running_tasks"]) or 1
    bottom.set_ylim(0, peak + 1.6)
    bottom.set_yticks(range(0, peak + 1))
    bottom.set_ylabel("worker tasks")
    bottom.set_xlabel("minutes since the first upload")

    events = []
    scale_out = first(d["elapsed_s"], d["desired_tasks"], lambda v: v > 0)
    if scale_out is not None:
        events.append((scale_out, "first scale-out"))
    done_at = next((s for s, c in zip(d["elapsed_s"], d["classified"]) if total and c >= total), None)
    if done_at is not None:
        events.append((done_at, "queue drained"))
        back = next((s for s, r, des in zip(d["elapsed_s"], d["running_tasks"], d["desired_tasks"])
                     if s > done_at and r == 0 and des == 0), None)
        if back is not None:
            events.append((back, "back to 0"))
    for s, text in events:
        for ax in (top, bottom):
            ax.axvline(s / 60, color=MUTED, lw=1, ls=":", zorder=1)
        top.annotate(text, xy=(s / 60, top.get_ylim()[1]), xytext=(3, -2), textcoords="offset points",
                     va="top", fontsize=9, color=INK_2)

    fig.suptitle("Worker pool scaling on queue depth", x=0.06, ha="left", fontsize=13, color=INK)
    fig.subplots_adjust(left=0.08, right=0.78, top=0.91, bottom=0.09)
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
