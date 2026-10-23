import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bootstrap_desktop import SetupError, ensure_venv, prepare_desktop


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jiehua-bootstrap-')
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def python_path(self, root):
        return root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

    def create_environment(self):
        environment = self.project / '.venv'
        subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True, capture_output=True)
        return environment

    def prepare(self):
        return Path(ensure_venv(self.project, base_python=sys.executable, emit=lambda *_: None))

    def assert_healthy_pip(self, interpreter):
        result = subprocess.run([str(interpreter), '-m', 'pip', '--version'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pip ', result.stdout)

    def test_damaged_rich_module_rebuilt_without_losing_existing_files(self):
        environment = self.create_environment()
        candidates = list(environment.rglob('pip/_vendor/rich/_extension.py'))
        self.assertEqual(len(candidates), 1, 'bundled pip layout must contain the reported missing module')
        candidates[0].rename(candidates[0].with_suffix('.test-backup'))
        marker = environment / 'KEEP_ME.txt'
        marker.write_text('preserve existing user files', encoding='utf-8')
        settings = self.project / '.env'
        settings.write_text('LOCAL_DUMMY_SETTING=retain-this\n', encoding='utf-8')
        broken = subprocess.run([str(self.python_path(environment)), '-m', 'pip', '--version'], capture_output=True, text=True)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn('pip._vendor.rich._extension', broken.stderr)

        interpreter = self.prepare()

        self.assertEqual(interpreter, self.python_path(environment))
        self.assert_healthy_pip(interpreter)
        backups = list(self.project.glob('.venv.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'KEEP_ME.txt').read_text(encoding='utf-8'), 'preserve existing user files')
        self.assertTrue(list(backups[0].rglob('_extension.test-backup')))
        self.assertEqual(settings.read_text(encoding='utf-8'), 'LOCAL_DUMMY_SETTING=retain-this\n')

    def test_healthy_environment_is_reused_without_backup(self):
        first = self.prepare()
        marker = self.project / '.venv' / 'KEEP_ME.txt'
        marker.write_text('unchanged', encoding='utf-8')

        second = self.prepare()

        self.assertEqual(first, second)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'unchanged')
        self.assertEqual(list(self.project.glob('.venv.backup-*')), [])
        self.assert_healthy_pip(second)

    def test_incomplete_environment_directory_is_preserved_before_rebuild(self):
        environment = self.project / '.venv'
        environment.mkdir()
        (environment / 'KEEP_ME.txt').write_text('partial setup contents', encoding='utf-8')

        interpreter = self.prepare()

        self.assertEqual(interpreter, self.python_path(environment))
        self.assert_healthy_pip(interpreter)
        backups = list(self.project.glob('.venv.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'KEEP_ME.txt').read_text(encoding='utf-8'), 'partial setup contents')

    def test_symlink_environment_is_rejected_without_touching_target(self):
        target = Path(self.temp.name) / 'external-environment'
        target.mkdir()
        marker = target / 'KEEP_ME.txt'
        marker.write_text('external files', encoding='utf-8')
        environment = self.project / '.venv'
        try:
            environment.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlinks unavailable for this account')

        with self.assertRaises(SetupError):
            self.prepare()

        self.assertTrue(environment.is_symlink())
        self.assertEqual(marker.read_text(encoding='utf-8'), 'external files')
        self.assertEqual(list(self.project.glob('.venv.backup-*')), [])

    def test_unhealthy_new_environment_stops_after_one_creation_attempt(self):
        environment = self.project / '.venv'
        def create_partial(command, **kwargs):
            environment.mkdir()
            return subprocess.CompletedProcess(command, 0)
        with patch('bootstrap_desktop.pip_healthy', return_value=False), patch('bootstrap_desktop.subprocess.run', side_effect=create_partial) as run:
            with self.assertRaises(SetupError) as raised:
                self.prepare()
        self.assertIn('新建环境', str(raised.exception))
        self.assertIn('修复', str(raised.exception))
        self.assertEqual(run.call_count, 1, 'a bad fresh environment must not trigger a rebuild loop')
        self.assertTrue(environment.is_dir())
        self.assertEqual(list(self.project.glob('.venv.backup-*')), [])

    def test_dependency_network_failure_does_not_rebuild_environment(self):
        interpreter = self.python_path(self.project / '.venv')
        failure = subprocess.CalledProcessError(1, ['pip', 'install'])
        with patch('bootstrap_desktop.ensure_venv', return_value=interpreter) as ensure, patch('bootstrap_desktop.dependencies_ready', return_value=False), patch('bootstrap_desktop.subprocess.run', side_effect=failure) as install:
            with self.assertRaises(SetupError) as raised:
                prepare_desktop(self.project, base_python=sys.executable, emit=lambda *_: None)
        self.assertIn('依赖安装失败', str(raised.exception))
        ensure.assert_called_once()
        install.assert_called_once()
        self.assertEqual(list(self.project.glob('.venv.backup-*')), [])
