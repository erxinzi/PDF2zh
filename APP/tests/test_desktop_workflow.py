import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils.desktop_translation import DEFAULTS, OUTPUTS, build_payload, load_options, read_profiles, concurrency_preset
from utils.task_control import TaskCancelled
from utils.task_manager import TaskManager
from utils.process_utils import stream_process
from utils.ui_jobs import task_rows


class DesktopOptionsTests(unittest.TestCase):
    def test_optimization_preserves_all_quality_and_rate_limit_parameters(self):
        for options in (DEFAULTS, {**DEFAULTS, 'poolSize': 100, 'disableGlossary': False, 'ocr': True, 'fontFamily': 'serif'}):
            for optimized in (True, False):
                after = concurrency_preset(options, optimized)
                changed = {key for key in options if after[key] != options[key]}
                self.assertLessEqual(changed, {'poolSize'})
                self.assertEqual(after['qps'], options['qps'])
                self.assertEqual(after['disableGlossary'], options['disableGlossary'])
    def test_defaults_match_every_plugin_translation_preference(self):
        text = (Path(__file__).resolve().parents[2] / 'Plugin/addon/prefs.js').read_text(encoding='utf-8')
        skip = {'new_serverip', 'sourceLangSelect', 'targetLangSelect', 'engineSelect', 'serviceSelect', 'next_serviceSelect', 'dialog-service', 'dialog-serviceSelect'}
        for key, raw in re.findall(r'pref\("__prefsPrefix__\.([^"\n]+)",\s*(.*?)\);', text):
            if key not in skip:
                self.assertEqual(DEFAULTS.get(key.replace('-', '_') if key in ('mono-cut', 'dual-cut', 'crop-compare') else key), json.loads(raw), key)

    def test_output_dependencies_avoid_unrequested_pdf_rendering(self):
        options = copy.deepcopy(DEFAULTS)
        options.update({key: False for key, _, _ in OUTPUTS})
        options['mono_cut'] = True
        payload = build_payload(options, {})
        self.assertTrue(payload['noDual'])
        self.assertFalse(payload['noMono'])
        options['noMono'] = True
        self.assertFalse(build_payload(options, {})['noMono'])
        options.update({'dual': True, 'noDual': True})
        self.assertFalse(build_payload(options, {})['noDual'])

    def test_desktop_profiles_are_copied_and_plugin_config_is_independent(self):
        from utils.config import Config
        options = copy.deepcopy(DEFAULTS); options['next_service'] = 'deepseek'
        profiles = {'pdf2zh_next:deepseek': {'apiKey': 'desktop-only', 'extraData': {'x': False}}}
        payload = build_payload(options, profiles)
        payload['llm_api']['extraData']['x'] = True
        self.assertFalse(profiles['pdf2zh_next:deepseek']['extraData']['x'])
        plugin = Config({'engine': 'pdf2zh_next', 'next_service': 'bing', 'qps': 3})
        self.assertEqual(plugin.qps, 3)
        self.assertEqual(plugin.service, 'bing')
        self.assertEqual(plugin.llm_api['apiKey'], '')

    def test_config_profiles_seed_credentials_without_modifying_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            text = '[deepseek_detail]\ndeepseek_api_key="example-key"\ndeepseek_model="test-model"\n'
            config.write_text(text, encoding='utf-8')
            self.assertEqual(read_profiles(temp)['pdf2zh_next:deepseek']['apiKey'], 'example-key')
            self.assertEqual(config.read_text(encoding='utf-8'), text)

    def test_bad_limits_and_empty_outputs_are_rejected(self):
        for change in ({'qps': 0}, {'poolSize': -1}, {'dualMode': 'broken'}, {'pdf_offset_ratio': 0}, {'sourceLang': ''}):
            with self.assertRaises(ValueError):
                build_payload({**DEFAULTS, **change}, {})
        self.assertIs(load_options({'translation': {'autoOcr': 'false'}})['autoOcr'], False)


