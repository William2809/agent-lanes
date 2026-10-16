"""Regression fixtures contain synthetic data only, never local transcripts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('stats', Path(__file__).with_name('session-stats.py'))
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


def row(second, kind, content, **extra):
    return dict(timestamp=f'2026-10-02T00:00:{second:02d}Z', type=kind,
                message={'id': f'm{second}', 'content': content}, **extra)


def call(second, cid, command, name='Bash', **inputs):
    return row(second, 'assistant', [dict(type='tool_use', id=cid, name=name,
                                        input=dict(command=command, **inputs))])


def result(second, cid, text, **extra):
    return row(second, 'user', [dict(type='tool_result', tool_use_id=cid, content=text)], **extra)


class RegressionTests(unittest.TestCase):
    def inspect(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'fixture.jsonl'
            transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            since = stats.stamp('2026-10-02T00:00:00Z')
            report, bad = stats.inspect(transcript, since, since + 60)
            self.assertEqual(bad, 0)
            return report

    def test_background_read_attributes_land_without_reader_exit_code(self):
        report = self.inspect([
            call(1, 'land', 'land worktree'),
            result(2, 'land', 'Read /tmp/fixture-missing.output',
                   toolUseResult={'backgroundTaskId': 'job'}),
            call(3, 'poll', 'tail /tmp/fixture-missing.output'),
            result(4, 'poll', 'still running'),
            call(5, 'read', 'cat /tmp/fixture-missing.output'),
            result(6, 'read', 'LANDED worktree sha\nProcess exited with code 0'),
            row(8, 'user', '<task-notification><task-id>job</task-id>'
                '<status>completed</status></task-notification>'),
        ])
        self.assertEqual(report['land']['passed'], 1)
        self.assertEqual(report['land']['seconds'], 5)

    def test_notification_and_task_output_do_not_erase_failure(self):
        report = self.inspect([
            call(1, 'check', 'pnpm check:remote --summary'),
            result(2, 'check', '', toolUseResult={'backgroundTaskId': 'job'}),
            call(3, 'read', '', name='TaskOutput', task_id='job'),
            result(4, 'read', '== finished rc=1 2026-10-02'),
            row(5, 'user', '<task-notification><task-id>job</task-id>'
                '<status>completed</status><summary>exit code 0</summary></task-notification>'),
        ])
        self.assertEqual(report['checks']['failed'], 1)
        self.assertEqual(report['checks']['seconds'], 3)

    def test_output_file_without_transcript_read_has_outcome_without_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'job.output'
            output.write_text('Tests  10 passed (10)\n== finished rc=0 2026-10-02\n')
            report = self.inspect([
                call(1, 'check', 'remote-ci check --summary'),
                result(2, 'check', f'Read {output}', toolUseResult={'backgroundTaskId': 'job'}),
            ])
        self.assertEqual(report['checks']['passed'], 1)
        self.assertEqual(report['checks']['timed'], 0)

    def test_notification_embedded_output_and_tool_id(self):
        report = self.inspect([
            call(1, 'land', 'land worktree'),
            result(2, 'land', '', toolUseResult={'backgroundTaskId': 'job'}),
            row(3, 'user', '<task-notification><tool-use-id>land</tool-use-id>'
                '<status>completed</status>LANDED worktree sha\n</task-notification>'),
        ])
        self.assertEqual(report['land']['passed'], 1)
        report = self.inspect([
            call(1, 'land', 'land worktree'),
            result(2, 'land', '', toolUseResult={'backgroundTaskId': 'job'}),
            row(3, 'user', '<task-notification><tool-use-id>land</tool-use-id>'
                '<status>completed</status>\nLANDED worktree sha\n</task-notification>'),
        ])
        self.assertEqual(report['land']['passed'], 1)

    def test_ci_summary_requires_whole_check_evidence(self):
        self.assertEqual(stats.outcome('Tests  704 passed (704)', {}), 'unknown')
        self.assertEqual(stats.outcome('Tests  704 passed (704)\n== finished rc=1 now', {}), 'failed')
        self.assertEqual(stats.outcome('\x1b[32m== finished rc=0 now\x1b[0m', {}), 'passed')
        self.assertEqual(stats.outcome('remote-ci: this exact code already passed on the runner (route); skipping.', {}), 'passed')

    def test_script_inspections_are_not_runs(self):
        for command in ["cat <<'EOF' > script\nland foo\nremote-ci check\nEOF",
                        "rg 'pnpm check:remote|land foo' script", 'remote-ci status',
                        'sh -n /tmp/land.sh', 'remote-ci check --help']:
            self.assertEqual(stats.command_kinds(command), [])
        self.assertEqual(stats.command_kinds('cd /tmp && pnpm check:remote --summary'), ['checks'])
        self.assertEqual(stats.command_kinds('bash /tmp/land.sh foo'), ['land'])

    def test_images_context_deduplication_and_safe_source_labels(self):
        assistant = call(1, 'read', 'SECRET=value cat /tmp/file')
        assistant['message']['usage'] = {'cache_read_input_tokens': 100}
        update = row(2, 'assistant', [])
        update['message']['id'] = assistant['message']['id']
        update['message']['usage'] = {'cache_read_input_tokens': 120}
        report = self.inspect([assistant, update, result(3, 'read', [
            {'type': 'text', 'text': 'abc'}, {'type': 'image', 'source': {'data': 'payload'}}])])
        self.assertEqual(report['images_read'], 1)
        self.assertEqual(report['context_reread_tokens'], 120)
        self.assertEqual(report['average_context_tokens'], 120)
        self.assertEqual(sum(report['tool_result_characters'].values()), 3)
        self.assertNotIn('SECRET', stats.render(report, 'fixture'))
        totals = stats.total_of([report, report])
        self.assertEqual(totals['images_read'], 2)
        self.assertEqual(totals['average_context_tokens'], 120)

    def test_workers_pid_reuse_missing_log_and_sibling_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude/state').mkdir(parents=True)
            project, worktree = home / 'project', home / 'worktree'
            completed = home / 'done.log'
            completed.write_text('tokens used\n100\n')
            registry = home / '.claude/state/workers.tsv'
            registry.write_text(''.join(f'10\t{pid}\t{log}\t{cwd}\n' for pid, log, cwd in [
                (1, completed, project), (2, home / 'missing', project),
                (3, home / 'missing', worktree), (4, home / 'missing', project)]))

            def process(args, **kwargs):
                if args[0] == 'git':
                    return stats.subprocess.CompletedProcess(args, 0, str(home / 'common.git') + '\n', '')
                command = 'codex exec' if args[2] == '3' else 'unrelated process'
                return stats.subprocess.CompletedProcess(args, 0, command, '')

            def alive(pid, signal):
                if pid == 4:
                    raise ProcessLookupError

            with patch.object(stats.Path, 'home', return_value=home), \
                    patch.object(stats.subprocess, 'run', side_effect=process), \
                    patch.object(stats.os, 'kill', side_effect=alive):
                self.assertEqual(stats.workers(project, 0, 20),
                                 dict(done=1, died=2, running=1, unknown=0))


    def test_worker_records_override_transcripts_and_alive_pids(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / '.claude/state'
            state.mkdir(parents=True)
            project = (home / 'project').resolve()
            rows = []
            for index, (rc, final) in enumerate(((0, 'report'), (7, 'partial'), (0, '')), 1):
                log = home / f'{index}.log'
                log.write_text('tokens used\n100\nlegacy report\n')
                Path(str(log) + '.run').write_text('run_id=fixture\n')
                Path(str(log) + '.last').write_text(final)
                Path(str(log) + '.exit').write_text(f'rc={rc} ended=12\n')
                rows.append(f'10\t{index}\t{log}\t{project}\n')
            missing = home / 'missing.log'
            missing.write_text('\x1b[1mtokens used\x1b[0m\n100\n')
            Path(str(missing) + '.run').write_text('run_id=hard-kill\n')
            rows.append(f'10\t4\t{missing}\t{project}\n')
            (state / 'workers.tsv').write_text(''.join(rows))
            with patch.object(stats.Path, 'home', return_value=home), \
                    patch.object(stats.os, 'kill', side_effect=ProcessLookupError):
                self.assertEqual(stats.workers(project, 0, 20), dict(done=1, died=3, running=0, unknown=0))


    def test_record_workers_with_hidden_ps_use_live_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            state = home / '.claude/state'
            state.mkdir(parents=True)
            project = home / 'project'
            rows = []
            for pid in range(1, 6):
                log = home / f'{pid}.log'
                log.write_text('still working\n')
                if pid != 4:
                    Path(str(log) + '.run').write_text('run_id=fixture\n')
                rows.append(f'10\t{pid}\t{log}\t{project}\n')
            (state / 'workers.tsv').write_text(''.join(rows))

            def process(args, **kwargs):
                if args[0] == 'git':
                    return stats.subprocess.CompletedProcess(args, 1, '', '')
                if args[2] == '3':
                    return stats.subprocess.CompletedProcess(args, 0, 'unrelated process', '')
                return stats.subprocess.CompletedProcess(args, 0 if args[2] == '5' else 1, '', '')

            def alive(pid, _signal):
                if pid == 1:
                    raise ProcessLookupError

            with patch.object(stats.Path, 'home', return_value=home), \
                    patch.object(stats.subprocess, 'run', side_effect=process), \
                    patch.object(stats.os, 'kill', side_effect=alive):
                self.assertEqual(stats.workers(project, 0, 20), dict(done=0, died=3, running=2, unknown=0))


if __name__ == '__main__':
    unittest.main()
