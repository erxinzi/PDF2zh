import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from tools.package_release import SERVER, package_release
from utils.plugin_install import plugin_path


class ReleasePackageTests(unittest.TestCase):
    def test_both_packages_include_our_xpi_and_exclude_credentials_cache_and_kernel(self):
        with tempfile.TemporaryDirectory(dir=SERVER / 'cache/tmp') as directory:
            root = Path(directory) / 'app'; root.mkdir()
            (root / '_internal/plugins').mkdir(parents=True)
            (root / '_internal/plugins/pdf-2-zh-for-zotero.xpi').write_bytes(plugin_path().read_bytes())
            for name in ('zotero-pdf2zh-server.exe', 'README-EXE.md', 'PERFORMANCE.md', 'LICENSE'):
                (root / name).write_text('release fixture', encoding='utf-8')
            (root / 'core/python/test-python').mkdir(parents=True)
            (root / 'core/python/test-python/python.exe').write_text('bundle fixture', encoding='utf-8')
            for relative in ('settings.json', 'config/llm_apis.json', 'cache/task-config/key.json', 'translated/private.pdf', 'core/zotero-pdf2zh-next-venv/secret.txt'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture-secret-must-never-ship', encoding='utf-8')
            result = package_release(root, Path(directory) / 'output')
            self.assertEqual(len(result['packages']), 2)
            for package in result['packages']:
                with zipfile.ZipFile(package['path']) as archive:
                    names = archive.namelist()
                    self.assertIn('zotero-pdf2zh-server/_internal/plugins/pdf-2-zh-for-zotero.xpi', names)
                    self.assertFalse(any(name.endswith('settings.json') or '/config/llm_apis.json' in name or '/translated/' in name or '/cache/' in name or '/zotero-pdf2zh-next-venv/' in name for name in names))
                    if '-update.zip' in package['path']:
                        self.assertFalse(any('/core/' in name for name in names))
                    manifest = json.loads(archive.read('zotero-pdf2zh-server/release-manifest.json'))
                    expected=json.loads((SERVER.parent/'Plugin/package.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['plugin_version'], expected['version'])

    def test_missing_plugin_stops_packaging_instead_of_silently_shipping_without_it(self):
        with tempfile.TemporaryDirectory(dir=SERVER / 'cache/tmp') as directory:
            with self.assertRaises(FileNotFoundError):
                package_release(Path(directory), Path(directory) / 'output')