class TaskControlTests(unittest.TestCase):
    def test_queued_pause_resume_cancel_does_not_run_cancelled_work(self):
        manager = TaskManager()
        manager.add_task('queued', {'taskId': 'queued', 'active': True, 'status': 'queued', 'progress': 0})
        manager.act('queued', 'pause')
        work, errors = [], []
        def run():
            try:
                manager.run_job('queued', lambda: work.append('ran'))
            except TaskCancelled as exc:
                errors.append(exc)
        thread = threading.Thread(target=run); thread.start()
        time.sleep(.1)
        self.assertEqual(work, [])
        self.assertEqual(task_rows(manager.get_active_tasks_list(), [])[0][1][1], '已暂停')
        manager.act('queued', 'cancel'); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(work, [])
        self.assertEqual(len(errors), 1)
        manager.complete_task('queued', 'failed', error='late child exit')
        self.assertEqual(manager.get_history()[0]['status'], 'cancelled')
        with self.assertRaises(ValueError):
            manager.act('queued', 'resume')

    def test_a_paused_queued_task_does_not_block_other_documents(self):
        manager = TaskManager()
        for key in ('first', 'second'):
            manager.add_task(key, {'taskId': key, 'active': True, 'status': 'queued', 'progress': 0})
        manager.act('first', 'pause')
        complete = threading.Event()
        thread = threading.Thread(target=lambda: manager.run_job('first', complete.set)); thread.start()
        self.assertEqual(manager.run_job('second', lambda: 42), 42)
        self.assertFalse(complete.is_set())
        manager.act('first', 'resume'); thread.join(3)
        self.assertTrue(complete.is_set())

    def test_history_survives_restart_and_keeps_stage_timings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'task-history.json'
            manager = TaskManager(); manager.configure_history(path)
            manager.add_task('saved', {'taskId': 'saved', 'active': True, 'fileName': 'a.pdf', 'progress': 0})
            manager.update_task('saved', {'stage': 'Parse PDF', 'progress': 25})
            manager.complete_task('saved', 'success', file_paths=['a.mono.pdf'])
            restarted = TaskManager(); restarted.configure_history(path)
            self.assertEqual(restarted.get_history()[0]['taskId'], 'saved')
            self.assertIn('Parse PDF', restarted.get_history()[0]['stageSeconds'])
            self.assertEqual(restarted.get_history()[0]['progress'], 100)
            self.assertNotIn('_stage_started', path.read_text(encoding='utf-8'))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows process-tree regression')
    def test_native_pause_resume_and_cancel_include_real_descendant(self):
        import utils.task_manager as module
        manager = TaskManager(); manager.add_task('native', {'taskId': 'native', 'active': True, 'progress': 0})
        child_code = "import os,time; print('CHILD='+str(os.getpid()),flush=True)\nwhile True: print('child tick',flush=True); time.sleep(.04)"
        parent_code = "import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-u','-c'," + repr(child_code) + "],stdout=sys.stdout,stderr=sys.stderr,creationflags=0x08000000)\nwhile True: print('parent tick',flush=True); time.sleep(.04)"
        output, errors = [], []
        ready = threading.Event()
        def line(value):
            output.append(value)
            if len(output) >= 10 and any(v.startswith('CHILD=') for v in output):
                ready.set()
        def run():
            try:
                stream_process([sys.executable, '-u', '-c', parent_code], task_id='native', on_line=line)
            except Exception as exc:
                errors.append(exc)
        with patch.object(module, 'task_manager', manager):
            thread = threading.Thread(target=run); thread.start()
            try:
                self.assertTrue(ready.wait(8))
                manager.act('native', 'pause')
                time.sleep(.15); frozen = len(output)
                time.sleep(.25); self.assertEqual(len(output), frozen, 'Descendant continued while paused')
                manager.act('native', 'resume')
                deadline = time.monotonic() + 3
                while len(output) <= frozen + 3 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertGreater(len(output), frozen + 3)
                manager.act('native', 'pause')
                manager.act('native', 'cancel')
                thread.join(5)
                self.assertFalse(thread.is_alive(), 'Cancelled process tree retained stdout handles')
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], TaskCancelled)
            finally:
                control = manager.control('native')
                if control:
                    control.cancel()
                thread.join(5)


class ServerWorkflowTests(unittest.TestCase):
    def translator(self):
        from server import PDFTranslator
        return PDFTranslator(SimpleNamespace(enable_venv=False))

    def test_browser_progress_is_removed_but_plugin_endpoints_remain(self):
        translator = self.translator(); client = translator.app.test_client()
        for path in ('/', '/events', '/favicon.svg', '/bo.mp3'):
            self.assertEqual(client.get(path).status_code, 404, path)
        for path in ('/health', '/api/tasks', '/api/history'):
            self.assertEqual(client.get(path).status_code, 200, path)
        routes = {rule.rule for rule in translator.app.url_map.iter_rules()}
        self.assertTrue({'/translate', '/crop', '/crop-compare', '/compare', '/translatedFile/<path:filename>'}.issubset(routes))

    def test_private_task_config_does_not_change_shared_plugin_config(self):
        import server
        translator = self.translator()
        from utils.config import Config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp); shared = path / 'config.toml'
            original = '[deepseek_detail]\ndeepseek_api_key="plugin-key"\ndeepseek_model="deepseek-chat"\n'
            shared.write_text(original, encoding='utf-8')
            config = Config({**DEFAULTS, 'next_service': 'deepseek', 'poolSize': 30, 'llm_api': {'apiKey': 'desktop-key'}})
            with patch.object(server, 'root_path', temp), patch.dict(server.config_path, {'pdf2zh_next': str(shared)}):
                private = translator._prepare_job_config(config, 'pdf2zh_next', 'job')
                self.assertIn('desktop-key', Path(private).read_text(encoding='utf-8'))
                self.assertEqual(shared.read_text(encoding='utf-8'), original)
                self.assertEqual(config.pool_size, 30)
                translator._run_controlled_job('job', lambda: None)
                self.assertFalse(Path(private).exists())


if __name__ == '__main__':
    unittest.main()
