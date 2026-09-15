# icmp example https://github.com/alessandromaggio/pythonping/tree/master/pythonping
# dns packet layout https://github.com/jvns/dns-weekend

import errno
import ipaddress
import random
import select
import socket
import statistics
import struct
import sys
import time
from datetime import datetime
from io import BytesIO

import click

from server import encode_name, exchange, read_name


# ======== dns query ========
def make_query(name, transaction_id):
    # ask for one ipv4 address
    recursion_desired = 256
    header = struct.pack('!6H', transaction_id, recursion_desired, 1, 0, 0, 0)
    question = encode_name(name)

    # type 1 for an a record and class 1 for internet
    question += struct.pack('!HH', 1, 1)

    return header + question


# ======== dns reply ========
def resolve(hostname, server='127.0.0.1', port=5354, timeout=2):
    # match the reply to this query using its id
    transaction_id = random.randint(0, 65535)
    print(f'DNS {hostname} via {server}:{port}, id={transaction_id}')
    query = make_query(hostname, transaction_id)
    reply = exchange(query, server, port, timeout)

    # read the 12 byte header first
    reader = BytesIO(reply)
    _, flags, questions, answers, _, _ = struct.unpack('!6H', reader.read(12))

    # read the truncation flag and error code as bits
    flag_bits = format(flags, '016b')
    if flag_bits[6] == '1':
        raise ValueError('The DNS response was truncated')
    error_code = int(flag_bits[12:16], 2)
    if error_code != 0:
        raise ValueError(f'DNS lookup failed (rcode={error_code})')
    
    # skip the questions to reach the answers
    for _ in range(questions):
        read_name(reader)
        reader.read(4)

    for _ in range(answers):
        read_name(reader)
        kind, family, ttl, length = struct.unpack('!HHIH', reader.read(10))
        data = reader.read(length)

        # skip any cname before the a record
        if kind == 1 and family == 1 and len(data) == 4:
            address = socket.inet_ntoa(data)
            print(f'DNS answer: {hostname} = {address}, id={transaction_id}')
            return address
        
    raise ValueError(f'No IPv4 address in the reply for {hostname}')


# ======== icmp checksum ========
def checksum(data):
    # pad odd lengths so every byte has a pair
    if len(data) % 2:
        data += b"\x00"
    total = 0

    # combine each pair with the high byte first
    for i in range(0, len(data), 2):
        first_byte = data[i]
        second_byte = data[i + 1]
        number = first_byte * 256 + second_byte
        total += number

    # add overflow back into the 16 bit sum
    while total > 65535:
        overflow = total // 65536
        total = total % 65536
        total += overflow

    # flip the bits in the final sum
    return 65535 - total


# ======== send a ping ========
def send_ping(sock, ip, identifier, sequence, payload):
    # header order is type code checksum id sequence
    header = struct.pack("!BBHHH", 8, 0, 0, identifier, sequence)

    # calculate with zero in the checksum field then fill it in
    check = checksum(header + payload)
    packet = struct.pack("!BBHHH", 8, 0, check, identifier, sequence) + payload

    # start the timer just before sending
    sent = time.perf_counter()
    sock.sendto(packet, (ip, 0))

    return sent


# ======== receive a ping ========
def receive_ping(sock, ip, identifier, sequence, sent, timeout):
    while True:
        # keep the same deadline when other packets arrive
        elapsed = time.perf_counter() - sent
        remaining = timeout - elapsed
        if remaining <= 0:
            return 0, None, None, "timeout"
        
        # wait for data or the timeout
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            return 0, None, None, "timeout"
        packet, source = sock.recvfrom(65535)
        elapsed = (time.perf_counter() - sent) * 1000
        if len(packet) < 20:
            continue
        
        # skip the ipv4 header using its length in 4 byte words
        header_words = packet[0] % 16
        offset = header_words * 4
        message = packet[offset:]
        # skip short packets and bad checksums
        if offset < 20 or len(message) < 8 or checksum(message) != 0:
            continue
        kind, code, check, reply_id, reply_seq = struct.unpack("!BBHHH", message[:8])

        # match the reply address id and sequence
        if kind == 0 and code == 0 and source[0] == ip:
            if reply_id == identifier and reply_seq == sequence:
                # byte 8 holds the ttl
                return len(message), packet[8], elapsed, "reply"
            
        # errors include our ipv4 header and first 8 icmp bytes
        if kind in (3, 11) and len(message) >= 36:
            original = message[8:]
            header_words = original[0] % 16
            start = header_words * 4
            if start < 20 or len(original) < start + 8:
                continue
            request_header = original[start:start + 8]
            request_type, request_code, request_check, request_id, request_seq = struct.unpack("!BBHHH", request_header)

            if socket.inet_ntoa(original[16:20]) != ip:
                # skip errors for other requests
                continue

            if request_type == 8 and request_id == identifier and request_seq == sequence:
                status = "unreachable"
                if kind == 11:
                    status = "time_exceeded"
                if kind == 3 and code == 4:
                    status = "fragmentation_needed"
                return 0, None, None, status


