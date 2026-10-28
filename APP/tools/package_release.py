"""Package only release binaries, the compiled plugin and bundled Python."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from utils.version import VERSION


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def package_release(root, output, validation=None):
    root, output = Path(root).resolve(strict=True), Path(output).resolve()
    release_directory = SERVER.parent / 'Release'
    if not root.is_relative_to(SERVER) or not (output.is_relative_to(SERVER) or output.is_relative_to(release_directory)):
        raise ValueError('Release paths must stay within the server workspace.')
    exe = root / 'zotero-pdf2zh-server.exe'
    plugin = root / '_internal/plugins/pdf-2-zh-for-zotero.xpi'
    with zipfile.ZipFile(plugin) as archive:
        if archive.testzip() is not None:
            raise ValueError('Bundled plugin ZIP verification failed.')
        plugin_manifest = json.loads(archive.read('manifest.json'))
    expected = json.loads((SERVER.parent / 'Plugin/package.json').read_text(encoding='utf-8'))
    if (plugin_manifest['version'] != expected['version'] or
            plugin_manifest['applications']['zotero']['id'] != expected['config']['addonID']):
        raise ValueError('Rebuild the Zotero plugin: its identity/version does not match the project.')
    test_path = root / 'logs/self-test-result.json'
    self_test = json.loads(test_path.read_text(encoding='utf-8')) if test_path.is_file() else None
    if self_test and self_test.get('version') != VERSION:
        raise ValueError('Self-test belongs to another version.')
    if validation and validation.get('passed') is not True:
        raise ValueError('Release validation failed.')
    self_test_summary = ({key: self_test[key] for key in ('version', 'window_title', 'plugin_bundled') if key in self_test}
                         if self_test else None)
    validation_summary = ({key: validation[key] for key in ('passed', 'backend_tests', 'plugin_tests') if key in validation}
                          if validation else None)
    manifest = {'version': VERSION, 'exe_sha256': digest(exe), 'plugin_version': plugin_manifest['version'],
                'plugin_sha256': digest(plugin), 'plugin_path': '_internal/plugins/pdf-2-zh-for-zotero.xpi',
                'packaged_self_test': self_test_summary, 'validation': validation_summary, 'credentials_in_package': False,
                'quality_parameters_preserved': True, 'cuda_isolation': 'Existing project core is preserved.'}
    (root / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    files = [root / name for name in ('zotero-pdf2zh-server.exe', 'README-EXE.md', 'PERFORMANCE.md', 'LICENSE', 'release-manifest.json')]
    files += sorted(path for path in (root / '_internal').rglob('*') if path.is_file())
    python_files = sorted(path for path in (root / 'core/python').rglob('*') if path.is_file())
    if not any((root / 'core/python').glob('*/python.exe')):
        raise ValueError('Full package requires its project-local Python interpreter.')
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    for suffix, members in (('', files + python_files), ('-update', files)):
        destination = output / f'PDF2zh-v{VERSION}-Windows-x64{suffix}.zip'
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for member in members:
                relative = member.relative_to(root)
                if (not member.resolve(strict=True).is_relative_to(root) or
                    relative.parts[0] in {'config', 'cache', 'logs', 'translated'} or
                    member.name in {'settings.json', 'llm_apis.json'}):
                    raise ValueError('Refusing to package external files or user data.')
                archive.write(member, 'zotero-pdf2zh-server/' + relative.as_posix())
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise ValueError('Release ZIP verification failed.')
            for file, checksum in ((exe, manifest['exe_sha256']), (plugin, manifest['plugin_sha256'])):
                if hashlib.sha256(archive.read('zotero-pdf2zh-server/' + file.relative_to(root).as_posix())).hexdigest() != checksum:
                    raise ValueError('Packaged EXE/plugin checksum mismatch.')
        packages.append({'path': str(destination), 'bytes': destination.stat().st_size, 'sha256': digest(destination)})
    return {'version': VERSION, 'plugin_version': manifest['plugin_version'], 'exe_sha256': manifest['exe_sha256'], 'packages': packages}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-root', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path, default=SERVER.parent / 'Release')
    parser.add_argument('--validation', type=Path)
    args = parser.parse_args()
    validation = json.loads(args.validation.read_text(encoding='utf-8-sig')) if args.validation else None
    print(json.dumps(package_release(args.release_root, args.output_dir, validation), ensure_ascii=False, indent=2))
