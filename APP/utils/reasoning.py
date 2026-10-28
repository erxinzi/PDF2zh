"""Translate explicit reasoning controls into native kernel extraData fields."""
from __future__ import annotations

import copy
from utils.llm_api import canonical_service


def reasoning_spec(service, model):
    service = canonical_service(service)
    if service == 'deepseek' and str(model).startswith('deepseek-v4-'):
        return {'mode': 'deepseek_thinking_mode', 'effort': 'deepseek_reasoning_effort',
                'mode_label': '思考模式', 'mode_values': [('disabled', '关闭'), ('enabled', '开启')],
                'effort_values': ['high', 'max'], 'kind': 'deepseek'}
    if service in ('openai', 'openailiked'):
        prefix = 'openai' if service == 'openai' else 'openai_compatible'
        return {'mode': prefix + '_send_reasoning_effort', 'effort': prefix + '_reasoning_effort',
                'mode_label': '发送思考强度参数', 'mode_values': [('true', '发送'), ('false', '不发送')],
                'effort_values': ['minimal', 'low', 'medium', 'high'], 'kind': 'boolean'}
    if service == 'silicon':
        return {'mode': 'siliconflow_enable_thinking', 'effort': None, 'mode_label': '思考模式',
                'mode_values': [('true', '开启'), ('false', '关闭')], 'effort_values': [], 'kind': 'boolean',
                'send_gate': 'siliconflow_send_enable_thinking_param'}
    return None


def apply_reasoning_control(extra, spec, control, value):
    if not isinstance(extra, dict):
        raise ValueError('附加参数应为 JSON 对象')
    result = copy.deepcopy(extra)
    field = spec[control]
    if not field:
        raise ValueError('此接口没有思考强度参数')
    if value == '':
        result.pop(field, None)
        return result
    allowed = [key for key, _ in spec['mode_values']] if control == 'mode' else spec['effort_values']
    if value not in allowed:
        raise ValueError('请选择此接口支持的思考选项')
    result[field] = (value == 'true') if control == 'mode' and spec['kind'] == 'boolean' else value
    if control == 'effort':
        result[spec['mode']] = 'enabled' if spec['kind'] == 'deepseek' else True
    elif spec['kind'] == 'deepseek' and value == 'disabled':
        result.pop(spec['effort'], None)
    if spec.get('send_gate'):
        result[spec['send_gate']] = True
    return result
