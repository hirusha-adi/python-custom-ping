#!/usr/bin/env bash
# ==================== setup ====================
# stop on errors or missing variables
set -eu

# work from the script folder
cd "$(dirname "$0")"

# uses the system python-click and python-matplotlib packages
# clear the last test run and copy the location labels
rm -rf runs/test
mkdir -p runs/test
cp geography-targets.json runs/test/

# ==================== dns and ping checks ====================
# start the dns server in another terminal first
# try the local name then the five public names
sudo python3 ping.py mydomain.local -c 2
sleep 1
sudo python3 ping.py www.google.com -c 2 || true
sleep 1
sudo python3 ping.py www.cloudflare.com -c 2 || true
sleep 1
sudo python3 ping.py www.python.org -c 2 || true
sleep 1
sudo python3 ping.py example.com -c 2 || true
sleep 1
sudo python3 ping.py www.wikipedia.org -c 2 || true
sleep 1

# ==================== RQs 1-5 ====================

# rq1 payload size
# try the four payload sizes
sudo python3 experiments.py 1.1.1.1 8.8.8.8 9.9.9.9 --rq 1 --output runs/test/rq1.csv

# rq2 connection type
# change the connection and label for the next medium
sudo python3 experiments.py 1.1.1.1 8.8.8.8 9.9.9.9 --rq 2 --condition wired --count 10 --output runs/test/rq2.csv

# rq3 time of day
# use the actual time period here and repeat on the other days
sudo python3 experiments.py 1.1.1.1 8.8.8.8 9.9.9.9 --rq 3 --condition afternoon --count 10 --output runs/test/rq3.csv

# rq4 destination locations
# check the starting location before using these tiers
sudo python3 experiments.py 67.219.110.24 108.61.212.117 192.53.169.225 --rq 4 --condition close --count 10 --output runs/test/rq4.csv
sudo python3 experiments.py 45.32.100.168 108.61.201.151 141.164.34.61 --rq 4 --condition midway --count 10 --output runs/test/rq4.csv
sudo python3 experiments.py 108.61.194.105 108.61.198.102 108.61.210.117 --rq 4 --condition far --count 10 --output runs/test/rq4.csv

# rq5 baseline and download
# 50 baseline pings
sudo python3 experiments.py 1.1.1.1 --rq 5 --condition idle --count 50 --output runs/test/rq5.csv
# 50 more pings while downloading a 100MB file from Vultr
curl -fsSL --max-time 120 -o /dev/null -w '\nDownload: %{size_download} bytes in %{time_total} seconds\n' https://mel-au-ping.vultr.com/vultr.com.100MB.bin &
sudo python3 experiments.py 1.1.1.1 --rq 5 --condition download --count 50 --output runs/test/rq5.csv
wait "$!" || echo "Download stopped or failed; check its output before using RQ5."

# results and graphs
# make the tables and graphs from this run
python3 visualise.py --evidence runs/test --output runs/test/plots
echo "Results and graphs: runs/test"
