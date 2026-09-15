# Custom Ping+DNS in Python

Custom IPv4 ICMP ping in Python with a local DNS resolver, latency statistics, and packet loss reporting.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install click matplotlib
python server.py --upstream 1.1.1.1
```

In another terminal, run (raw ICMP requires administrator privileges):

```sh
sudo python ping.py mydomain.local -c 4
```

IP addresses work without the DNS server. Use `--help` for options. `experiments.py` saves CSV measurements; `visualise.py` plots results (requires Matplotlib).
