from __future__ import annotations

import csv
import json
import math
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BASE_DIRECTORY = Path(__file__).resolve().parent
PERIOD_LABELS = {"4pm": "4 pm (off-peak)", "7pm": "7 pm (peak)", "session": "Recorded session"}
MEDIUM_COLORS = {"Wired": "#0072B2", "Wi-Fi": "#E69F00", "Cellular": "#009E73", "Unlabelled": "#777777"}
IP_COLORS = {"1.1.1.1": "#0072B2", "8.8.8.8": "#D55E00", "9.9.9.9": "#009E73"}
LOAD_COLORS = {"idle": "#0072B2", "download": "#D55E00"}
LOAD_LABELS = {"idle": "Idle", "download": "Download"}
ORIGIN_CITY = "Melbourne"
ORIGIN_COORDINATES = (-37.8, 145.0)

plt.rcParams.update({
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
})


@dataclass(frozen=True)
class Sample:
    run: str
    period: str
    medium: str
    rq: int
    condition: str
    ip: str
    size: int
    timestamp: datetime
    sequence: int
    status: str
    rtt: float | None
    distance_km: float | None


@dataclass(frozen=True)
class Stats:
    attempts: int
    sent: int
    replies: int
    mean: float
    median: float
    p95: float
    stddev: float
    loss: float

    @classmethod
    def from_rows(cls, rows: list[Sample]) -> Stats:
        values = np.asarray([row.rtt for row in rows if row.status == "reply"], dtype=float)
        sent = sum(row.status != "too_big" for row in rows)
        count = len(values)
        return cls(
            len(rows), sent, count,
            float(np.mean(values)) if count else math.nan,
            float(np.median(values)) if count else math.nan,
            float(np.percentile(values, 95)) if count else math.nan,
            float(np.std(values, ddof=1)) if count > 1 else math.nan,
            (sent - count) / sent * 100 if sent else math.nan,
        )


class Dataset:
    def __init__(self, directory: Path):
        paths = sorted(directory.glob("rq[1-5].csv")) or sorted(directory.glob("*/rq[1-5].csv"))
        if not paths:
            raise click.ClickException(f"No rq1.csv to rq5.csv files found in {directory}")
        self.rows: list[Sample] = []
        self.targets: list[dict] = []
        self.paths = paths
        sites = {}
        for folder in dict.fromkeys(path.parent for path in paths):
            metadata = folder / "geography-targets.json"
            if metadata.exists():
                sites.update({site["ip"]: site for site in json.loads(metadata.read_text())["targets"]})
        self.targets = list(sites.values())
        for path in paths:
            folder = path.parent.name.lower().replace("cullular", "cellular")
            medium = next((label for word, label in (("wired", "Wired"), ("wifi", "Wi-Fi"), ("cellular", "Cellular"))
                           if word in folder), "Unlabelled")
            period = next((value for value in ("4pm", "7pm") if folder.startswith(value)), "session")
            with path.open(newline="") as stream:
                for line, row in enumerate(csv.DictReader(stream), 2):
                    if row.get("df") == "True":
                        continue
                    try:
                        rtt = float(row["rtt_ms"]) if row["status"] == "reply" else None
                        if rtt is not None and (not math.isfinite(rtt) or rtt < 0):
                            raise ValueError("Invalid reply RTT")
                        distance = float(row["distance_km"]) if row.get("distance_km") else None
                        sample = Sample(
                            path.parent.name, period, medium, int(row["rq"]), row["condition"], row["ip"], int(row["size"]),
                            datetime.fromisoformat(row["timestamp"]), int(row["sequence"]), row["status"], rtt, distance,
                        )
                        if sample.rq != int(path.stem[-1]):
                            raise ValueError("RQ number differs from the filename")
                        self.rows.append(sample)
                    except (KeyError, ValueError) as error:
                        raise click.ClickException(f"{path}:{line}: {error}") from error
        if not self.rows:
            raise click.ClickException("No measurements remain after reading the CSV files")
        for ip in dict.fromkeys(row.ip for row in self.rows if row.rq == 4):
            if ip not in sites:
                self.targets.append({"ip": ip, "city": ip, "operator": "Unknown"})
        self.periods = tuple(value for value in PERIOD_LABELS if any(row.period == value for row in self.rows))
        self.media = tuple(value for value in MEDIUM_COLORS if any(row.medium == value for row in self.rows))
        self.dates = ", ".join(sorted({row.timestamp.date().isoformat() for row in self.rows}))
        self.ips = sorted({row.ip for row in self.rows if row.rq in (1, 2, 3)}, key=lambda ip: tuple(map(int, ip.split("."))))

    def select(self, rq: int, **filters) -> list[Sample]:
        return [row for row in self.rows if row.rq == rq and all(getattr(row, key) == value for key, value in filters.items())]

    def distance(self, ip: str) -> float:
        recorded = {row.distance_km for row in self.select(4, ip=ip) if row.distance_km is not None}
        if len(recorded) == 1:
            return recorded.pop()
        site = next((site for site in self.targets if site["ip"] == ip), None)
        if site is None or site.get("lat") is None or site.get("lon") is None:
            return math.nan
        lat1, lon1, lat2, lon2 = map(math.radians, (*ORIGIN_COORDINATES, site["lat"], site["lon"]))
        value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(min(1, max(0, value))))


