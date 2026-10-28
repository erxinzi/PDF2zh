"""Native API management; network work is delivered through the GUI job queue."""
from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from utils.llm_api import ApiRegistry, configured, discover_models, load_catalog, requires_model, supported_engines
from utils.reasoning import reasoning_spec, apply_reasoning_control


class SelectBox(ttk.Combobox):
    """A selection-only field with a separate stored value and display label."""
    def __init__(self, parent, variable, choices=(), command=None, **kwargs):
        self.variable = variable
        self.display = tk.StringVar(master=parent)
        self.choices = []
        super().__init__(parent, textvariable=self.display, state='readonly', **kwargs)
        self.set_choices(choices)
        self._trace = variable.trace_add('write', self._sync)
        self.bind('<<ComboboxSelected>>', lambda _: self._choose(command))
        self.bind('<Button-1>', self._post)
        self.bind('<Destroy>', self._destroyed, add='+')
        self._sync()

    def _destroyed(self, event):
        if event.widget is self:
            self.variable.trace_remove('write', self._trace)

    def _post(self, _):
        if not self.instate(['disabled']):
            self.focus_set()
            self.tk.call('ttk::combobox::Post', str(self))
        return 'break'

    def _choose(self, command):
        index = self.current()
        if 0 <= index < len(self.choices):
            self.variable.set(self.choices[index][0])
            if command:
                command()

    def _sync(self, *_):
        value = self.variable.get()
        self.display.set(next((label for code, label in self.choices if code == value), value))

    def set_choices(self, choices):
        self.choices = [(str(item[0]), str(item[1])) if isinstance(item, (list, tuple)) else (str(item), str(item)) for item in choices]
        self.configure(values=[label for _, label in self.choices])
        self._sync()


