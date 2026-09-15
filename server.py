# dns packet layout https://github.com/jvns/dns-weekend

import socket
import struct
import time
from io import BytesIO

import click

# ======== local dns records ========
ZONE = 'mydomain.local'
RECORDS = {
    'mydomain.local': '127.0.0.1',
    'www.mydomain.local': '127.0.0.1',
    'mail.mydomain.local': '127.0.0.2',
}

# values for the dns header flags
RESPONSE = 32768
AUTHORITATIVE = 1024
RECURSION_DESIRED = 256
RECURSION_AVAILABLE = 128


# ======== encode a dns name ========
# put the length before each label
def encode_name(name):
    result = b''
    name = name.rstrip('.')
    labels = name.split('.')
    for label in labels:
        label = label.encode('ascii')
        if not 1 <= len(label) <= 63:
            raise ValueError('DNS labels must contain 1 to 63 bytes')
        result += bytes([len(label)])
        result += label
    if len(result) > 254:
        raise ValueError('DNS name is too long')
    return result + b'\0'


# ======== read a dns name ========
def read_name(reader, depth=0):
    # stop bad pointers looping forever
    if depth > 10:
        raise ValueError('DNS compression pointer loop')
    
    parts = []
    while True:
        byte = reader.read(1)
        if not byte:
            raise ValueError('Incomplete DNS name')
        length = byte[0]
        if length == 0:
            return '.'.join(parts).lower()
        if length >= 192:

            # this points to a name elsewhere in the packet
            first_byte = length - 192  # remove the pointer marker
            second_byte = reader.read(1)
            if not second_byte:
                raise ValueError('Incomplete DNS pointer')
            pointer_bytes = bytes([first_byte]) + second_byte
            pointer = int.from_bytes(pointer_bytes, byteorder='big')
            position = reader.tell()
            reader.seek(pointer)
            name = read_name(reader, depth + 1)
            parts.append(name)

            # go back to where we were reading
            reader.seek(position)
            return '.'.join(parts).lower()
        
        if length > 63:
            raise ValueError('Invalid DNS label length')
        
        label = reader.read(length)
        parts.append(label.decode('ascii'))


# ======== send a udp dns query ========
# send the query as is and wait for a reply
def exchange(query, server, port, timeout=2):
    socket.inet_pton(socket.AF_INET, server)  # needs a numeric ip here
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, port))
        sock.send(query)
        reply = sock.recv(65535)

    # check the reply header and query id
    if len(reply) < 12:
        raise ValueError('Incomplete DNS response')
    if reply[:2] != query[:2]:
        raise ValueError('DNS transaction IDs do not match')
    if reply[2] < 128:  # first bit marks a response
        raise ValueError('Expected a DNS response')
    
    return reply


# ======== answer or forward a query ========
def answer(query, client, upstream, upstream_port=53):
    # read the header then the name and record type
    reader = BytesIO(query)
    transaction_id, flags, questions, _, _, _ = struct.unpack('!6H', reader.read(12))
    flag_bits = format(flags, '016b')  # one character per flag bit
    if questions != 1 or flag_bits[:5] != '00000':
        raise ValueError('Expected a standard DNS query with one question')
    name = read_name(reader)
    kind, family = struct.unpack('!HH', reader.read(4))

    # check if the name belongs to our zone
    local = name == ZONE or name.endswith('.' + ZONE)
    route = 'forward'
    if local:
        route = 'local'
    print(f'{client[0]}:{client[1]} id={transaction_id} {name}: {route}')
    if not local:
        return exchange(query, upstream, upstream_port)

    # only look up ipv4 a records in the internet class
    address = None
    if kind == 1 and family == 1:
        address = RECORDS.get(name)

    # set the reply flags and keep the recursion bit
    flags = RESPONSE + AUTHORITATIVE + RECURSION_AVAILABLE
    if flag_bits[7] == '1':
        flags += RECURSION_DESIRED
    if name not in RECORDS:
        flags += 3  # nxdomain means the name is missing from our zone
    answer_count = 0
    if address:
        answer_count = 1

    # reuse the id and question then add the record if found
    header = struct.pack('!6H', transaction_id, flags, 1, answer_count, 0, 0)
    question = encode_name(name) + struct.pack('!HH', kind, family)
    record = b''
    if address:
        record = encode_name(name)
        record += struct.pack('!HHIH', 1, 1, 60, 4)
        record += socket.inet_aton(address)

    return header + question + record


# ======== command line options and server loop ========
@click.command()
@click.option('--port', default=5354, type=click.IntRange(1, 65535))
@click.option('--upstream', required=True, help='Numeric IPv4 address of your DNS resolver.')
@click.option('--add-delay', default=0.0, type=click.FloatRange(min=0), show_default=True, help='Delay each DNS reply by this many seconds.')
def main(port, upstream, add_delay):
    socket.inet_pton(socket.AF_INET, upstream)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', port))
        print(f'DNS 127.0.0.1:{port}, zone={ZONE}, upstream={upstream}:53')

        # read a request and reply to the same client
        while True:
            query, client = sock.recvfrom(65535)
            try:
                response = answer(query, client, upstream)
                # add a delay when testing timeouts
                if add_delay > 0:
                    print(f'Delaying DNS reply by {add_delay:g} seconds')
                    time.sleep(add_delay)
                sock.sendto(response, client)
            except (OSError, ValueError, struct.error) as error:
                print(f'{client}: {error}')


# only start listening when this file runs directly
if __name__ == '__main__':
    main()
