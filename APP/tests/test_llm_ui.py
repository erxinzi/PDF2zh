import copy
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import launcher
from utils.llm_api import ApiRegistry
from utils.llm_ui import SelectBox


class LlmUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        registry_path = Path(self.temp.name) / 'llm_apis.json'
        class App(launcher.LauncherApp):
            def refresh_kernel_status(self, silent=False):
                pass
            def refresh_cache_size(self):
                pass
            def seed_translation_profiles(self, directory, settings):
                self.api_registry = ApiRegistry(registry_path, self.api_catalog).load_or_migrate({}, {})
                self.refresh_api_manager()
                for button in self.api_manager_buttons:
                    button.configure(state='normal')
        self.root = tk.Tk(); self.root.withdraw()
        self.app = App(self.root)
        self.app.desktop_settings = copy.deepcopy(launcher.SETTINGS)
        self.app.save_desktop_settings = lambda _: None
        self.root.update()

    def tearDown(self):
        self.app._close()
        self.temp.cleanup()

    def add_api(self):
        app = self.app
        app.new_api()
        app.api_editor_vars['service'].set('deepseek'); app._api_type_selected()
        app.api_editor_vars['name'].set('Configured account')
        app.api_editor_vars['apiKey'].set('fixture-key')
        app.api_custom_model.set('keep-this-model'); app.add_custom_model()
        app.save_api()
        return app.api_registry.get(app.api_editor_key)

    def test_basic_section_has_five_dropdowns_and_only_last_three_manual_fields(self):
        children = [widget for key, widget in self.app.translation_fields.items() if key != 'fontFamily']
        self.assertEqual(sum(widget.winfo_class() == 'TEntry' for widget in children), 3)
        boxes = [widget for widget in children if isinstance(widget, SelectBox)]
        self.assertEqual(len(boxes), 5)
        for box in boxes:
            self.assertIn(str(box.cget('state')), ('readonly', 'disabled'))
            self.assertTrue(box.bind('<Button-1>'))

    def test_single_click_posts_selection_list_without_enabling_editing(self):
        box = self.app.translation_fields['engine']
        original = box.tk; calls = []
        class TkProxy:
            def call(self, *args):
                if args[0] == 'ttk::combobox::Post':
                    calls.append(args); return ''
                return original.call(*args)
            def __getattr__(self, key):
                return getattr(original, key)
        box.tk = TkProxy()
        try:
            self.assertEqual(box._post(None), 'break')
        finally:
            box.tk = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(box.cget('state')), 'readonly')

    def test_engine_switch_hides_other_engine_parameters_and_preserves_api_selection(self):
        app = self.app
        saved = self.add_api()
        app.provider_choice.set(saved['key']); app._provider_selected()
        self.assertEqual(app.translation_vars['next_service'].get(), 'deepseek')
        self.assertEqual(app.model_choice.get(), 'keep-this-model')
        app.translation_vars['engine'].set('pdf2zh')
        self.assertEqual(app.provider_label.get(), '翻译服务')
        self.assertEqual(app.engine_sections['pdf2zh'].winfo_manager(), 'pack')
        self.assertEqual(app.engine_sections['pdf2zh_next'].winfo_manager(), '')
        app.translation_vars['engine'].set('pdf2zh_next')
        self.assertEqual(app.provider_label.get(), '大模型供应商')
        self.assertEqual(app.provider_choice.get(), saved['key'])
        self.assertEqual(app.model_choice.get(), 'keep-this-model')

    def test_add_and_disable_update_the_settings_provider_picker(self):
        app = self.app; saved = self.add_api()
        for box in app.provider_boxes:
            self.assertIn(saved['key'], dict(box.choices))
        app.api_tree.selection_set(saved['key']); app.toggle_api()
        for box in app.provider_boxes:
            self.assertNotIn(saved['key'], dict(box.choices))

    def test_home_uses_settings_and_places_api_next_to_translate_and_plugin_at_left(self):
        app = self.app
        self.assertEqual(len(app.provider_boxes), 1)
        self.assertEqual(len(app.model_boxes), 1)
        self.assertEqual(app.upload_button.master, app.api_settings_button.master)
        self.assertEqual(app.upload_button.master, app.plugin_button.master)
        self.assertEqual(app.plugin_button.cget('text'), 'Zotero插件')
        self.assertEqual(app.plugin_button.pack_info()['side'], 'left')
        self.assertEqual(app.api_settings_button.pack_info()['side'], 'right')
        self.assertIn('Plugins Manager', app.plugin_drag.tooltip.text)

    def test_plugin_button_starts_native_file_drag_only_after_moving_and_suppresses_click(self):
        from utils.plugin_install import plugin_path
        drag = self.app.plugin_drag
        drag.press(SimpleNamespace(x_root=100, y_root=100))
        with patch('utils.windows_drag.drag_file', return_value=False) as native:
            drag.motion(SimpleNamespace(x_root=102, y_root=101))
            native.assert_not_called()
            self.assertEqual(drag.motion(SimpleNamespace(x_root=110, y_root=105)), 'break')
            native.assert_called_once_with(plugin_path())
            self.assertEqual(drag.release(None), 'break')

    def test_wheel_routes_before_class_bindings_and_requires_a_click(self):
        app = self.app
        self.root.geometry('1000x650+5000+5000'); self.root.deiconify()
        app.show_page('translation'); self.root.update()
        box = app.translation_fields['engine']
        canvas = app.page_canvases['translation']
        before = app.translation_vars['engine'].get()
        box.event_generate('<MouseWheel>', delta=-120); self.root.update()
        self.assertEqual(app.translation_vars['engine'].get(), before)
        self.assertGreater(canvas.yview()[0], 0)
        app.wheel_focus.click(SimpleNamespace(widget=box))
        box.event_generate('<MouseWheel>', delta=-120); self.root.update()
        self.assertNotEqual(app.translation_vars['engine'].get(), before)
        self.assertLess(box.bindtags().index(app.wheel_focus.tag), box.bindtags().index('TCombobox'))
        app.show_page('home'); self.root.update()
        tree = app.task_trees['active']
        for i in range(60):
            tree.insert('', 'end', iid='wheel-' + str(i), values=(str(i), 'running', '1%', 'pdf2zh_next'))
        tree.yview_moveto(0); canvas = app.page_canvases['home']; canvas.yview_moveto(0)
        tree.event_generate('<MouseWheel>', delta=-120); self.root.update()
        self.assertEqual(tree.yview()[0], 0)
        self.assertGreater(canvas.yview()[0], 0)
        app.wheel_focus.click(SimpleNamespace(widget=tree))
        page_before = canvas.yview()
        tree.event_generate('<MouseWheel>', delta=-120); self.root.update()
        self.assertGreater(tree.yview()[0], 0)
        self.assertEqual(canvas.yview(), page_before)

    def test_task_description_follows_visible_tab_during_refresh_and_empty_selection(self):
        app = self.app
        running = {'taskId': 'running', 'fileName': 'active.pdf', 'active': True, 'status': 'running', 'message': 'Parsing'}
        history = {'taskId': 'old', 'fileName': 'history.pdf', 'active': False, 'status': 'success', 'message': 'Finished'}
        app._paint_tasks([running], [history])
        app.task_trees['active'].selection_set('running'); self.root.update()
        self.assertIn('active.pdf', app.task_detail_var.get())
        app.task_notebook.select(1); self.root.update()
        self.assertNotIn('active.pdf', app.task_detail_var.get())
        app.task_trees['history'].selection_set('old'); self.root.update()
        self.assertIn('history.pdf', app.task_detail_var.get())
        app._paint_tasks([{**running, 'message': 'Translating'}], [history])
        self.assertIn('history.pdf', app.task_detail_var.get())
        app.task_notebook.select(0); self.root.update()
        app._paint_tasks([running], [history])
        self.assertIn('active.pdf', app.task_detail_var.get())
        app._paint_tasks([], [history]); self.root.update()
        self.assertNotIn('active.pdf', app.task_detail_var.get())
        self.assertNotIn('history.pdf', app.task_detail_var.get())

    def test_checkbuttons_use_checkmark_images_including_disabled_selected_state(self):
        from tkinter import ttk
        layout = str(ttk.Style(self.root).layout('TCheckbutton'))
        self.assertIn('PDF2zh.Check.indicator', layout)
        self.assertNotIn("'Checkbutton.indicator'", layout)
        images = self.root._pdf2zh_check_images
        self.assertEqual(len(images), 4)
        selected = images[1]
        size = selected.width() - 5
        self.assertEqual(selected.get(round(size * .41), round(size * .73) + 1), (255, 255, 255))

    def test_selected_account_is_bound_to_translation_payload_and_missing_selection_is_rejected(self):
        from utils.desktop_translation import build_payload
        app = self.app
        saved = self.add_api()
        second = app.api_registry.upsert({**saved, 'key': 'another-account', 'apiKey': 'other-fixture-key', 'model': 'different-model'})
        app.provider_choice.set(second['key']); app._provider_selected()
        app._require_selected_provider()
        payload = build_payload(app._translation_values(), app.translation_profiles)
        self.assertEqual(payload['llm_api']['apiKey'], 'other-fixture-key')
        self.assertEqual(payload['llm_api']['model'], 'different-model')
        self.assertEqual(payload['next_service'], 'deepseek')
        app.api_registry.delete(second['key']); app.refresh_api_manager()
        with self.assertRaises(ValueError):
            app._require_selected_provider()

    def test_probe_runs_off_gui_thread_and_does_not_change_selected_model(self):
        app = self.app; saved = self.add_api()
        app.api_tree.selection_set(saved['key']); app.edit_api()
        release, started = threading.Event(), threading.Event()
        worker_ids = []
        def slow(*_):
            worker_ids.append(threading.get_ident()); started.set(); release.wait(3)
            return ['newly-discovered']
        with patch('utils.llm_ui.discover_models', slow):
            begin = time.perf_counter(); app.probe_api_models()
            self.assertLess(time.perf_counter() - begin, .15)
            self.assertTrue(started.wait(2))
            self.assertNotEqual(worker_ids[0], threading.get_ident())
            self.root.update()
            self.assertTrue(app.jobs.busy('api-model-discovery'))
            release.set()
            deadline = time.monotonic() + 3
            while app.jobs.busy('api-model-discovery') and time.monotonic() < deadline:
                self.root.update(); time.sleep(.01)
        self.assertIn('newly-discovered', dict(app.api_editor_model_box.choices))
        self.assertEqual(app.api_editor_vars['model'].get(), 'keep-this-model')
        app.save_api()
        self.assertEqual(app.api_registry.get(saved['key'])['model'], 'keep-this-model')

    def test_stale_probe_result_does_not_overwrite_a_new_provider_draft(self):
        app = self.app; saved = self.add_api()
        app.api_tree.selection_set(saved['key']); app.edit_api()
        release = threading.Event()
        def slow(*_):
            release.wait(3); return ['stale-result']
        with patch('utils.llm_ui.discover_models', slow):
            app.probe_api_models()
            app.cancel_api_edit(); app.new_api()
            release.set()
            deadline = time.monotonic() + 3
            while app.jobs.busy('api-model-discovery') and time.monotonic() < deadline:
                self.root.update(); time.sleep(.01)
        self.assertNotIn('stale-result', dict(app.api_editor_model_box.choices))
        self.assertEqual(app.api_editor_vars['service'].get(), 'openailiked')

    def test_deepseek_thinking_controls_save_selected_effort_and_preserve_custom_fields(self):
        import json
        app = self.app
        saved = self.add_api()
        entry = {**saved, 'model': 'deepseek-v4-pro', 'extraData': {
            'deepseek_thinking_mode': 'enabled', 'deepseek_reasoning_effort': 'high', 'custom_hint': 'preserve'}}
        app.api_registry.upsert(entry); app.refresh_api_manager()
        app.api_tree.selection_set(entry['key']); app.edit_api()
        self.assertEqual(app.reasoning_frame.winfo_manager(), 'grid')
        self.assertEqual(app.reasoning_effort.get(), 'high')
        app.reasoning_effort.set('max'); app._reasoning_selected('effort')
        app.save_api()
        stored = app.api_registry.get(entry['key'])
        self.assertEqual(stored['extraData']['deepseek_reasoning_effort'], 'max')
        self.assertEqual(stored['extraData']['deepseek_thinking_mode'], 'enabled')
        self.assertEqual(stored['extraData']['custom_hint'], 'preserve')
        app.api_tree.selection_set(entry['key']); app.edit_api()
        self.assertEqual(app.reasoning_effort.get(), 'max')

    def test_compatible_effort_control_sets_send_flag_and_plain_edit_preserves_it(self):
        import json
        app = self.app
        app.new_api(); app.api_editor_vars['model'].set('reasoning-model')
        app.api_editor_vars['apiKey'].set('fixture-key')
        app.reasoning_effort.set('high'); app._reasoning_selected('effort')
        expected = json.loads(app.api_extra.get('1.0', 'end'))
        self.assertIs(expected['openai_compatible_send_reasoning_effort'], True)
        app.save_api(); identity = app.api_editor_key
        app.api_tree.selection_set(identity); app.edit_api(); app.save_api()
        self.assertEqual(app.api_registry.get(identity)['extraData'], expected)

    def test_footer_version_links_and_hover_text_match_requested_attribution(self):
        from utils.version import VERSION
        from utils.links import UPSTREAM_URL, PROJECT_URL
        app = self.app
        self.assertEqual(app.footer_version.cget('text'), 'v' + VERSION)
        self.assertEqual(app.upstream_link.cget('text'), 'PDF2zh')
        self.assertEqual(app.project_link.cget('text'), 'erxinzi')
        self.assertEqual(app.upstream_link.tooltip.text, '本项目基于zotero pdf2zh二次开发，点击跳转原作者GitHub')
        self.assertEqual(app.project_link.tooltip.text, '点击跳转本项目主页')
        app.upstream_link.tooltip.schedule()
        with patch('utils.links.webbrowser.open_new_tab') as open_link:
            app.upstream_link.open(); app.project_link.open()
        self.assertEqual([call.args[0] for call in open_link.call_args_list], [UPSTREAM_URL, PROJECT_URL])
        self.assertIsNone(app.upstream_link.tooltip.timer)


if __name__ == '__main__':
    unittest.main()
