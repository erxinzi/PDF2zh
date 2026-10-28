import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.reasoning import reasoning_spec, apply_reasoning_control
from utils.config import Config
from utils.deepseek_thinking import normalize_deepseek_extra_data, prepare_deepseek_runtime_command


class ReasoningControlsTests(unittest.TestCase):
    def test_explicit_deepseek_effort_enables_thinking_and_preserves_other_quality_parameters(self):
        spec = reasoning_spec('deepseek', 'deepseek-v4-pro')
        original = {'deepseek_enable_json_mode': False, 'custom_hint': 'keep-me'}
        changed = apply_reasoning_control(original, spec, 'effort', 'max')
        self.assertEqual(changed['deepseek_thinking_mode'], 'enabled')
        self.assertEqual(changed['deepseek_reasoning_effort'], 'max')
        self.assertFalse(changed['deepseek_enable_json_mode'])
        self.assertEqual(changed['custom_hint'], 'keep-me')
        self.assertNotIn('deepseek_thinking_mode', original)
        off = apply_reasoning_control(changed, spec, 'mode', 'disabled')
        self.assertNotIn('deepseek_reasoning_effort', off)
        self.assertEqual(off['deepseek_thinking_mode'], 'disabled')

    def test_openai_and_compatible_effort_sets_native_send_gate(self):
        for service, prefix in (('openai', 'openai'), ('openailiked', 'openai_compatible')):
            changed = apply_reasoning_control({}, reasoning_spec(service, 'reasoning-model'), 'effort', 'high')
            self.assertEqual(changed[prefix + '_reasoning_effort'], 'high')
            self.assertIs(changed[prefix + '_send_reasoning_effort'], True)

    def test_silicon_thinking_mode_sets_native_gate_and_keeps_false_as_boolean(self):
        changed = apply_reasoning_control({}, reasoning_spec('silicon', 'some-model'), 'mode', 'false')
        self.assertIs(changed['siliconflow_enable_thinking'], False)
        self.assertIs(changed['siliconflow_send_enable_thinking_param'], True)

    def test_native_deepseek_command_receives_user_selected_effort(self):
        extra = apply_reasoning_control({}, reasoning_spec('deepseek', 'deepseek-v4-pro'), 'effort', 'max')
        profile = {'model': 'deepseek-v4-pro', 'extraData': extra}
        normalize_deepseek_extra_data(profile, {})
        import tempfile, toml
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            config.write_text(toml.dumps({'deepseek_detail': {'deepseek_model': profile['model'], **profile['extraData']}}), encoding='utf-8')
            command = ['pdf2zh_next', 'paper.pdf', '--deepseek', '--config-file', str(config)]
            with patch('utils.deepseek_thinking._runtime_supports_thinking', return_value=True):
                prepared = prepare_deepseek_runtime_command(command, {})
            self.assertEqual(prepared[prepared.index('--deepseek-thinking-mode') + 1], 'enabled')
            self.assertEqual(prepared[prepared.index('--deepseek-reasoning-effort') + 1], 'max')


if __name__ == '__main__':
    unittest.main()
