import copy
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from utils.llm_api import ApiRegistry, ModelDiscoveryError, configured, discover_models, extract_model_ids, load_catalog, model_request_spec


def entry(**values):
    return {'key': 'account-one', 'name': 'My API', 'service': 'deepseek', 'apiKey': 'test-secret',
            'apiUrl': 'http://127.0.0.1:1/v1', 'model': 'keep-this-model',
            'extraData': {'deepseek_thinking_mode': 'enabled', 'deepseek_reasoning_effort': 'high'}, **values}


class ApiRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config/llm_apis.json'
        self.registry = ApiRegistry(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_registry_is_self_contained_and_does_not_list_all_templates(self):
        self.registry.load_or_migrate({}, {})
        self.assertTrue(self.path.is_file())
        self.assertEqual(len(self.registry.choices('pdf2zh_next')), 1)
        self.assertEqual(self.registry.choices('pdf2zh_next')[0]['service'], 'siliconflowfree')
        self.assertGreater(len(self.registry.catalog['providers']), 20)
        self.assertGreater(len(self.registry.catalog['languages']), 100)

    def test_only_enabled_configured_compatible_entries_can_be_selected(self):
        self.registry.upsert(entry())
        self.registry.upsert(entry(key='disabled', activate=False))
        self.registry.upsert(entry(key='incomplete', apiKey=''), require_complete=False)
        self.registry.upsert(entry(key='legacy', engines=['pdf2zh']))
        self.assertEqual([value['key'] for value in self.registry.choices('pdf2zh_next')], ['account-one'])
        for key in ('disabled', 'incomplete', 'legacy', 'not-saved'):
            with self.assertRaises(ValueError):
                self.registry.select('pdf2zh_next', key)
        self.registry.select('pdf2zh_next', 'account-one')
        loaded = ApiRegistry(self.path).load_or_migrate({}, {})
        self.assertEqual(loaded.selected_entry('pdf2zh_next')['model'], 'keep-this-model')

    def test_migration_keeps_saved_models_keys_and_extra_data_and_skips_empty_templates(self):
        saved = entry()
        profiles = {'pdf2zh_next:deepseek': saved, 'pdf2zh_next:openai': {'apiKey': 'null', 'model': 'null'}}
        settings = {'translation': {'next_service': 'deepseek'}}
        before = copy.deepcopy(profiles)
        self.registry.load_or_migrate(settings, profiles)
        selected = self.registry.selected_entry('pdf2zh_next')
        for key in ('model', 'apiKey', 'apiUrl', 'extraData'):
            self.assertEqual(selected[key], saved[key])
        self.assertNotIn('openai', [item['service'] for item in self.registry.entries])
        self.assertEqual(profiles, before)

    def test_empty_legacy_desktop_fields_keep_credentials_previously_inherited_from_config(self):
        settings = {'translation': {'next_service': 'deepseek'}, 'translation_apis': {
            'pdf2zh_next:deepseek': {'apiKey': '', 'apiUrl': '', 'model': '', 'extraData': {'deepseek_thinking_mode': 'disabled'}}}}
        self.registry.load_or_migrate(settings, {'pdf2zh_next:deepseek': entry()})
        selected = self.registry.selected_entry('pdf2zh_next')
        self.assertEqual(selected['apiKey'], 'test-secret')
        self.assertEqual(selected['model'], 'keep-this-model')
        self.assertEqual(selected['extraData']['deepseek_thinking_mode'], 'disabled')

    def test_deleting_selected_entry_does_not_silently_switch_accounts_or_restore_on_restart(self):
        self.registry.upsert(entry()); self.registry.upsert(entry(key='other-account'))
        self.registry.select('pdf2zh_next', 'account-one')
        self.registry.delete('account-one')
        self.assertIsNone(self.registry.selected_entry('pdf2zh_next'))
        loaded = ApiRegistry(self.path).load_or_migrate({}, {'pdf2zh_next:deepseek': entry()})
        self.assertIsNone(loaded.get('account-one'))
        self.assertIsNone(loaded.selected_entry('pdf2zh_next'))

    def test_model_discovery_cache_preserves_current_model_and_quality_parameters(self):
        original = self.registry.upsert(entry())
        updated = {**original, 'models': ['discovered-b', 'discovered-a']}
        self.registry.upsert(updated)
        saved = self.registry.get(original['key'])
        self.assertEqual(saved['model'], original['model'])
        self.assertEqual(saved['extraData'], original['extraData'])
        self.assertIn('keep-this-model', self.registry.model_choices(saved))
        self.assertIn('discovered-b', self.registry.model_choices(saved))

    def test_json_import_supports_multiple_accounts_and_rejects_bad_input_atomically(self):
        self.assertEqual(self.registry.import_data({'llmApis': json.dumps([entry(), entry(key='another')])}), 2)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.registry.import_data([entry(key='valid'), {'service': 'invalid'}])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.registry.entries), 2)
        with self.assertRaises(ValueError):
            self.registry.upsert(entry(extraData=[]))

    def test_write_failure_rolls_back_in_memory_selection_and_credentials(self):
        self.registry.upsert(entry())
        before = copy.deepcopy(self.registry.entries)
        with patch.object(self.registry, 'save', side_effect=OSError('read-only fixture')):
            with self.assertRaises(OSError):
                self.registry.upsert(entry(apiKey='replacement'))
        self.assertEqual(self.registry.entries, before)

    def test_invalid_registry_is_not_overwritten_by_migration(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.registry.load_or_migrate({}, {})
        self.assertEqual(self.path.read_text(), '{broken')


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = lambda handler: (200, {}, {'data': [{'id': 'one'}, {'id': 'two'}, {'id': 'one'}]})
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                code, headers, payload = owner.response(self)
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(payload).encode('utf-8'))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def test_actual_get_uses_custom_endpoint_and_bearer_header(self):
        profile = entry(apiUrl=self.base + '/v1/chat/completions')
        before = copy.deepcopy(profile)
        self.assertEqual(discover_models(profile), ['one', 'two'])
        self.assertEqual(self.requests[0][0], '/v1/models')
        self.assertEqual(self.requests[0][1]['Authorization'], 'Bearer test-secret')
        self.assertEqual(profile, before)

    def test_gemini_pagination_uses_header_and_keeps_secret_out_of_url(self):
        def respond(handler):
            if 'pageToken=next' in handler.path:
                return 200, {}, {'models': [{'name': 'models/second'}]}
            return 200, {}, {'models': [{'name': 'models/first'}], 'nextPageToken': 'next'}
        self.response = respond
        self.assertEqual(discover_models(entry(service='gemini', apiUrl=self.base + '/v1beta')), ['first', 'second'])
        self.assertEqual(len(self.requests), 2)
        for path, headers in self.requests:
            self.assertNotIn('test-secret', path)
            self.assertEqual(next(value for key, value in headers.items() if key.lower() == 'x-goog-api-key'), 'test-secret')

    def test_ollama_and_xinference_formats(self):
        self.response = lambda _: (200, {}, {'models': [{'name': 'local-model:latest'}]})
        self.assertEqual(discover_models(entry(service='ollama', apiUrl=self.base, apiKey='')), ['local-model:latest'])
        self.assertEqual(self.requests[-1][0], '/api/tags')
        self.assertEqual(model_request_spec(entry(service='xinference', apiUrl=self.base, apiKey=''))[0], self.base + '/v1/models')
        self.assertEqual(extract_model_ids({'model-one': {}}), [])
        self.assertEqual(extract_model_ids({'uid-one': {'model_name': 'one'}, 'uid-two': {'model_name': 'two'}}), ['one', 'two'])

    def test_http_error_never_echoes_secret_response_body(self):
        self.response = lambda _: (401, {}, {'error': 'bad key: test-secret'})
        with self.assertRaises(ModelDiscoveryError) as raised:
            discover_models(entry(apiUrl=self.base))
        self.assertIn('401', str(raised.exception))
        self.assertNotIn('test-secret', str(raised.exception))

    def test_transport_failures_identify_the_underlying_cause_without_echoing_secrets(self):
        cases = [
            (socket.gaierror(11001, 'test-secret'), 'DNS', '11001'),
            (ConnectionRefusedError(10061, 'test-secret'), '连接被拒绝', '10061'),
            (PermissionError(13, 'test-secret'), '网络访问权限', '13'),
            (ssl.SSLError(1, 'test-secret'), 'TLS 握手', None),
            (TimeoutError('test-secret'), '超时', None),
        ]
        for reason, expected, code in cases:
            with self.subTest(reason=type(reason).__name__):
                with patch('utils.llm_api.build_opener') as build:
                    build.return_value.open.side_effect = URLError(reason)
                    with self.assertRaises(ModelDiscoveryError) as raised:
                        discover_models(entry(apiUrl=self.base))
                message = str(raised.exception)
                self.assertIn(expected, message)
                if code:
                    self.assertIn(code, message)
                self.assertNotIn('test-secret', message)

    def test_certificate_failures_keep_verification_enabled_and_report_the_code(self):
        reason = ssl.SSLCertVerificationError(1, 'test-secret')
        reason.verify_code = 20
        with patch('utils.llm_api.build_opener') as build:
            build.return_value.open.side_effect = URLError(reason)
            with self.assertRaises(ModelDiscoveryError) as raised:
                discover_models(entry(apiUrl=self.base))
        self.assertIn('TLS 证书验证失败', str(raised.exception))
        self.assertIn('20', str(raised.exception))
        self.assertNotIn('test-secret', str(raised.exception))
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_unclassified_network_reasons_do_not_echo_proxy_credentials(self):
        with patch('utils.llm_api.build_opener') as build:
            build.return_value.open.side_effect = URLError('proxy http://user:test-secret@proxy.invalid')
            with self.assertRaises(ModelDiscoveryError) as raised:
                discover_models(entry(apiUrl=self.base))
        self.assertNotIn('test-secret', str(raised.exception))
        self.assertNotIn('proxy.invalid', str(raised.exception))

    def test_cross_origin_redirect_does_not_forward_credentials(self):
        self.response = lambda _: (302, {'Location': f'http://localhost:{self.server.server_port}/capture'}, {})
        with self.assertRaises(ModelDiscoveryError):
            discover_models(entry(apiUrl=self.base))
        self.assertEqual(len(self.requests), 1)

    def test_empty_bad_json_and_timeout_produce_readable_failures(self):
        self.response = lambda _: (200, {}, {})
        with self.assertRaises(ModelDiscoveryError):
            discover_models(entry(apiUrl=self.base))
        def delayed(_):
            time.sleep(.2)
            return 200, {}, {'data': ['later']}
        self.response = delayed
        with self.assertRaises(ModelDiscoveryError):
            discover_models(entry(apiUrl=self.base), timeout=.04)

    def test_unsupported_provider_does_not_make_a_network_request(self):
        with self.assertRaises(ModelDiscoveryError):
            discover_models(entry(service='bing', apiUrl=self.base))
        self.assertEqual(self.requests, [])

    def test_azure_auth_and_both_models_endpoint_versions(self):
        url, headers = model_request_spec(entry(service='azure-openai', apiUrl=self.base))
        self.assertIn('/openai/models?api-version=2024-10-21', url)
        self.assertEqual(headers['api-key'], 'test-secret')
        self.assertNotIn('Authorization', headers)
        for suffix in ('/openai/v1', '/openai/v1/models'):
            url, _ = model_request_spec(entry(service='azure-openai', apiUrl=self.base + suffix))
            self.assertEqual(url, self.base + '/openai/v1/models')

    def test_normalization_accepts_plugin_payload_variations(self):
        for payload in (['one', 'two'], {'result': {'data': [{'model_name': 'one'}, {'model': 'two'}]}}, {'data': [{'id': 'one'}, {'name': 'models/two'}]}):
            self.assertEqual(extract_model_ids(payload), ['one', 'two'])


if __name__ == '__main__':
    unittest.main()
