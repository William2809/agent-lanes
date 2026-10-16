"""Private tool-version input for runner fixtures, with no package download."""
from pathlib import Path


def isolate_corepack(remote, home):
    # The portable PATH can expose a host Corepack shim. A fixture HOME has no
    # Corepack store. Its version query must not provision packages or use the
    # host's mutable cache. These fixtures exercise runner flow, not pnpm.
    versions = Path(home) / 'tool-versions'
    versions.mkdir(exist_ok=True)
    probe = versions / 'pnpm'
    probe.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = --version ] || exit 97\n'
                     "printf '10.0.0\\n'\n")
    probe.chmod(0o755)
    with (Path(remote) / 'platform.sh').open('a') as stream:
        stream.write('\nplatform_path="$HOME/tool-versions:$platform_path"\n')
