"""Explicit red check for the lane brief, before running the candidate fixtures.

Run through remote-ci: python3 skills/remote-ci/pool_baseline.py <old-commit>.
This harness never resets or changes the checkout under test.
REMOTE_CI_BASELINE_REPOSITORY can select the runner's bare submission store when
an old rebased commit is absent from the current slot's checkout.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    kit = Path(__file__).resolve().parent
    repository = os.environ.get('REMOTE_CI_BASELINE_REPOSITORY', '.')
    archive = subprocess.check_output(['git', '-C', repository, 'archive', sys.argv[1], 'skills/remote-ci'])
    with tempfile.TemporaryDirectory(prefix='remote-ci-pool-baseline-') as tmp:
        subprocess.run(['tar', '-x', '-C', tmp], input=archive, check=True)
        old = Path(tmp) / 'skills/remote-ci'
        for source in kit.glob('test_pool*.py'):
            shutil.copyfile(source, old / source.name)
        if len(sys.argv) > 2 and sys.argv[2] == 'postgres-path':
            tests = ['test_pool.Adapters.test_postgres_path_before_install']
            expected = 'FAILED (failures=1)'
            label = 'PostgreSQL path before installation fails'
        elif len(sys.argv) > 2 and sys.argv[2] == 'macos-memory':
            tests = ['test_pool.Adapters.test_macos_reclaimable_memory']
            expected = 'FAILED (failures=2)'
            label = 'macOS kernel memory estimate fails on the old adapter'
        elif len(sys.argv) > 2 and sys.argv[2] == 'scoring':
            tests = ['test_pool_scoring']
            expected = 'FAILED (failures=5)'
            label = 'admission and competitive wake regressions fail on the old pool'
        elif len(sys.argv) > 2 and sys.argv[2] == 'availability':
            tests = ['test_pool_availability']
            expected = 'FAILED (failures=10, errors=2)'
            label = 'ready-host, capacity and freeze regressions fail on the old pool'
        else:
            tests = ['test_pool', 'test_pool_runner']
            # All original 21 cases must fail on the protocol-2 baseline.
            # Two entry points reject the contract; 19 lack new modules.
            expected = 'FAILED (failures=2, errors=19)'
            label = '21 requirement tests fail (2 assertions, 19 missing-feature errors)'
        result = subprocess.run([sys.executable, '-m', 'unittest', *tests], cwd=old,
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 1 or expected not in result.stderr:
            print(result.stderr)
            raise SystemExit('FAIL old pool baseline did not produce the expected failures')
        print('ok old pool baseline: ' + label)


if __name__ == '__main__':
    main()