class LlmUi:
    def init_api_manager(self):
        self.api_catalog = load_catalog()
        self.api_registry = None
        self.provider_choice = tk.StringVar()
        self.model_choice = tk.StringVar()
        self.provider_label = tk.StringVar(value='大模型供应商')
        self.provider_boxes, self.model_boxes = [], []
        self.api_state = tk.StringVar(value='正在加载 API 配置…')
        self.api_editor_key = None
        self.api_draft_models = []
        self.api_generation = 0
        self.api_editor_vars = {key: tk.StringVar() for key in ('name', 'service', 'apiKey', 'apiUrl', 'model')}
        self.api_active_var = tk.BooleanVar(value=True)
        self.api_probe_status = tk.StringVar()
        self._api_refreshing = False
        self.translation_profiles = {}

    def seed_translation_profiles(self, config_dir, settings):
        from utils.desktop_translation import read_profiles
        directory = Path(config_dir)
        path = directory.parent / 'logs/llm-apis-self-test.json' if '--self-test' in sys.argv else directory / 'llm_apis.json'
        snapshot = copy.deepcopy(settings)
        def work():
            if '--self-test' in sys.argv:
                return ApiRegistry(path, self.api_catalog).load_or_migrate({}, {})
            return ApiRegistry(path, self.api_catalog).load_or_migrate(snapshot, read_profiles(directory))
        def ready(registry):
            self.api_registry = registry
            self.api_state.set(f'API 配置自动保存在 {registry.path}')
            self.refresh_api_manager()
            for button in self.api_manager_buttons:
                button.configure(state='normal')
        def failed(exc):
            self.api_state.set('API 配置加载失败；原文件已保留，请查看日志。')
            self._msg('API 配置加载失败', str(exc), error=True)
        self.jobs.submit('translation-api-seed', work, ready, failed)

    def _engine_changed(self, *_):
        if self._api_refreshing:
            return
        self._sync_provider_selection()
        if hasattr(self, 'engine_sections'):
            selected = self.translation_vars['engine'].get()
            for engine, frame in self.engine_sections.items():
                frame.pack_forget()
                if engine == selected:
                    frame.pack(fill='x', pady=self.px(8), before=self.output_section)

    def language_choices(self, source=False):
        preferred = {'English': '英语', 'Simplified Chinese': '简体中文', 'Traditional Chinese - Hong Kong': '繁体中文（香港）',
                     'Traditional Chinese - Taiwan': '繁体中文（台湾）', 'Japanese': '日语', 'Korean': '韩语',
                     'German': '德语', 'French': '法语', 'Spanish': '西班牙语', 'Russian': '俄语'}
        choices = [(code, f'{preferred.get(name, name)} · {code}') for name, code in self.api_catalog['languages'].items()]
        if source:
            choices.insert(0, ('auto', '自动识别 · auto'))
        current = self.translation_vars['sourceLang' if source else 'targetLang'].get()
        if current and current not in {value for value, _ in choices}:
            choices.append((current, current))
        return choices

    def provider_box(self, parent, width=28):
        widget = SelectBox(parent, self.provider_choice, command=self._provider_selected, width=width)
        self.provider_boxes.append(widget)
        return widget

    def model_box(self, parent, width=28):
        widget = SelectBox(parent, self.model_choice, command=self._translation_model_selected, width=width)
        self.model_boxes.append(widget)
        return widget

    def _sync_provider_selection(self):
        engine = self.translation_vars['engine'].get()
        self.provider_label.set('大模型供应商' if engine == 'pdf2zh_next' else '翻译服务')
        if not self.api_registry:
            for widget in self.provider_boxes + self.model_boxes:
                widget.configure(state='disabled')
            return
        registry = self.api_registry
        choices = registry.choices(engine)
        labels = [(entry['key'], f"{entry['name']} · {registry.catalog['providers'][entry['service']].get('name', entry['service'])}") for entry in choices]
        names = [label for _, label in labels]
        labels = [(key, label + (' [' + key[-6:] + ']' if names.count(label) > 1 else '')) for key, label in labels]
        selected = registry.selected_entry(engine)
        self._api_refreshing = True
        try:
            self.provider_choice.set(selected['key'] if selected else '')
            for widget in self.provider_boxes:
                widget.set_choices(labels)
                widget.configure(state='readonly' if labels else 'disabled')
            if selected:
                self.translation_vars['service' if engine == 'pdf2zh' else 'next_service'].set(selected['service'])
            else:
                self.translation_vars['service' if engine == 'pdf2zh' else 'next_service'].set('')
            self.model_choice.set(selected['model'] if selected else '')
            model_values = registry.model_choices(selected) if selected and requires_model(selected['service'], engine) else []
            for widget in self.model_boxes:
                widget.set_choices(model_values)
                widget.configure(state='readonly' if model_values else 'disabled')
        finally:
            self._api_refreshing = False
        self._store_api_profile()

    def _provider_selected(self):
        if not self.api_registry:
            return
        try:
            self.api_registry.select(self.translation_vars['engine'].get(), self.provider_choice.get())
            self._sync_provider_selection()
        except ValueError as exc:
            self._msg('供应商选择', str(exc), error=True)

    def _translation_model_selected(self):
        if not self.api_registry:
            return
        entry = self.api_registry.selected_entry(self.translation_vars['engine'].get())
        if entry:
            updated = copy.deepcopy(entry); updated['model'] = self.model_choice.get()
            self.api_registry.upsert(updated)
            self.refresh_api_manager()

    def _store_api_profile(self):
        self.translation_profiles = {}
        if self.api_registry:
            for engine in ('pdf2zh_next', 'pdf2zh'):
                entry = self.api_registry.selected_entry(engine)
                if entry:
                    self.translation_profiles[f"{engine}:{entry['service']}"] = {key: copy.deepcopy(entry[key]) for key in ('apiKey', 'apiUrl', 'model', 'extraData')}

    def _require_selected_provider(self):
        if not self.api_registry:
            raise ValueError('API 配置尚未加载完成')
        entry = self.api_registry.selected_entry(self.translation_vars['engine'].get())
        if not entry:
            raise ValueError('请先添加并启用 API 配置，再选择供应商')
        self._store_api_profile()

    def refresh_api_manager(self):
        if not self.api_registry:
            return
        registry = self.api_registry
        selection = self.api_tree.selection()
        self.api_tree.delete(*self.api_tree.get_children())
        for entry in registry.entries:
            complete = any(configured(entry, engine) for engine in entry['engines'])
            state = '已启用' if complete and entry['activate'] else '未启用' if complete else '待补全'
            self.api_tree.insert('', 'end', iid=entry['key'], values=(entry['name'], registry.catalog['providers'][entry['service']].get('name', entry['service']), entry['model'] or '无需模型', state))
        if selection and registry.get(selection[0]):
            self.api_tree.selection_set(selection[0])
        self._sync_provider_selection()

    def _build_api_manager(self, parent):
        body = self._translation_card(parent, 'LLM API 管理')
        ttk.Label(body, textvariable=self.api_state, style='MutedCard.TLabel', wraplength=self.px(820)).pack(anchor='w', pady=(0, 8))
        self.api_tree = ttk.Treeview(body, columns=('name', 'provider', 'model', 'state'), show='headings', height=4)
        for column, title, width in (('name', '配置名称', 190), ('provider', '供应商', 170), ('model', '模型', 220), ('state', '状态', 90)):
            self.api_tree.heading(column, text=title); self.api_tree.column(column, width=self.px(width), minwidth=self.px(60))
        self.api_tree.pack(fill='x')
        self.api_tree.bind('<Double-1>', lambda _: self.edit_api())
        actions = ttk.Frame(body, style='Card.TFrame'); actions.pack(fill='x', pady=8)
        self.api_manager_buttons = []
        for label, action in (('新增 API', self.new_api), ('编辑', self.edit_api), ('删除', self.delete_api), ('启用 / 停用', self.toggle_api), ('导入 JSON', self.import_api_profiles), ('导出 JSON', self.export_api_profiles)):
            button = ttk.Button(actions, text=label, command=action, state='disabled')
            button.pack(side='left', padx=(0, 6)); self.api_manager_buttons.append(button)
        self.api_editor = ttk.Frame(body, style='Card.TFrame', padding=(0, self.px(8)))
        self.api_editor.pack(fill='x')
        self.api_editor_heading = tk.StringVar(value='点击“新增 API”或选择配置后点击“编辑”')
        self.api_url_label = tk.StringVar(value='Base URL')
        ttk.Label(self.api_editor, textvariable=self.api_editor_heading, style='H2.TLabel').grid(row=0, column=0, columnspan=3, sticky='w', pady=6)
        template_choices = [(service, template.get('name', service)) for service, template in self.api_catalog['providers'].items() if template.get('engines')]
        for row, (key, label) in enumerate((('name', '配置名称'), ('service', '供应商类型'), ('apiKey', 'API Key'), ('apiUrl', 'Base URL'), ('model', '模型')), start=1):
            ttk.Label(self.api_editor, textvariable=self.api_url_label if key == 'apiUrl' else None,
                      text=label, style='Card.TLabel').grid(row=row, column=0, sticky='w', padx=(0, 14), pady=5)
            if key == 'service':
                widget = SelectBox(self.api_editor, self.api_editor_vars[key], template_choices, command=self._api_type_selected, width=36)
            elif key == 'model':
                widget = SelectBox(self.api_editor, self.api_editor_vars[key], width=36)
                self.api_editor_model_box = widget
            else:
                widget = ttk.Entry(self.api_editor, textvariable=self.api_editor_vars[key], show='●' if key == 'apiKey' else '', width=38)
            widget.grid(row=row, column=1, sticky='ew', pady=5)
        self.api_editor.columnconfigure(1, weight=1)
        self.api_probe_button = ttk.Button(self.api_editor, text='探测模型', command=self.probe_api_models)
        self.api_probe_button.grid(row=5, column=2, padx=8)
        self.api_custom_model = tk.StringVar()
        ttk.Label(self.api_editor, text='手动模型 ID（可选）', style='MutedCard.TLabel').grid(row=6, column=0, sticky='w')
        ttk.Entry(self.api_editor, textvariable=self.api_custom_model).grid(row=6, column=1, sticky='ew', pady=5)
        ttk.Button(self.api_editor, text='加入模型列表', command=self.add_custom_model).grid(row=6, column=2, padx=8)
        ttk.Label(self.api_editor, text='附加参数（JSON）', style='Card.TLabel').grid(row=8, column=0, sticky='nw', pady=6)
        self.api_extra = tk.Text(self.api_editor, height=5, width=40, font=('Consolas', 10), relief='solid', borderwidth=1)
        self.api_extra.grid(row=8, column=1, sticky='ew', pady=6); self.api_extra.insert('1.0', '{}')
        self.api_extra_hint = tk.StringVar()
        ttk.Label(self.api_editor, textvariable=self.api_extra_hint, style='MutedCard.TLabel', wraplength=self.px(700)).grid(row=9, column=1, columnspan=2, sticky='w')
        ttk.Checkbutton(self.api_editor, text='启用此配置', variable=self.api_active_var).grid(row=10, column=1, sticky='w', pady=6)
        buttons = ttk.Frame(self.api_editor, style='Card.TFrame'); buttons.grid(row=11, column=1, sticky='w', pady=6)
        ttk.Button(buttons, text='保存 API', style='Accent.TButton', command=self.save_api).pack(side='left')
        ttk.Button(buttons, text='取消编辑', command=self.cancel_api_edit).pack(side='left', padx=8)
        ttk.Label(body, textvariable=self.api_probe_status, style='MutedCard.TLabel', wraplength=self.px(820)).pack(anchor='w')
        self.api_editor.pack_forget()
        self.reasoning_mode = tk.StringVar()
        self.reasoning_effort = tk.StringVar()
        self.reasoning_frame = ttk.Frame(self.api_editor, style='Card.TFrame')
        self.reasoning_frame.grid(row=7, column=0, columnspan=3, sticky='ew', pady=4)
        self.reasoning_mode_label = tk.StringVar(value='思考模式')
        ttk.Label(self.reasoning_frame, textvariable=self.reasoning_mode_label, style='Card.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 14), pady=4)
        self.reasoning_mode_box = SelectBox(self.reasoning_frame, self.reasoning_mode,
                                           command=lambda: self._reasoning_selected('mode'), width=32)
        self.reasoning_mode_box.grid(row=0, column=1, sticky='ew', pady=4)
        self.reasoning_effort_label = ttk.Label(self.reasoning_frame, text='思考强度', style='Card.TLabel')
        self.reasoning_effort_label.grid(row=1, column=0, sticky='w', padx=(0, 14), pady=4)
        self.reasoning_effort_box = SelectBox(self.reasoning_frame, self.reasoning_effort,
                                             command=lambda: self._reasoning_selected('effort'), width=32)
        self.reasoning_effort_box.grid(row=1, column=1, sticky='ew', pady=4)
        self.reasoning_frame.columnconfigure(1, weight=1)
        self.reasoning_frame.grid_remove()
        self.api_editor_vars['model'].trace_add('write', lambda *_: self._refresh_reasoning())
        self.api_extra.bind('<FocusOut>', lambda _: self._refresh_reasoning())

    def new_api(self):
        if not self.api_registry:
            return
        self.api_editor_key = None; self.api_generation += 1
        self.api_draft_models = []
        for variable in self.api_editor_vars.values():
            variable.set('')
        self.api_editor_vars['service'].set('openailiked')
        self.api_active_var.set(True)
        self.api_extra.delete('1.0', 'end'); self.api_extra.insert('1.0', '{}')
        self.api_editor_heading.set('新增 API')
        self.api_editor.pack(fill='x')
        self._api_type_selected()

    def edit_api(self):
        selected = self.api_tree.selection()
        entry = self.api_registry.get(selected[0]) if selected and self.api_registry else None
        if not entry:
            return
        self.api_generation += 1; self.api_editor_key = entry['key']
        self.api_draft_models = list(entry.get('models', []))
        for key, variable in self.api_editor_vars.items():
            variable.set(entry[key])
        self.api_active_var.set(entry['activate'])
        self.api_custom_model.set('')
        self.api_extra.delete('1.0', 'end'); self.api_extra.insert('1.0', json.dumps(entry['extraData'], ensure_ascii=False, indent=2))
        self.api_editor_heading.set('编辑 API · ' + entry['name'])
        self._editor_model_choices()
        self._extra_hint()
        self._refresh_reasoning()
        self.api_editor.pack(fill='x')

    def _api_type_selected(self):
        self.api_generation += 1
        service = self.api_editor_vars['service'].get()
        template = self.api_catalog['providers'].get(service, {})
        self.api_editor_vars['name'].set(template.get('name', service))
        self.api_editor_vars['apiUrl'].set(next(iter(template.get('urls', [])), ''))
        self.api_editor_vars['apiKey'].set('')
        self.api_editor_vars['model'].set('')
        self.api_draft_models = []
        self.api_extra.delete('1.0', 'end'); self.api_extra.insert('1.0', '{}')
        self.api_custom_model.set('')
        self._editor_model_choices(); self._extra_hint()
        self._refresh_reasoning()
        self.api_probe_status.set('请选择已有模型，或点击“探测模型”；保存后可在翻译设置中选择此供应商。')

    def _extra_hint(self):
        from utils.config_map import pdf2zh_next_service_aliases
        from utils.llm_api import field_map
        service = self.api_editor_vars['service'].get()
        canonical = pdf2zh_next_service_aliases.get(service, service)
        fields = self.api_catalog.get('extra_fields', {}).get(canonical, [])
        support = '适用内核：' + '、'.join(supported_engines(service)) + '。'
        custom_url = any('apiUrl' in field_map(service, engine) for engine in supported_engines(service))
        self.api_url_label.set('翻译接口 Base URL' if custom_url else '模型探测 Base URL')
        if not custom_url:
            support += '该原生供应商的翻译地址由内核固定，自定义翻译地址请选择 OpenAI-Liked。'
        self.api_extra_hint.set(support + ('附加参数：' + '、'.join(field['key'] for field in fields) if fields else '附加参数按 JSON 类型填写。'))

    def _editor_model_choices(self):
        service = self.api_editor_vars['service'].get()
        template = self.api_catalog['providers'].get(service, {})
        current = self.api_editor_vars['model'].get()
        choices = list(dict.fromkeys(value for value in [current, *self.api_draft_models, *template.get('models', [])] if value))
        self.api_editor_model_box.set_choices(choices)
        self.api_editor_model_box.configure(state='readonly' if choices else 'disabled')

    def _refresh_reasoning(self):
        if not hasattr(self, 'reasoning_frame'):
            return
        spec = reasoning_spec(self.api_editor_vars['service'].get(), self.api_editor_vars['model'].get())
        if not spec:
            self.reasoning_frame.grid_remove()
            return
        try:
            extra = json.loads(self.api_extra.get('1.0', 'end').strip() or '{}')
            if not isinstance(extra, dict):
                return
        except ValueError:
            return
        self.reasoning_frame.grid()
        self.reasoning_mode_label.set(spec['mode_label'])
        mode = extra.get(spec['mode'], '')
        mode = str(mode).lower() if isinstance(mode, bool) else str(mode or '')
        effort = str(extra.get(spec['effort'], '') or '') if spec['effort'] else ''
        self.reasoning_mode.set(mode); self.reasoning_effort.set(effort)
        self.reasoning_mode_box.set_choices([('', '沿用内核设置'), *spec['mode_values']])
        values = list(dict.fromkeys([*spec['effort_values'], *([effort] if effort else [])]))
        self.reasoning_effort_box.set_choices([('', '沿用内核设置'), *values])
        if spec['effort']:
            self.reasoning_effort_label.grid(); self.reasoning_effort_box.grid()
            self.reasoning_effort_box.configure(state='disabled' if mode in ('false', 'disabled') else 'readonly')
        else:
            self.reasoning_effort_label.grid_remove(); self.reasoning_effort_box.grid_remove()

    def _reasoning_selected(self, control):
        spec = reasoning_spec(self.api_editor_vars['service'].get(), self.api_editor_vars['model'].get())
        if not spec:
            return
        try:
            extra = json.loads(self.api_extra.get('1.0', 'end').strip() or '{}')
            value = self.reasoning_mode.get() if control == 'mode' else self.reasoning_effort.get()
            extra = apply_reasoning_control(extra, spec, control, value)
            self.api_extra.delete('1.0', 'end')
            self.api_extra.insert('1.0', json.dumps(extra, ensure_ascii=False, indent=2))
            self._refresh_reasoning()
        except ValueError as exc:
            self.api_probe_status.set(str(exc))

    def add_custom_model(self):
        model = self.api_custom_model.get().strip()
        if model:
            self.api_draft_models = list(dict.fromkeys([*self.api_draft_models, model]))
            self.api_editor_vars['model'].set(model)
            self._editor_model_choices(); self.api_custom_model.set('')

    def _editor_entry(self):
        extra = json.loads(self.api_extra.get('1.0', 'end').strip() or '{}')
        if not isinstance(extra, dict):
            raise ValueError('附加参数应为 JSON 对象')
        previous = self.api_registry.get(self.api_editor_key) if self.api_registry else None
        return {**{key: value.get().strip() for key, value in self.api_editor_vars.items()},
                'key': self.api_editor_key or str(uuid.uuid4()), 'extraData': extra,
                'engines': previous['engines'] if previous and previous['service'] == self.api_editor_vars['service'].get() else supported_engines(self.api_editor_vars['service'].get()),
                'activate': self.api_active_var.get(), 'models': list(self.api_draft_models)}

    def save_api(self):
        if not self.api_registry:
            return
        try:
            entry = self.api_registry.upsert(self._editor_entry())
            self.api_editor_key = entry['key']
            self.api_generation += 1
            self.refresh_api_manager(); self.api_tree.selection_set(entry['key'])
            self.api_probe_status.set('API 已保存，可在上方供应商列表中选择；当前模型未自动更改。')
            self.api_editor.pack_forget()
        except (ValueError, OSError) as exc:
            self.api_probe_status.set(str(exc))

    def cancel_api_edit(self):
        self.api_generation += 1
        self.api_editor.pack_forget()
        self.api_probe_status.set('已取消编辑，已保存的配置保持不变。')

    def delete_api(self):
        selected = self.api_tree.selection()
        if selected and self.api_registry:
            self.api_registry.delete(selected[0])
            if self.api_editor_key == selected[0]:
                self.cancel_api_edit()
            self.refresh_api_manager()
            self.api_probe_status.set('配置已删除；如删除了当前供应商，请重新选择。')

    def toggle_api(self):
        selected = self.api_tree.selection()
        entry = self.api_registry.get(selected[0]) if selected and self.api_registry else None
        if entry:
            updated = copy.deepcopy(entry); updated['activate'] = not updated['activate']
            self.api_registry.upsert(updated, require_complete=False)
            self.refresh_api_manager()

    def probe_api_models(self):
        try:
            draft = self._editor_entry()
        except ValueError as exc:
            self.api_probe_status.set(str(exc)); return
        generation = self.api_generation
        identity = tuple(draft.get(key) for key in ('service', 'apiUrl', 'apiKey'))
        self.api_probe_button.configure(state='disabled', text='探测中…')
        self.api_probe_status.set('正在获取模型列表…')
        def current():
            return generation == self.api_generation and identity == tuple(self.api_editor_vars[key].get().strip() for key in ('service', 'apiUrl', 'apiKey'))
        def done(models):
            self.api_probe_button.configure(state='normal', text='探测模型')
            if not current():
                return
            self.api_draft_models = list(dict.fromkeys([*self.api_draft_models, *models]))
            self._editor_model_choices()
            self.api_probe_status.set(f'已获取 {len(models)} 个模型；请选择模型后保存，当前模型保持不变。')
        def failed(exc):
            self.api_probe_button.configure(state='normal', text='探测模型')
            if current():
                self.api_probe_status.set(str(exc))
        self.jobs.submit('api-model-discovery', lambda: discover_models(draft, self.api_catalog), done, failed)

    def import_api_profiles(self):
        if not self.api_registry:
            return
        filename = filedialog.askopenfilename(parent=self.root, title='导入 LLM API JSON', filetypes=[('JSON', '*.json')])
        if not filename:
            return
        try:
            count = self.api_registry.import_data(json.loads(Path(filename).read_text(encoding='utf-8-sig')))
            self.refresh_api_manager()
            self.api_probe_status.set(f'已导入 {count} 个 API 配置；只有配置完整且启用的条目会出现在供应商列表。')
        except (ValueError, OSError) as exc:
            self.api_probe_status.set(str(exc))

    def export_api_profiles(self):
        if not self.api_registry:
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title='导出 LLM API JSON（包含凭据）', defaultextension='.json', filetypes=[('JSON', '*.json')])
        if filename:
            try:
                Path(filename).write_text(json.dumps(self.api_registry.entries, ensure_ascii=False, indent=2), encoding='utf-8')
                self.api_probe_status.set('已导出 API 配置 JSON。')
            except OSError:
                self.api_probe_status.set('无法写入导出文件，请检查目录权限。')