# ======== ping statistics ========
def print_statistics(host, rows):
    sent = 0
    rtts = []

    # local size errors were never sent and only replies have an rtt
    for row in rows:
        if row["status"] != "too_big":
            sent += 1
        if row["status"] == "reply":
            rtts.append(row["rtt_ms"])

    loss = "n/a"
    if sent > 0:
        lost = sent - len(rtts)
        loss_percentage = lost / sent * 100
        loss = f"{loss_percentage:.1f}%"

    print(f"\n--- {host} ping statistics ---")
    print("ping destination IP addresses in Python")
    print(f"{sent} packets transmitted, {len(rtts)} received, {loss} packet loss")
    if len(rows) != sent:
        print(f"{len(rows) - sent} packets rejected locally: too large")

    if rtts:
        minimum = min(rtts)
        average = statistics.mean(rtts)
        maximum = max(rtts)
        print(f"rtt min/avg/max = {minimum:.3f}/{average:.3f}/{maximum:.3f} ms")


# ======== run the pings ========
def ping(host, count=4, size=56, timeout=2, interval=1, dns_server="127.0.0.1", dns_port=5354):
    # use ip addresses directly and look up names through our dns server
    try:
        ip = str(ipaddress.IPv4Address(host))
    except ValueError:
        ip = resolve(host, dns_server, dns_port, timeout)
        
    # keep at least one second between requests
    if interval < 1:
        interval = 1

    rows = []
    identifier = random.randint(0, 65535)
    payload = b"Q" * size
    # send icmp ourselves and let the os add the ipv4 header
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 64)
        if sys.platform.startswith("linux"):
            # allow large packets to be split if needed
            sock.setsockopt(socket.IPPROTO_IP, 10, 0)
        print(f"PING {host} ({ip}) {size} bytes of data.")

        for sequence in range(1, count + 1):
            # save the date separately from the timer
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            started = time.perf_counter()
            try:
                sent = send_ping(sock, ip, identifier, sequence, payload)
                received, ttl, rtt, status = receive_ping(sock, ip, identifier, sequence, sent, timeout)
            except OSError as error:
                # emsgsize means the packet was too big to send
                if error.errno != errno.EMSGSIZE:
                    raise
                received, ttl, rtt, status = 0, None, None, "too_big"

            # save failures too so we can calculate loss
            row = {
                "timestamp": timestamp,
                "host": host,
                "ip": ip,
                "sequence": sequence,
                "size": size,
                "bytes": received,
                "ttl": ttl,
                "rtt_ms": rtt,
                "status": status,
            }
            rows.append(row)
            if status == "reply":
                print(f"{received} bytes from {ip}: icmp_seq={sequence} ttl={ttl} time={rtt:.3f} ms")
            elif status == "timeout":
                print(f"Request timed out (icmp_seq={sequence})")
            else:
                print(f"icmp_seq={sequence}: {status.replace('_', ' ')}")

            # subtract time already spent waiting for the reply
            if sequence < count:
                elapsed = time.perf_counter() - started
                delay = interval - elapsed
                if delay > 0:
                    time.sleep(delay)

    print_statistics(host, rows)
    return rows


# ======== single ping for the task ========
def my_ping(host):
    # return the rtt or none if there was no reply
    rows = ping(host, count=1)
    return rows[0]["rtt_ms"]


# ======== command line options ========
@click.command()
@click.argument("host")
@click.option("-c", "--count", default=4, type=click.IntRange(1, 65535))
@click.option("-s", "--size", default=56, type=click.IntRange(0, 65507))
@click.option("-W", "--timeout", default=2.0, type=click.FloatRange(0, 60, min_open=True))
@click.option("-i", "--interval", default=1.0, type=click.FloatRange(1, 3600))
@click.option("--dns-server", default="127.0.0.1")
@click.option("--dns-port", default=5354, type=click.IntRange(1, 65535))
def main(host, count, size, timeout, interval, dns_server, dns_port):
    """Send ICMP echo requests to HOST through the custom DNS resolver."""
    try:
        rows = ping(host, count, size, timeout, interval, dns_server, dns_port)
    except PermissionError:
        print("Raw ICMP needs root/Administrator privileges. Try: sudo python3 ping.py HOST")
        raise SystemExit(1)
    except (OSError, ValueError) as error:
        print(f"Error: {error}")
        raise SystemExit(1)
    
    # success needs at least one reply
    for row in rows:
        if row["status"] == "reply":
            return
        
    raise SystemExit(1)


if __name__ == "__main__":
    main()
