"""Resolve parameter-only plugin jobs against the backend's saved settings."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from utils.desktop_translation import load_options, read_profiles
from utils.llm_api import ApiRegistry

BACKEND_PROTOCOL = 'backend-v1'
PLUGIN_PARAMETERS = frozenset({
    'skipLastPages', 'transFirst', 'fontFamily', 'dualMode', 'ocr', 'autoOcr',
    'noWatermark', 'saveGlossary', 'disableGlossary', 'noDual', 'noMono',
    'skipClean', 'disableRichTextTranslate', 'enhanceCompatibility',
    'translateTableText', 'onlyIncludeTranslatedPage',
    'mono', 'dual', 'mono_cut', 'dual_cut', 'crop_compare', 'compare',
})


class BackendSettingsError(ValueError):
    pass


def resolve_plugin_settings(data, home):
    root = Path(home)
    path = root / 'settings.json'
    try:
        settings = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        if not isinstance(settings, dict):
            raise ValueError('settings.json 应为 JSON 对象')
        options = load_options(settings)
        engine = options['engine']
        if engine not in ('pdf2zh', 'pdf2zh_next'):
            raise ValueError('后端保存的翻译引擎无效')
        directory = root / 'config'
        registry = ApiRegistry(directory / 'llm_apis.json').load_or_migrate(settings, read_profiles(directory))
        selected = registry.selected_entry(engine)
        if selected is None:
            raise ValueError('请在 EXE 的“设置 · 翻译”中选择已配置并启用的供应商，然后保存设置')
        options['service' if engine == 'pdf2zh' else 'next_service'] = selected['service']
        options['llm_api'] = {key: copy.deepcopy(selected[key]) for key in ('apiKey', 'apiUrl', 'model', 'extraData')}
        for key in PLUGIN_PARAMETERS:
            if key in data:
                options[key] = copy.deepcopy(data[key])
        if engine == 'pdf2zh_next':
            from utils.config import stringToBoolean
            need_mono = any(stringToBoolean(options.get(key)) for key in ('mono', 'mono_cut'))
            need_dual = any(stringToBoolean(options.get(key)) for key in ('dual', 'dual_cut', 'crop_compare', 'compare'))
            if need_mono or need_dual:
                if not need_mono:
                    options['noMono'] = True
                if not need_dual:
                    options['noDual'] = True
        # An explicit protocol flag prevents old clients from silently changing
        # configuration precedence; file payloads are processed separately.
        return options
    except (OSError, ValueError) as exc:
        raise BackendSettingsError('无法使用后端翻译设置：' + str(exc)) from None
