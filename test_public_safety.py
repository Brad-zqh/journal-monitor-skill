"""Exercise default behavior and publication checks without credentials or network."""

import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import check_journals as monitor

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('publication_audit', ROOT / 'skills/journal-monitor/scripts/audit_repo.py')
publication_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication_audit)


class PublicationAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        for name in (*publication_audit.REQUIRED_FILES, '.gitignore'):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True, capture_output=True)

    def test_template_passes_publication_audit(self):
        errors, warnings, _ = publication_audit.audit(self.repo)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_tracked_cache_is_rejected(self):
        cache = self.repo / '.journal-monitor' / 'article_status_cache.json'
        cache.parent.mkdir()
        cache.write_text('{}', encoding='utf-8')
        subprocess.run(['git', 'add', '-f', str(cache)], cwd=self.repo, check=True, capture_output=True)
        errors, _, _ = publication_audit.audit(self.repo)
        self.assertTrue(any('sensitive file names are tracked' in error for error in errors))

    def test_public_literal_credential_is_rejected(self):
        path = self.repo / 'journals_config.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        config['journals'][0]['password'] = 'synthetic-test-value'
        path.write_text(json.dumps(config), encoding='utf-8')
        errors, _, _ = publication_audit.audit(self.repo)
        self.assertTrue(any('literal credential' in error for error in errors))


class RuntimeSafetyTests(unittest.TestCase):
    def test_no_enabled_journals_performs_no_network_or_notifications(self):
        with patch.object(monitor, 'CONFIG', {'journals': []}), \
             patch.object(monitor, 'fetch_all', new_callable=AsyncMock) as fetch, \
             patch.object(monitor, 'notify') as notify:
            asyncio.run(monitor.main())
        fetch.assert_not_awaited()
        notify.assert_not_called()

    def test_runtime_creates_private_cache_parent(self):
        article = {'journal': 'TEST', 'id': 'TEST-D-30-00001', 'status': 'With Editor', 'title': 'Synthetic title'}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'private-runtime' / 'article_status_cache.json'
            config = {'journals': [{'name': 'TEST', 'enabled': True}]}
            with patch.object(monitor, 'CONFIG', config), \
                 patch.object(monitor, 'CACHE_FILE', cache), \
                 patch.object(monitor, 'fetch_all', new=AsyncMock(return_value=([article], {}))), \
                 patch.object(monitor, 'notify', new=Mock()):
                asyncio.run(monitor.main())
            self.assertEqual(json.loads(cache.read_text(encoding='utf-8'))['TEST::TEST-D-30-00001']['status'], 'With Editor')


if __name__ == '__main__':
    unittest.main()
