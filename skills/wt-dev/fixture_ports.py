"""Private fixture ranges. UDP claims prevent overlap between concurrent test processes."""
import socket


class PortRange:
    def __init__(self, size=8):
        self.claims = []
        for _ in range(100):
            probes = []
            try:
                # Choose and claim the base with UDP before touching TCP. A TCP
                # bind(0) could steal a claimed port while its server starts.
                first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.claims.append(first)
                first.bind(('127.0.0.1', 0))
                base = first.getsockname()[1]
                if base + size > 65536:
                    self.close()
                    continue
                for port in range(base + 1, base + size):
                    # Keep UDP claims until fixture cleanup. TCP stays available for
                    # the real child servers. No shared directory or stale lock files.
                    claim = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.claims.append(claim)
                    claim.bind(('127.0.0.1', port))
                for port in range(base, base + size):
                    probe = socket.socket()
                    probes.append(probe)
                    probe.bind(('127.0.0.1', port))
                self.base = base
                self.ports = range(base, base + size)
                return
            except OSError:
                self.close()
            finally:
                for probe in probes:
                    probe.close()
        raise RuntimeError('no free fixture port range after 100 attempts')

    def close(self):
        for claim in self.claims:
            claim.close()
        self.claims.clear()
