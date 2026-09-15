import csv
import time
from pathlib import Path

import click

from ping import ping


# ======== command line options ========
@click.command()
@click.argument("hosts", nargs=-1, required=True)
@click.option("--rq", type=click.IntRange(1, 5), required=True)
@click.option("--condition", default="baseline", help="For example: wired, morning, close or idle.")
@click.option("--size", "sizes", multiple=True, type=click.IntRange(0, 65507))
@click.option("--count", type=click.IntRange(1, 65535))
@click.option("--distance-km", multiple=True, type=click.FloatRange(min=0))
@click.option("--output", default="results.csv", type=click.Path(path_type=Path, dir_okay=False))
def main(hosts, rq, condition, sizes, count, distance_km, output):
    """Collect real measurements for HOSTS. Repeat runs after changing conditions."""
    # ======== default experiment settings ========
    # 10 pings per size for rq1 otherwise 50
    if count is None:
        if rq == 1:
            count = 10
        else:
            count = 50

    # use the four sizes for rq1 otherwise 56 bytes
    if not sizes:
        if rq == 1:
            sizes = (32, 500, 1400, 3000)
        else:
            sizes = (56,)

    # check the packet count and number of distances
    if rq == 1 and count < 10:
        raise click.UsageError("RQ1 needs at least 10 packets per size.")
    if distance_km and len(distance_km) != len(hosts):
        raise click.UsageError("Give one --distance-km for each host, in the same order.")

    # ======== open the results csv ========
    # save the experiment labels with each ping result
    fields = [
        "rq",
        "condition",
        "distance_km",
        "timestamp",
        "host",
        "ip",
        "sequence",
        "size",
        "bytes",
        "ttl",
        "rtt_ms",
        "status",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a+", newline="", encoding="utf-8") as file:
        # check the columns before adding more rows
        file.seek(0)
        header = next(csv.reader(file), None)
        if header and header != fields:
            raise click.ClickException("CSV columns differ. Choose a new output file.")
        file.seek(0, 2)
        writer = csv.DictWriter(file, fieldnames=fields)
        if not header:
            writer.writeheader()

        # ======== run each host and payload size ========
        try:
            for index, host in enumerate(hosts):
                distance = ""
                if distance_km:
                    distance = distance_km[index]

                for size in sizes:
                    # pause between runs since these are separate ping calls
                    time.sleep(1)
                    rows = ping(host, count=count, size=size)

                    # add the rq and condition to each result
                    for row in rows:
                        measurement = row.copy()
                        measurement["rq"] = rq
                        measurement["condition"] = condition
                        measurement["distance_km"] = distance
                        writer.writerow(measurement)

                    # flush these results before the next run
                    file.flush()
                    print(f"Saved {len(rows)} measurements to {output}")
                    if len(rows) < count:
                        return
                    
        except KeyboardInterrupt:
            # keep completed results and exit on ctrl c
            print("Stopped. Completed host runs are saved.")
            raise SystemExit(130)


# ======== start the experiment ========
if __name__ == "__main__":
    main()
