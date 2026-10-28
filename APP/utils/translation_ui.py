"""Desktop upload, translation preferences and task actions."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from utils.desktop_translation import OUTPUTS, BOOL_LABELS, load_options, validate_options, build_payload, concurrency_preset
from utils.llm_ui import LlmUi, SelectBox


class TranslationUi(LlmUi):
    def init_translation(self, settings, save_settings):
        self.desktop_settings, self.save_desktop_settings = settings, save_settings
        self.translation_vars = {}
        for key, value in load_options(settings).items():
            self.translation_vars[key] = (tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value)))
        self.selected_files = []
        self._all_tasks, self._all_history = [], []
        self._task_records = {}
        self._task_view = 'active'
        self.init_api_manager()
        self.translation_vars['engine'].trace_add('write', self._engine_changed)

    def _translation_values(self):
        values = validate_options({key: var.get() for key, var in self.translation_vars.items()})
        if values['engine'] == 'pdf2zh_next':
            values['noMono'] = not any(values[key] for key in ('mono', 'mono_cut'))
            values['noDual'] = not any(values[key] for key in ('dual', 'dual_cut', 'compare', 'crop_compare'))
        return values

    def _save_translation(self, notify=True):
        try:
            self._store_api_profile()
            values = self._translation_values()
            self.desktop_settings['translation'] = values
            self.save_desktop_settings(self.desktop_settings)
            if notify:
                self._msg('翻译设置', '已保存；本地上传和插件任务从下一个任务使用此接口。')
            return values
        except (ValueError, OSError) as exc:
            self._msg('无法保存翻译设置', str(exc), error=True)
            return None

    def _field(self, parent, row, label, key, values=None, width=24):
        ttk.Label(parent, text=label, style='Card.TLabel').grid(row=row, column=0, sticky='w', padx=(0, 14), pady=5)
        if values:
            widget = SelectBox(parent, self.translation_vars[key], values, width=width)
        else:
            widget = ttk.Entry(parent, textvariable=self.translation_vars[key], width=width)
        widget.grid(row=row, column=1, sticky='ew', pady=5)
        parent.columnconfigure(1, weight=1)
        return widget

    def _translation_card(self, parent, title, *, pack=True):
        card = self._card(parent, title, pack=pack)
        body = ttk.Frame(card, style='Card.TFrame')
        body.pack(fill='x')
        return body

    def _paired_field(self, parent, row, column, label, key, values=None, factory=None):
        """Equal-width halves, with a left label and a right-aligned control."""
        parent.columnconfigure(column, weight=1, uniform='translation-pair')
        cell = ttk.Frame(parent, style='Card.TFrame')
        cell.grid(row=row, column=column, sticky='ew', padx=(0, self.px(12)) if column == 0 else (self.px(12), 0))
        cell.columnconfigure(1, weight=1)
        options = {'textvariable': label} if isinstance(label, tk.Variable) else {'text': label}
        ttk.Label(cell, style='Card.TLabel', **options).grid(row=0, column=0, sticky='w', padx=(0, self.px(8)), pady=5)
        if factory:
            widget = factory(cell, width=22)
        elif values:
            widget = SelectBox(cell, self.translation_vars[key], values, width=22)
        else:
            widget = ttk.Entry(cell, textvariable=self.translation_vars[key], width=22)
        widget.grid(row=0, column=1, sticky='e', pady=5)
        self.translation_fields[key] = widget
        return widget

    def _balanced_checks(self, parent, keys):
        """Keep three balanced columns and wrap long labels to the available width."""
        from tkinter import font
        frame = ttk.Frame(parent, style='Card.TFrame')
        frame.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(self.px(8), 0))
        for column in range(3):
            frame.columnconfigure(column, weight=1, uniform='translation-checks')
        controls = {}
        for index, key in enumerate(keys):
            control = ttk.Checkbutton(frame, text=BOOL_LABELS[key], variable=self.translation_vars[key])
            control.grid(row=index // 3, column=index % 3, sticky='w', padx=(0, self.px(10)), pady=self.px(4))
            controls[key] = control
        measure = font.Font(root=self.root, font=ttk.Style(self.root).lookup('TCheckbutton', 'font'))
        last_width = None
        def wrap(event):
            nonlocal last_width
            if not frame.winfo_ismapped() or event.width == last_width:
                return
            last_width = event.width
            limit = max(self.px(60), event.width // 3 - self.px(38))
            for key, control in controls.items():
                lines, line = [], ''
                for char in BOOL_LABELS[key]:
                    if line and measure.measure(line + char) > limit:
                        lines.append(line); line = ''
                    line += char
                control.configure(text='\n'.join(lines + [line]))
        frame.bind('<Configure>', wrap)
        self.next_flag_grid, self.next_flags = frame, controls

    def _build_upload(self, parent):
        card = self._card(parent, '本地翻译')
        plus = tk.Canvas(card, height=self.px(68), bg='white', highlightthickness=0, cursor='hand2')
        plus.pack(fill='x')
        def draw(event):
            plus.delete('all')
            plus.create_rectangle(2, 2, event.width - 3, event.height - 3, dash=(6, 4), outline='#aab0c0', width=2)
            plus.create_text(event.width / 2, event.height / 2, text='＋  选择 PDF 文件（支持多选）', fill='#3566bd', font=('Segoe UI', 13))
        plus.bind('<Configure>', draw)
        plus.bind('<Button-1>', lambda _: self.select_files())
        plus.bind('<Return>', lambda _: self.select_files())
        plus.configure(takefocus=True)
        self.upload_selection = tk.StringVar(value='尚未选择文件')
        ttk.Label(card, textvariable=self.upload_selection, style='MutedCard.TLabel', wraplength=self.px(760)).pack(anchor='w', pady=(5, 8))
        ttk.Label(card, text='引擎、语言、接口和并发统一使用“设置 · 翻译”中的配置。', style='MutedCard.TLabel').pack(anchor='w', pady=(0, 8))
        modes = ttk.Frame(card, style='Card.TFrame'); modes.pack(fill='x')
        self.home_output_controls = {}
        for index, (key, label, _) in enumerate(OUTPUTS):
            control = ttk.Checkbutton(modes, text=label, variable=self.translation_vars[key])
            control.grid(row=index // 4, column=index % 4, sticky='w', padx=(0, 18), pady=4)
            self.home_output_controls[key] = control
        self.home_layout_controls = []
        for index, (mode, label) in enumerate((('LR', '左右双语（LR）'), ('TB', '上下双语（TB）')), start=6):
            control = ttk.Radiobutton(modes, text=label, value=mode, variable=self.translation_vars['dualMode'],
                                      command=lambda: self.translation_vars['dual'].set(True))
            control.grid(row=index // 4, column=index % 4, sticky='w', padx=(0, 18), pady=4)
            self.home_layout_controls.append(control)
        self.speed_hint = tk.StringVar(value='优化并发仅调整并发数量，保留模型、术语提取、富文本和 OCR 设置。')
        actions = ttk.Frame(card, style='Card.TFrame'); actions.pack(fill='x', pady=(10, 0))
        self.plugin_button = ttk.Button(actions, text='Zotero插件', command=self.reveal_plugin)
        self.plugin_button.pack(side='left')
        from utils.plugin_install import PluginDragButton
        self.plugin_drag = PluginDragButton(self.plugin_button, lambda exc: self._msg('插件拖拽失败', str(exc), error=True))
        self.upload_button = ttk.Button(actions, text='开始翻译', style='Accent.TButton', command=self.submit_upload)
        self.upload_button.pack(side='right')
        self.api_settings_button = ttk.Button(actions, text='API 与完整参数', command=lambda: self.show_page('translation'))
        self.api_settings_button.pack(side='right', padx=8)

    def reveal_plugin(self):
        from utils.plugin_install import plugin_path
        try:
            self._open_dir(plugin_path().parent)
        except OSError as exc:
            self._msg('插件文件不可用', str(exc), error=True)

    def apply_speed_preset(self, fast):
        try:
            values = concurrency_preset({key: var.get() for key, var in self.translation_vars.items()}, fast)
            self.translation_vars['poolSize'].set(str(values['poolSize']))
            self.speed_hint.set(f"并发设为 {values['poolSize']}，请求速率仍为 {values['qps']} QPS；翻译质量相关参数保持当前设置。" if fast else '并发跟随 QPS；翻译质量相关参数保持当前设置。')
        except ValueError:
            self._msg('并发设置', '请先填写有效的 QPS。', error=True)

    def select_files(self):
        chosen = filedialog.askopenfilenames(parent=self.root, title='选择需要翻译的 PDF', filetypes=[('PDF 文档', '*.pdf')])
        if chosen:
            self.selected_files = list(chosen)
            self.upload_selection.set('已选择 ' + str(len(chosen)) + ' 个文件：' + '；'.join(Path(path).name for path in chosen))

    def submit_upload(self):
        if not self.selected_files:
            self.select_files()
            if not self.selected_files:
                return
        values = self._save_translation(notify=False)
        if values is None:
            return
        try:
            self._require_selected_provider()
            payload = build_payload(values, self.translation_profiles)
        except ValueError as exc:
            self._msg('翻译参数无效', str(exc), error=True)
            return
        paths = list(self.selected_files)
        if self._server_busy:
            self._msg('服务正在启停', '请待服务启停完成后提交文件。')
            return
        self.upload_button.configure(state='disabled', text='正在提交…')
        self._server_busy = True
        self.server_btn.configure(state='disabled')
        def work():
            if not self.ctl.running:
                ok, message = self.ctl.start()
                if not ok:
                    raise RuntimeError(message)
            return self.ctl.translator.submit_local(paths, payload)
        def reset():
            self._server_busy = False
            self.upload_button.configure(state='normal', text='开始翻译')
            self._paint_server()
        def done(identifiers):
            reset()
            self.selected_files = []
            self.upload_selection.set(f'已提交 {len(identifiers)} 个翻译任务，可继续选择文件。')
            self.task_notebook.select(0)
            self._tasks_revision = -1; self.refresh_tasks()
        def failed(exc):
            reset(); self._msg('提交失败', str(exc), error=True)
        self.jobs.submit('local-upload', work, done, failed)

    def _build_translation(self):
        f = self.page_bodies['translation']
        ttk.Label(f, text='设置 · 翻译', style='Title.TLabel').pack(anchor='w')
        ttk.Label(f, text='语言、引擎、并发和 API 同时用于本地上传与插件；插件仅覆盖自己的文档处理参数。', style='Muted.TLabel', wraplength=self.px(850)).pack(anchor='w', pady=6)
        basic = self._translation_card(f, '语言、供应商与模型')
        self.basic_settings = basic
        self.translation_fields = {}
        self._paired_field(basic, 1, 0, '翻译引擎', 'engine', ('pdf2zh_next', 'pdf2zh'))
        self._paired_field(basic, 1, 1, '跳过最后几页', 'skipLastPages')
        self._paired_field(basic, 2, 0, '源语言', 'sourceLang', self.language_choices(True))
        self._paired_field(basic, 2, 1, '目标语言', 'targetLang', self.language_choices())
        self._paired_field(basic, 3, 0, self.provider_label, 'provider', factory=self.provider_box)
        self._paired_field(basic, 3, 1, '模型', 'model', factory=self.model_box)
        self._paired_field(basic, 4, 0, '请求速率 QPS', 'qps')
        self._paired_field(basic, 4, 1, '并发数量', 'poolSize')
        ttk.Label(basic, text='只显示已配置并启用的供应商；并发为 0 时跟随 QPS。', style='MutedCard.TLabel', wraplength=self.px(650)).grid(row=5, column=0, columnspan=2, sticky='w', pady=6)
        speed = ttk.Frame(basic, style='Card.TFrame'); speed.grid(row=6, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Button(speed, text='默认并发', command=lambda: self.apply_speed_preset(False)).pack(side='left')
        ttk.Button(speed, text='优化并发', command=lambda: self.apply_speed_preset(True)).pack(side='left', padx=8)
        ttk.Label(basic, textvariable=self.speed_hint, style='MutedCard.TLabel', wraplength=self.px(650)).grid(row=7, column=0, columnspan=2, sticky='w', pady=6)
        self._build_api_manager(f)
        newer = self._translation_card(f, 'pdf2zh_next 参数')
        self._paired_field(newer, 1, 0, '字体', 'fontFamily', ('auto', 'serif', 'sans-serif', 'script'))
        newer.columnconfigure(1, weight=1, uniform='translation-pair')
        self._balanced_checks(newer, ('transFirst', 'ocr', 'autoOcr', 'noWatermark', 'saveGlossary', 'disableGlossary',
                                    'skipClean', 'disableRichTextTranslate', 'enhanceCompatibility',
                                    'translateTableText', 'onlyIncludeTranslatedPage'))
        older = self._translation_card(f, 'pdf2zh 参数')
        self._field(older, 1, '翻译线程数', 'threadNum')
        self._field(older, 2, '字体文件路径', 'fontFile')
        for row, key in enumerate(('skipSubsetFonts', 'babeldoc'), start=3):
            ttk.Checkbutton(older, text=BOOL_LABELS[key], variable=self.translation_vars[key]).grid(row=row, column=0, columnspan=2, sticky='w', pady=4)
        self.output_section = ttk.Frame(f, style='TFrame')
        self.output_section.pack(fill='x', pady=self.px(8))
        for column in range(2):
            self.output_section.columnconfigure(column, weight=1, uniform='output-cards')
        outputs = self._translation_card(self.output_section, '完成后打开', pack=False)
        outputs.master.grid(row=0, column=0, sticky='nsew', padx=(0, self.px(8)))
        self.engine_sections = {'pdf2zh_next': newer.master, 'pdf2zh': older.master}
        self._engine_changed()
        self.open_output_controls = {}
        for row, (key, label, detail) in enumerate(OUTPUTS, start=1):
            control = ttk.Checkbutton(outputs, text=label, variable=self.translation_vars[key.replace('_', '-') + '-open'])
            control.grid(row=row, column=0, sticky='w', pady=4)
            self.open_output_controls[key] = control
        ttk.Checkbutton(outputs, text=BOOL_LABELS['showProgress'], variable=self.translation_vars['showProgress']).grid(row=7, column=0, sticky='w', pady=4)
        ttk.Label(outputs, text='输出格式和双语布局在开始页选择。\n桌面文件沿用原文件名；Zotero 附件重命名由插件负责。', style='MutedCard.TLabel',
                  wraplength=self.px(250), justify='left').grid(row=8, column=0, sticky='w', pady=6)
        crop = self._translation_card(self.output_section, '裁剪边距', pack=False)
        crop.master.grid(row=0, column=1, sticky='nsew', padx=(self.px(8), 0))
        self.crop_settings = crop
        for row, (key, label) in enumerate((('pdf_w_offset', '宽边裁剪值'), ('pdf_h_offset', '上下裁剪值'), ('pdf_offset_ratio', '宽窄边比例'), ('pdf_white_margin', '保留白边')), start=1):
            self._field(crop, row, label, key, width=14)
        ttk.Button(f, text='保存翻译设置', style='Accent.TButton', command=self._save_translation).pack(anchor='e', pady=12)

    def _build_task_tables(self, parent):
        card = self._card(parent, '任务')
        self.task_notebook = ttk.Notebook(card); self.task_notebook.pack(fill='both', expand=True)
        self.task_trees = {}
        for key, title in (('active', '翻译任务'), ('history', '历史任务')):
            frame = ttk.Frame(self.task_notebook, style='Card.TFrame'); self.task_notebook.add(frame, text=title)
            tree = ttk.Treeview(frame, columns=('file', 'status', 'progress', 'engine'), show='headings', height=7)
            for name, width, label in (('file', 360, '文件'), ('status', 90, '状态'), ('progress', 70, '进度'), ('engine', 135, '引擎')):
                tree.heading(name, text=label); tree.column(name, width=self.px(width), minwidth=self.px(60), anchor='w' if name == 'file' else 'center')
            sy = ttk.Scrollbar(frame, orient='vertical', command=tree.yview); sy.pack(side='right', fill='y')
            sx = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview); sx.pack(side='bottom', fill='x')
            tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
            tree.pack(fill='both', expand=True)
            tree.bind('<Button-3>', lambda event, current=tree: self.task_menu(event, current))
            tree.bind('<<TreeviewSelect>>', lambda event, current=tree: self._task_details(current))
            tree.bind('<Double-1>', lambda event, current=tree: self.open_task_output(current))
            self.task_trees[key] = tree
        self.task_tree = self.task_trees['active']
        self.task_notebook.bind('<<NotebookTabChanged>>', lambda _: self._task_details())
        self.task_detail_var = tk.StringVar(value='右键可暂停、继续或取消任务；双击历史任务打开结果。')
        ttk.Label(card, textvariable=self.task_detail_var, style='MutedCard.TLabel', wraplength=self.px(850)).pack(anchor='w', pady=7)
        row = ttk.Frame(card, style='Card.TFrame'); row.pack(fill='x')
        ttk.Button(row, text='刷新', command=self.refresh_tasks).pack(side='left')
        ttk.Button(row, text='打开输出目录', command=lambda: self._open_dir(__import__('app_paths').output_dir())).pack(side='left', padx=6)
        ttk.Label(row, text='实时刷新 · 历史保存在本项目', style='MutedCard.TLabel').pack(side='right')

    def task_menu(self, event, tree):
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)
        task = self._task_records.get(row, {})
        active = task.get('active') is True
        action = 'resume' if task.get('paused') or task.get('status') == 'paused' else 'pause'
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='继续' if action == 'resume' else '暂停', state='normal' if active else 'disabled', command=lambda: self.control_task(row, action))
        menu.add_command(label='取消任务', state='normal' if active else 'disabled', command=lambda: self.control_task(row, 'cancel'))
        if task.get('filePaths'):
            menu.add_separator(); menu.add_command(label='打开结果目录', command=lambda: self.open_task_output(tree))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def control_task(self, task_id, action):
        def done(_):
            self._tasks_revision = -1; self.refresh_tasks()
        self.jobs.submit('task-control-' + task_id, lambda: self.ctl._task_manager.act(task_id, action), done,
                         lambda exc: self._msg('任务操作失败', str(exc), error=True))

    def _task_details(self, tree=None):
        current = self.task_trees['active' if self.task_notebook.index('current') == 0 else 'history']
        if tree is not None and tree is not current:
            return
        tree = current
        selection = tree.selection()
        if not selection:
            self.task_detail_var.set('右键可暂停、继续或取消任务。' if self.task_notebook.index('current') == 0 else '请选择历史任务；双击可打开生成文件。')
            return
        task = self._task_records.get(selection[0], {})
        parts = [task.get('fileName', ''), '来源：' + ('桌面' if task.get('source') == 'desktop' else '插件')]
        if task.get('service'):
            parts.append('接口：' + task['service'])
        parts.extend(task.get('qualityWarnings', []))
        if self.translation_vars['showProgress'].get():
            parts.append(task.get('message') or task.get('error') or task.get('stage') or '')
            if task.get('stageSeconds'):
                parts.append('；'.join(f'{name}: {seconds:.1f}s' for name, seconds in task['stageSeconds'].items()))
        self.task_detail_var.set(' · '.join(str(part) for part in parts if part))

    def open_task_output(self, tree):
        selection = tree.selection()
        if selection:
            paths = self._task_records.get(selection[0], {}).get('filePaths') or []
            if paths:
                self._open_dir(Path(paths[0]).parent)
