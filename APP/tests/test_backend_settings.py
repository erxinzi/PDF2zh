import base64
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils.backend_settings import BACKEND_PROTOCOL, PLUGIN_PARAMETERS, BackendSettingsError, resolve_plugin_settings
from utils.desktop_translation import DEFAULTS
from utils.llm_api import ApiRegistry


class BackendSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = {'translation': {**DEFAULTS, 'engine': 'pdf2zh_next', 'next_service': 'deepseek',
                                         'sourceLang': 'en', 'targetLang': 'zh-CN', 'qps': 8, 'poolSize': 24}}
        self.settings_file = self.root / 'settings.json'
        self.settings_file.write_text(json.dumps(self.settings), encoding='utf-8')
        self.registry = ApiRegistry(self.root / 'config/llm_apis.json')
        self.registry.upsert({'key': 'backend-account', 'name': 'Backend API', 'service': 'deepseek',
                              'apiKey': 'backend-fixture-key', 'model': 'deepseek-v4-pro',
                              'extraData': {'deepseek_thinking_mode': 'enabled', 'deepseek_reasoning_effort': 'max'}})
        self.registry.select('pdf2zh_next', 'backend-account')

    def tearDown(self):
        self.temp.cleanup()

    def test_backend_owns_interfaces_language_engine_and_concurrency(self):
        body = {'translationConfigSource': BACKEND_PROTOCOL, 'engine': 'pdf2zh', 'service': 'wrong',
                'next_service': 'wrong', 'sourceLang': 'ja', 'targetLang': 'fr', 'qps': 999, 'poolSize': 999,
                'llm_api': {'apiKey': 'plugin-key', 'model': 'plugin-model'},
                'skipLastPages': '2', 'fontFamily': 'serif', 'transFirst': 'true',
                'mono': 'false', 'dual': 'true', 'disableGlossary': 'false'}
        before = copy.deepcopy(body)
        resolved = resolve_plugin_settings(body, self.root)
        self.assertEqual(resolved['engine'], 'pdf2zh_next')
        self.assertEqual((resolved['sourceLang'], resolved['targetLang'], resolved['qps'], resolved['poolSize']), ('en', 'zh-CN', 8, 24))
        self.assertEqual(resolved['next_service'], 'deepseek')
        self.assertEqual(resolved['llm_api']['apiKey'], 'backend-fixture-key')
        self.assertEqual(resolved['llm_api']['model'], 'deepseek-v4-pro')
        self.assertEqual(resolved['llm_api']['extraData']['deepseek_reasoning_effort'], 'max')
        for key in ('skipLastPages', 'fontFamily', 'transFirst', 'mono', 'dual', 'disableGlossary'):
            self.assertEqual(resolved[key], body[key])
        self.assertEqual(body, before)

    def test_each_request_uses_latest_saved_backend_settings(self):
        first = resolve_plugin_settings({}, self.root)
        self.settings['translation']['targetLang'] = 'de'
        self.settings_file.write_text(json.dumps(self.settings), encoding='utf-8')
        entry = {**self.registry.get('backend-account'), 'model': 'deepseek-v4-flash'}
        self.registry.upsert(entry)
        second = resolve_plugin_settings({}, self.root)
        self.assertEqual(first['targetLang'], 'zh-CN')
        self.assertEqual(second['targetLang'], 'de')
        self.assertEqual(second['llm_api']['model'], 'deepseek-v4-flash')

    def test_unselected_bilingual_output_is_not_rendered_for_mono_only_plugin_request(self):
        resolved = resolve_plugin_settings({'mono': 'true', 'dual': 'false'}, self.root)
        self.assertIs(resolved['noDual'], True)
        self.assertFalse(resolved['noMono'])
        self.assertEqual(resolved['llm_api']['extraData']['deepseek_reasoning_effort'], 'max')

    def test_missing_or_disabled_supplier_is_an_error_without_fallback(self):
        entry = {**self.registry.get('backend-account'), 'activate': False}
        self.registry.upsert(entry)
        with self.assertRaises(BackendSettingsError):
            resolve_plugin_settings({'llm_api': {'apiKey': 'client-key'}}, self.root)

    def test_actual_plugin_request_uses_backend_account_even_for_legacy_plugin_header(self):
        import server
        translator = server.PDFTranslator(SimpleNamespace(enable_venv=False))
        data = {'fileName': 'paper.pdf', 'fileContent': base64.b64encode(b'%PDF-1.7 fixture').decode(),
                'fontFamily': 'serif', 'llm_api': {'apiKey': 'client-key'}}
        for headers, flag in (({'X-PDF2zh-Plugin-Version': '4.1.7'}, None), ({}, BACKEND_PROTOCOL)):
            body = {**data, **({'translationConfigSource': flag} if flag else {})}
            with patch.object(server, 'root_path', str(self.root)):
                with translator.app.test_request_context('/translate', json=body, headers=headers):
                    path, config = translator.process_request('request-job')
            self.assertTrue(Path(path).is_file())
            self.assertEqual(config.service, 'deepseek')
            self.assertEqual(config.llm_api['apiKey'], 'backend-fixture-key')
            self.assertEqual(config.font_family, 'serif')
            self.assertEqual(config.qps, 8)

    def test_configuration_error_is_409_and_health_advertises_protocol_without_credentials(self):
        import server
        translator = server.PDFTranslator(SimpleNamespace(enable_venv=False))
        self.registry.delete('backend-account')
        client = translator.app.test_client()
        with patch.object(server, 'root_path', str(self.root)):
            response = client.post('/translate', json={'translationConfigSource': BACKEND_PROTOCOL,
                                                        'fileName': 'paper.pdf', 'fileContent': ''})
        self.assertEqual(response.status_code, 409)
        self.assertIn('后端翻译设置', response.json['message'])
        health = client.get('/health').json
        self.assertIs(health['capabilities']['backendTranslationConfig'], True)
        self.assertNotIn('backend-fixture-key', json.dumps(health))

    def test_python_and_plugin_contract_parameter_names_match(self):
        source = (Path(__file__).resolve().parents[2] / 'Plugin/src/modules/pdf2zhProtocol.ts').read_text(encoding='utf-8')
        import re
        declaration = source.split('PLUGIN_TRANSLATION_PARAMETERS = [', 1)[1].split('] as const', 1)[0]
        self.assertEqual(set(re.findall(r'"([A-Za-z_]+)"', declaration)), PLUGIN_PARAMETERS)

    def test_concurrent_first_requests_share_one_registry_identity(self):
        path = self.root / 'new-config/llm_apis.json'
        results = []
        barrier = threading.Barrier(2)
        def load():
            barrier.wait()
            registry = ApiRegistry(path).load_or_migrate({}, {})
            results.append(registry.selected_entry('pdf2zh_next')['key'])
        threads = [threading.Thread(target=load) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
