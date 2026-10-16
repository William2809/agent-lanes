"""Review P1: concurrent allocators must not take a claimed fixture's TCP port."""
import multiprocessing
import socket
import unittest
from unittest.mock import patch

from fixture_ports import PortRange


def allocate(role, port, start, allocated, contender, served, release, result):
    real_socket = socket.socket
    first = True

    class ScheduledSocket(real_socket):
        def bind(self, address):
            nonlocal first
            if address[1] != 0 or not first:
                return super().bind(address)
            first = False
            if role == 'contender' and not allocated.wait(10):
                raise RuntimeError('owner did not allocate')
            # Force the same kernel candidate in both processes. All sockets and
            # conflicts are real; only candidate choice and scheduling are fixed.
            try:
                value = super().bind((address[0], port))
            except OSError:
                if role == 'contender':
                    contender.set()
                raise
            if role == 'contender':
                contender.set()
                if not served.wait(10):
                    raise RuntimeError('owner did not try its server')
            return value

    reservation = None
    try:
        start.wait(10)
        with patch('socket.socket', ScheduledSocket):
            reservation = PortRange(size=1)
        if role == 'owner':
            allocated.set()
            if not contender.wait(10):
                raise RuntimeError('contender did not attempt allocation')
            try:
                with real_socket() as server:
                    server.bind(('127.0.0.1', reservation.base))
                    server.listen()
                    result.send(('listening', reservation.base))
                    served.set()
                    release.wait(10)
            except OSError as error:
                result.send(('server bind failed', error.errno))
                served.set()
        else:
            result.send(('allocated', reservation.base))
            release.wait(10)
    except Exception as error:
        result.send(('worker error', str(error)))
    finally:
        if reservation is not None:
            reservation.close()
        result.close()


class PortRangeRace(unittest.TestCase):
    def test_concurrent_allocators_leave_claimed_tcp_port_available(self):
        # P1 requirement: a competing allocator cannot steal the owner's TCP
        # port between allocation and server bind, even with the same candidate.
        ctx = multiprocessing.get_context('spawn')
        start = ctx.Barrier(3)
        allocated, contender, served, release = [ctx.Event() for _ in range(4)]
        children, readers = [], []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as seed:
            seed.bind(('127.0.0.1', 0))
            port = seed.getsockname()[1]
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
                for role in ('owner', 'contender'):
                    reader, writer = ctx.Pipe(duplex=False)
                    child = ctx.Process(target=allocate, args=(role, port, start,
                        allocated, contender, served, release, writer))
                    child.start()
                    writer.close()
                    children.append(child)
                    readers.append(reader)
        try:
            start.wait(10)  # Both independent processes enter allocation together.
            for reader in readers:
                self.assertTrue(reader.poll(15), 'allocator did not return')
            owner, other = [reader.recv() for reader in readers]
            self.assertEqual(owner, ('listening', port), owner)
            self.assertEqual(other[0], 'allocated', other)
            self.assertNotEqual(other[1], port)
        finally:
            release.set()
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            for reader in readers:
                reader.close()
        for child in children:
            self.assertEqual(child.exitcode, 0)
