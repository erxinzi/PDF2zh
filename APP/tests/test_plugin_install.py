import ctypes
import os
import tempfile
import unittest
import zipfile
from ctypes import wintypes
from pathlib import Path

from utils.plugin_install import plugin_path


class PluginInstallTests(unittest.TestCase):
    def test_bundled_plugin_is_our_compiled_backend_settings_version(self):
        import json
        path = plugin_path()
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['applications']['zotero']['id'], 'pdf2zh@guaguastandup.com')
            self.assertEqual(manifest['homepage_url'], 'https://github.com/erxinzi/PDF2zh')
            self.assertNotIn('content/llmApiEditor.xhtml', archive.namelist())

    @unittest.skipUnless(os.name == 'nt', 'Windows Shell OLE file data')
    def test_drag_object_contains_real_unicode_xpi_file_in_standard_file_drop_format(self):
        from utils.windows_drag import ShellFileData, _method
        class Format(ctypes.Structure):
            _fields_ = [('format', wintypes.WORD), ('target', ctypes.c_void_p), ('aspect', wintypes.DWORD), ('index', wintypes.LONG), ('tymed', wintypes.DWORD)]
        class Medium(ctypes.Structure):
            _fields_ = [('tymed', wintypes.DWORD), ('data', ctypes.c_void_p), ('release', ctypes.c_void_p)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Zotero 插件安装.xpi'
            path.write_bytes(plugin_path().read_bytes())
            with ShellFileData(path) as data:
                format = Format(15, None, 1, -1, 1)  # CF_HDROP, DVASPECT_CONTENT, TYMED_HGLOBAL
                medium = Medium()
                result = _method(data.pointer, 3, ctypes.c_long, ctypes.POINTER(Format), ctypes.POINTER(Medium))(data.pointer, ctypes.byref(format), ctypes.byref(medium))
                self.assertEqual(result, 0)
                try:
                    shell = ctypes.WinDLL('shell32')
                    shell.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
                    shell.DragQueryFileW.restype = wintypes.UINT
                    self.assertEqual(shell.DragQueryFileW(medium.data, 0xffffffff, None, 0), 1)
                    buffer = ctypes.create_unicode_buffer(32768)
                    self.assertGreater(shell.DragQueryFileW(medium.data, 0, buffer, len(buffer)), 0)
                    self.assertEqual(Path(buffer.value), path.resolve())
                finally:
                    release = ctypes.OleDLL('ole32').ReleaseStgMedium
                    release.argtypes = [ctypes.POINTER(Medium)]
                    release.restype = None
                    release(ctypes.byref(medium))

    @unittest.skipUnless(os.name == 'nt', 'Windows OLE drag lifecycle')
    def test_drag_callbacks_offer_copy_and_support_release_and_escape(self):
        from utils.windows_drag import DropSource
        source = DropSource()
        self.assertEqual(source.vtable.QueryContinueDrag(None, False, 1), 0)
        self.assertEqual(source.vtable.QueryContinueDrag(None, False, 0), 0x40100)
        self.assertEqual(source.vtable.QueryContinueDrag(None, True, 1), 0x40101)
        self.assertEqual(source.vtable.GiveFeedback(None, 1), 0x40102)