def number(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}" if math.isfinite(value) else "n/a"


def response_times(rows: list[Sample]) -> list[float]:
    return [row.rtt for row in rows if row.status == "reply"]


class Plotter:
    def __init__(self, data: Dataset, output: Path):
        self.data = data
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.generated = []

    def save(self, figure, filename: str, title: str, note: str = "") -> None:
        figure.suptitle(title, x=0.02, y=0.99, ha="left", fontsize=16, fontweight="bold")
        caption = f"{self.data.dates}. {note}".strip()
        figure.text(0.02, 0.015, textwrap.fill(caption, int(figure.get_figwidth() * 14)), fontsize=8, va="bottom", color="#444444")
        figure.tight_layout(rect=(0, 0.09, 1, 0.89))
        path = self.output / filename
        figure.savefig(path, dpi=180)
        plt.close(figure)
        self.generated.append(path)
        print(f"Saved {path}")

    def grid(self):
        shape = (len(self.data.periods), len(self.data.media))
        figure, axes = plt.subplots(*shape, figsize=(max(5.5, 4.7 * shape[1]), 3.4 * shape[0] + 1), squeeze=False)
        for i, period in enumerate(self.data.periods):
            for j, medium in enumerate(self.data.media):
                axes[i, j].set_title(f"{medium}, {PERIOD_LABELS[period]}")
                axes[i, j].grid(axis="y", alpha=0.18)
        return figure, axes

    def legend(self, figure, axis, columns: int = 3) -> None:
        handles, labels = axis.get_legend_handles_labels()
        figure.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.945), ncol=columns, frameon=False)

    def overview(self) -> None:
        cells = []
        for period in self.data.periods:
            for medium in self.data.media:
                rows = [row for row in self.data.rows if row.period == period and row.medium == medium]
                if not rows:
                    continue
                start, end = min(row.timestamp for row in rows), max(row.timestamp for row in rows)
                summary = Stats.from_rows(rows)
                counts = [str(sum(row.rq == rq for row in rows)) for rq in range(1, 6)]
                cells.append([PERIOD_LABELS[period], medium, f"{start:%H:%M:%S}–{end:%H:%M:%S}", *counts,
                              str(summary.attempts), str(summary.replies), str(summary.sent - summary.replies)])
        figure, ax = plt.subplots(figsize=(15, max(4.5, 0.5 * len(cells) + 2)))
        ax.axis("off")
        headers = ["Period", "Medium", "Recorded local time", "RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "Attempts", "Replies", "No reply"]
        table = ax.table(cellText=cells, colLabels=headers, cellLoc="center", loc="center",
                         colWidths=[0.14, 0.08, 0.18, 0.07, 0.07, 0.07, 0.07, 0.07, 0.08, 0.08, 0.08])
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 2.1)
        for (row, column), cell in table.get_celld().items():
            cell.set_edgecolor("#DDDDDD")
            cell.set_facecolor("#EAF0F5" if row == 0 else ("#F7F8FA" if row % 2 == 0 else "white"))
        note = ("Counts are attempts per question. Local times retain the CSV timezone. Medium and period use folder names; "
                "the CSV condition labels were hardcoded. Off-peak/peak labels are the experimenter's definitions.")
        self.save(figure, "00_run_coverage.png", f"Experiment coverage: {len(self.data.rows):,} attempts", note)

    def rq1(self) -> None:
        sizes = sorted({row.size for row in self.data.select(1)})
        for metric, filename, label in (
            ("mean", "rq1_payload_rtt.png", "Average RTT (ms)"),
            ("loss", "rq1_payload_loss.png", "Packet loss (%)"),
        ):
            figure, axes = self.grid()
            maximum = 0
            for i, period in enumerate(self.data.periods):
                for j, medium in enumerate(self.data.media):
                    ax = axes[i, j]
                    for ip in self.data.ips:
                        summaries = [Stats.from_rows(self.data.select(1, period=period, medium=medium, ip=ip, size=size)) for size in sizes]
                        values = [getattr(summary, metric) for summary in summaries]
                        maximum = max(maximum, *(value for value in values if math.isfinite(value)), 0)
                        ax.plot(range(len(sizes)), values, "o-", color=IP_COLORS.get(ip), label=ip)
                    ax.set_xticks(range(len(sizes)), sizes)
                    ax.set_xlabel("Payload size (bytes)")
                    ax.set_ylabel(label)
            for ax in axes.flat:
                ax.set_ylim(0, 105 if metric == "loss" else max(1, maximum * 1.15))
            self.legend(figure, axes[0, 0])
            note = "10 attempts per target and size in these runs. Missing RTT points mean no replies; losses do not prove fragmentation."
            self.save(figure, filename, f"RQ1: Payload size and {label.lower()}", note)
    def comparison(self, rq: int, filename: str, title: str) -> None:
        figure, axes = plt.subplots(2, len(self.data.periods), figsize=(7 * len(self.data.periods), 7.5), squeeze=False)
        categories = self.data.ips if rq == 2 else list(self.data.media)
        series = list(self.data.media) if rq == 2 else ["idle", "download"]
        colors = MEDIUM_COLORS if rq == 2 else LOAD_COLORS
        maxima = [0, 0]
        width = 0.78 / len(series)
        for column, period in enumerate(self.data.periods):
            for index, series_value in enumerate(series):
                rows = [
                    self.data.select(rq, period=period, **({"ip": category, "medium": series_value} if rq == 2 else
                                                         {"medium": category, "condition": series_value}))
                    for category in categories
                ]
                summaries = list(map(Stats.from_rows, rows))
                positions = np.arange(len(categories)) + (index - (len(series) - 1) / 2) * width
                for row, metric in enumerate(("mean", "loss")):
                    values = [getattr(summary, metric) for summary in summaries]
                    bars = axes[row, column].bar(positions, values, width, color=colors[series_value],
                                                 label=LOAD_LABELS.get(series_value, series_value))
                    axes[row, column].bar_label(bars, labels=[number(value, 1) for value in values], padding=3, fontsize=8)
                    maxima[row] = max(maxima[row], *(value for value in values if math.isfinite(value)), 0)
            for row, ylabel in enumerate(("Average RTT (ms)", "Packet loss (%)")):
                axes[row, column].set_title(PERIOD_LABELS[period])
                axes[row, column].set_xticks(range(len(categories)), categories)
                axes[row, column].set_ylabel(ylabel)
                axes[row, column].grid(axis="y", alpha=0.18)
        for column in range(len(self.data.periods)):
            axes[0, column].set_ylim(0, max(1, maxima[0] * 1.25))
            axes[1, column].set_ylim(0, max(5, maxima[1] * 1.3))
        self.legend(figure, axes[0, 0], len(series))
        note = "Means use successful replies; loss uses all transmitted requests. "
        note += ("10 attempts per target and medium; all RQ2 requests received replies." if rq == 2 else
                 "50 attempts per idle/download condition. Download labels do not measure link utilization.")
        self.save(figure, filename, title, note)

    def rq2(self) -> None:
        self.comparison(2, "rq2_medium_rtt.png", "RQ2: Connection types at each time")
        figure, axes = plt.subplots(len(self.data.periods), len(self.data.ips),
                                    figsize=(4.7 * len(self.data.ips), 3.4 * len(self.data.periods) + 1), squeeze=False)
        for i, period in enumerate(self.data.periods):
            for j, ip in enumerate(self.data.ips):
                ax = axes[i, j]
                for medium in self.data.media:
                    rows = sorted(self.data.select(2, period=period, medium=medium, ip=ip), key=lambda row: row.timestamp)
                    ax.plot(range(1, len(rows) + 1), [row.rtt if row.rtt is not None else math.nan for row in rows],
                            "o-", color=MEDIUM_COLORS[medium], label=medium, markersize=4)
                ax.set_title(f"{ip}, {PERIOD_LABELS[period]}")
                ax.set_xlabel("Request number")
                ax.set_ylabel("RTT (ms)")
                ax.set_ylim(bottom=0)
                ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
                ax.grid(axis="y", alpha=0.18)
        self.legend(figure, axes[0, 0])
        self.save(figure, "rq2_rtt_timeseries.png", "RQ2: Individual ping times by connection",
                  "Each dot is one reply. Connections were tested separately. Vertical scales differ so smaller changes stay visible.")

    def rq3(self) -> None:
        figure, axes = plt.subplots(1, len(self.data.media), figsize=(4.7 * len(self.data.media), 5), squeeze=False)
        maximum = 0
        for j, medium in enumerate(self.data.media):
            for ip in self.data.ips:
                values = [Stats.from_rows(self.data.select(3, period=period, medium=medium, ip=ip)).mean for period in self.data.periods]
                maximum = max(maximum, *(value for value in values if math.isfinite(value)), 0)
                axes[0, j].plot(range(len(values)), values, "o-", label=ip, color=IP_COLORS.get(ip))
            axes[0, j].set_title(medium)
            axes[0, j].set_xticks(range(len(self.data.periods)), [PERIOD_LABELS[period] for period in self.data.periods])
            axes[0, j].set_ylabel("Average RTT (ms)")
            axes[0, j].grid(axis="y", alpha=0.18)
        for ax in axes.flat:
            ax.set_ylim(0, max(1, maximum * 1.2))
        self.legend(figure, axes[0, 0])
        note = "10 attempts per target and period. One day only; this does not establish a repeated daily trend or meet the three-day requirement."
        self.save(figure, "rq3_peak_comparison.png", "RQ3: Average RTT by time of day", note)
        if {"4pm", "7pm"} <= set(self.data.periods):
            figure, axes = plt.subplots(1, 2, figsize=(12, 5))
            changes = np.full((len(self.data.media), len(self.data.ips)), np.nan)
            percentages = changes.copy()
            for i, medium in enumerate(self.data.media):
                for j, ip in enumerate(self.data.ips):
                    before = Stats.from_rows(self.data.select(3, period="4pm", medium=medium, ip=ip)).mean
                    after = Stats.from_rows(self.data.select(3, period="7pm", medium=medium, ip=ip)).mean
                    changes[i, j] = after - before
                    percentages[i, j] = (after - before) / before * 100 if before > 0 else math.nan
            for ax, matrix, label in zip(axes, (changes, percentages), ("Change in mean RTT (ms)", "Change in mean RTT (%)")):
                limit = max(1, float(np.nanmax(np.abs(matrix)))) if np.isfinite(matrix).any() else 1
                picture = ax.imshow(np.ma.masked_invalid(matrix), cmap="RdBu_r", vmin=-limit, vmax=limit, aspect="auto")
                ax.set_xticks(range(len(self.data.ips)), self.data.ips)
                ax.set_yticks(range(len(self.data.media)), self.data.media)
                ax.set_title(label)
                for i, j in np.ndindex(matrix.shape):
                    value = matrix[i, j]
                    label_text = f"{value:+.1f}" if math.isfinite(value) else "n/a"
                    ax.text(j, i, label_text, ha="center", va="center", color="white" if abs(value) > limit * 0.6 else "black")
                figure.colorbar(picture, ax=ax, shrink=0.8)
            self.save(figure, "rq3_peak_change.png", "RQ3: Change from 4 pm to 7 pm",
                      "Positive = higher mean at 7 pm; negative = lower. Percentage change uses the 4 pm mean as the denominator. One day only.")
        figure, axes = self.grid()
        maximum = max(response_times(self.data.select(3)), default=1)
        for i, period in enumerate(self.data.periods):
            for j, medium in enumerate(self.data.media):
                selected = self.data.select(3, period=period, medium=medium)
                if not selected:
                    continue
                start = min(row.timestamp for row in selected)
                for ip in self.data.ips:
                    rows = sorted([row for row in selected if row.ip == ip], key=lambda row: row.timestamp)
                    axes[i, j].plot([(row.timestamp - start).total_seconds() for row in rows],
                                    [row.rtt if row.rtt is not None else math.nan for row in rows], ".-",
                                    label=ip, color=IP_COLORS.get(ip))
                axes[i, j].set_xlabel(f"Seconds since {start:%H:%M:%S}")
                axes[i, j].set_ylabel("RTT (ms)")
                axes[i, j].set_ylim(0, maximum * 1.1)
        self.legend(figure, axes[0, 0])
        self.save(figure, "rq3_rtt_timeseries.png", "RQ3: Individual replies during each session",
                  "Times use the recorded local timestamps. Targets were tested sequentially. Gaps indicate missing replies.")

    def rq5(self) -> None:
        self.comparison(5, "rq5_load_comparison.png", "RQ5: Idle versus download")
        figure, axes = self.grid()
        maximum = max(response_times(self.data.select(5)), default=1)
        for i, period in enumerate(self.data.periods):
            for j, medium in enumerate(self.data.media):
                for condition in LOAD_LABELS:
                    rows = sorted(self.data.select(5, period=period, medium=medium, condition=condition), key=lambda row: row.timestamp)
                    axes[i, j].plot(range(1, len(rows) + 1), [row.rtt if row.rtt is not None else math.nan for row in rows],
                                    ".-", label=LOAD_LABELS[condition], color=LOAD_COLORS[condition], markersize=3, linewidth=1)
                    missing = [position for position, row in enumerate(rows, 1) if row.rtt is None]
                    axes[i, j].plot(missing, [0.025 if condition == "idle" else 0.055] * len(missing), "x",
                                    transform=axes[i, j].get_xaxis_transform(), color=LOAD_COLORS[condition], markersize=6)
                axes[i, j].set_xlabel("Request number within each condition")
                axes[i, j].set_ylabel("RTT (ms)")
                axes[i, j].set_ylim(0, maximum * 1.1)
        self.legend(figure, axes[0, 0], 2)
        self.save(figure, "rq5_rtt_timeseries.png", "RQ5: RTT fluctuations during idle and download runs",
                  "50 attempts per condition. Gaps and crosses near the axis mark no reply; crosses are not zero-millisecond measurements.")
        self.load_changes()
        self.load_table()

    def load_changes(self) -> None:
        figure, axes = plt.subplots(2, 1, figsize=(12, 7))
        labels, differences, losses, colors = [], [], [], []
        for period in self.data.periods:
            for medium in self.data.media:
                idle = Stats.from_rows(self.data.select(5, period=period, medium=medium, condition="idle"))
                load = Stats.from_rows(self.data.select(5, period=period, medium=medium, condition="download"))
                labels.append(f"{PERIOD_LABELS[period]}\n{medium}")
                differences.append(load.mean - idle.mean)
                losses.append(load.loss - idle.loss)
                colors.append(MEDIUM_COLORS[medium])
        for ax, values, ylabel in zip(axes, (differences, losses), ("Change in mean RTT (ms)", "Change in loss (percentage points)")):
            bars = ax.bar(range(len(labels)), values, color=colors)
            ax.bar_label(bars, labels=[f"{value:+.2f}" if math.isfinite(value) else "n/a" for value in values], padding=3, fontsize=9)
            ax.axhline(0, color="#444444", linewidth=0.8)
            ax.set_xticks(range(len(labels)), labels)
            ax.set_ylabel(ylabel)
            ax.grid(axis="y", alpha=0.18)
            ax.margins(y=0.22)
        self.save(figure, "rq5_load_change.png", "RQ5: Download minus idle",
                  "Negative values mean the download-labelled run was lower. These differences do not establish how much traffic used the link.")

    def load_table(self) -> None:
        cells = []
        for period in self.data.periods:
            for medium in self.data.media:
                for condition in LOAD_LABELS:
                    summary = Stats.from_rows(self.data.select(5, period=period, medium=medium, condition=condition))
                    cells.append([PERIOD_LABELS[period], medium, LOAD_LABELS[condition], f"{summary.replies}/{summary.sent}",
                                  number(summary.mean), number(summary.median), number(summary.p95), number(summary.stddev),
                                  number(summary.loss, 1)])
        figure, ax = plt.subplots(figsize=(14, 0.38 * len(cells) + 2.5))
        ax.axis("off")
        headers = ["Period", "Medium", "Condition", "Replies/sent", "Mean ms", "Median ms", "95th %ile ms", "SD ms", "Loss %"]
        table = ax.table(cellText=cells, colLabels=headers, cellLoc="center", loc="center",
                         colWidths=[0.15, 0.09, 0.1, 0.1, 0.1, 0.1, 0.12, 0.1, 0.08])
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 1.8)
        for (row, column), cell in table.get_celld().items():
            cell.set_edgecolor("#DDDDDD")
            cell.set_facecolor("#EAF0F5" if row == 0 else ("#F7F8FA" if (row - 1) // 2 % 2 else "white"))
        self.save(figure, "rq5_summary_table.png", "RQ5: Results by connection and time",
                  "RTT statistics exclude timeouts. SD is sample standard deviation; the 95th percentile describes these samples.")


    def rq4(self):
        targets = self.data.targets
        sessions = [(period, medium) for period in self.data.periods for medium in self.data.media]
        values = np.array([
            [Stats.from_rows(self.data.select(4, period=p, medium=m, ip=t["ip"])).mean for p, m in sessions] for t in targets
        ], dtype=float)
        labels = [f'{t["city"]} · {t["operator"]}\n{t["ip"]}' for t in targets]
        maximum = np.nanmax(values) if np.isfinite(values).any() else 1.0
        fig, axes = plt.subplots(1, len(self.data.periods), figsize=(17, 9), sharex=True, sharey=True, squeeze=False)
        positions = np.arange(len(targets))
        width = 0.23
        for column, period in enumerate(self.data.periods):
            ax = axes[0, column]
            for index, medium in enumerate(self.data.media):
                series = values[:, column * len(self.data.media) + index]
                offset = (index - (len(self.data.media) - 1) / 2) * width
                bars = ax.barh(positions + offset, series, width, color=MEDIUM_COLORS[medium], label=medium)
                ax.bar_label(bars, labels=[f"{v:.1f}" if np.isfinite(v) else "n/a" for v in series], padding=3, fontsize=8)
            ax.set_title(PERIOD_LABELS[period])
            ax.set_yticks(positions, labels, fontsize=9)
            ax.set_xlabel("Mean RTT (ms)")
            ax.set_xlim(0, maximum * 1.16)
            ax.grid(axis="x", alpha=0.2)
            ax.set_axisbelow(True)
            ax.legend(loc="lower right", fontsize=9)
        axes[0, 0].invert_yaxis()
        self.save(fig, "rq4_destination_rtt.png", "RQ4: RTT by destination", "Means include replies only; 10 attempts per destination and session")
        self._rq4_distance(values, maximum)
        self._rq4_heatmap(values, labels, sessions, maximum)

    def _rq4_distance(self, values, maximum):
        distances = np.array([self.data.distance(t["ip"]) for t in self.data.targets], dtype=float)
        if not np.isfinite(distances).any():
            print("Skipping RQ4 distance plot: no recorded distances or destination coordinates")
            return
        names = [t["city"] for t in self.data.targets]
        names = [f'{name} {t["operator"][0]}' if names.count(name) > 1 else name for name, t in zip(names, self.data.targets)]
        offsets = [(6, 7), (6, 16), (6, -16), (-4, 10), (-8, 14), (6, -15), (-4, -15), (-6, 14), (-6, -17)]
        fig, axes = self.grid()
        for row, period in enumerate(self.data.periods):
            for column, medium in enumerate(self.data.media):
                ax = axes[row, column]
                means = values[:, row * len(self.data.media) + column]
                valid = np.isfinite(distances) & np.isfinite(means)
                ax.scatter(distances[valid], means[valid], s=34, color=MEDIUM_COLORS[medium], zorder=3)
                if len(np.unique(distances[valid])) >= 3:
                    slope, intercept = np.polyfit(distances[valid], means[valid], 1)
                    fitted = slope * distances[valid] + intercept
                    variance = np.sum((means[valid] - np.mean(means[valid])) ** 2)
                    r_squared = 1 - np.sum((means[valid] - fitted) ** 2) / variance if variance > 0 else np.nan
                    x = np.array([np.min(distances[valid]), np.max(distances[valid])])
                    ax.plot(x, slope * x + intercept, "--", color="#697386", linewidth=1.2)
                    ax.text(0.97, 0.94, f"Linear fit R² = {r_squared:.2f}", transform=ax.transAxes, ha="right", va="top", fontsize=9)
                nearby = [i for i in range(len(distances)) if valid[i] and distances[i] <= 1000]
                previous = -np.inf
                for index in sorted(nearby, key=lambda i: means[i]):
                    label_y = max(means[index], maximum * 0.025, previous + maximum * 0.062)
                    ax.annotate(names[index], (distances[index], means[index]), xytext=(1700, label_y), fontsize=8, va="center",
                                arrowprops={"arrowstyle": "-", "color": "#9aa0a6", "linewidth": 0.6})
                    previous = label_y
                for index, (distance, value, name) in enumerate(zip(distances, means, names)):
                    if valid[index] and index not in nearby:
                        offset = offsets[index % len(offsets)]
                        ax.annotate(name, (distance, value), xytext=offset, textcoords="offset points", fontsize=8,
                                    ha="right" if offset[0] < 0 else "left", va="center")
                ax.set_xlabel("Estimated city distance (km)")
                ax.set_ylabel("Mean RTT (ms)")
                ax.set_xlim(-350, np.nanmax(distances) * 1.08 if np.isfinite(distances).any() else 18000)
                ax.set_ylim(0, maximum * 1.2)
                ax.grid(alpha=0.2)
        self.save(fig, "rq4_distance_rtt.png", "RQ4: Distance and RTT",
                  f"Approximate distances from {ORIGIN_CITY} city coordinates; not route length; dashed lines show linear fits")

    def _rq4_heatmap(self, values, labels, sessions, maximum):
        fig, ax = plt.subplots(figsize=(15, 9))
        palette = plt.get_cmap("Blues").copy()
        palette.set_bad("#eeeeee")
        mesh = ax.imshow(np.ma.masked_invalid(values), cmap=palette, vmin=0, vmax=maximum, aspect="auto")
        ax.set_xticks(np.arange(len(sessions)), [f"{PERIOD_LABELS[p]}\n{m}" for p, m in sessions], fontsize=10)
        ax.set_yticks(np.arange(len(labels)), labels, fontsize=9)
        ax.tick_params(length=0)
        for row in range(values.shape[0]):
            for column in range(values.shape[1]):
                value = values[row, column]
                label = f"{value:.1f}" if np.isfinite(value) else "n/a"
                color = "white" if np.isfinite(value) and value > maximum / 2 else "#17212f"
                ax.text(column, row, label, ha="center", va="center", color=color, fontsize=11)
        fig.colorbar(mesh, ax=ax, pad=0.025, label="Mean RTT (ms)")
        self.save(fig, "rq4_rtt_heatmap.png", "RQ4: Destination RTT across all sessions",
                  "Each cell is the mean of successful replies; n/a means no replies")


@click.command(help="Plot the saved experiments by connection type and period.")
@click.option("--rq", type=click.Choice(["1", "2", "3", "4", "5"]), help="Plot one question; otherwise plot all five.")
@click.option("--evidence", default=BASE_DIRECTORY / "runs", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", default=BASE_DIRECTORY / "runs" / "viz", type=click.Path(file_okay=False, path_type=Path))
def main(rq: str | None, evidence: Path, output: Path) -> None:
    data = Dataset(evidence)
    plotter = Plotter(data, output)
    totals = Stats.from_rows(data.rows)
    print(f"{len(data.paths)} CSV files: {totals.attempts} attempts, {totals.replies} replies, {totals.sent - totals.replies} without replies")
    print("Medium and period labels come from run folders; raw CSV condition fields are not modified.")
    if not rq:
        plotter.overview()
    for question in ((int(rq),) if rq else range(1, 6)):
        if data.select(question):
            getattr(plotter, f"rq{question}")()
    print(f"Generated {len(plotter.generated)} PNG files in {output}")
    print("RQ1 fragmentation needs packet captures. RQ3 still needs the other days and morning measurements.")
    print("RQ4 uses approximate city distances from Melbourne. RQ5 labels do not verify download duration or utilization.")


if __name__ == "__main__":
    main()
