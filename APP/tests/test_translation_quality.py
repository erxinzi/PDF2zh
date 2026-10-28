import sys
import unittest
from unittest.mock import patch

from utils.process_utils import stream_process
from utils.translation_quality import TranslationQuality
from unittest.mock import Mock
from utils.task_manager import TaskManager
from utils.ui_jobs import task_rows


class TranslationQualityTests(unittest.TestCase):
    def test_missing_glyph_warning_is_shown_as_quality_warning_without_a_translation_error(self):
        manager=Mock();quality=TranslationQuality('layout',manager)
        quality.feed('WARNING Layout integrity warning: 第 12 页含 85 个空字形')
        fields=manager.update_task.call_args.args[1]
        self.assertIn('85',fields['qualityWarnings'][0])
        self.assertEqual(fields['translationErrorCount'],0)

    def test_successful_child_with_permanent_failures_keeps_warnings_in_history(self):
        manager = TaskManager()
        manager.add_task('quality', {'taskId': 'quality', 'active': True, 'status': 'running', 'progress': 0})
        code = "print('WARNING Retrying translation after HTTP 500'); print('ERROR Error translating paragraph. Paragraph: abc (text). Error: RetryError'); print('ERROR Error translating paragraph. Paragraph: abc (text). Error: RetryError'); print('WARNING Error during automatic terms extract: invalid JSON')"
        with patch('utils.task_manager.task_manager', manager):
            result = stream_process([sys.executable, '-c', code], task_id='quality')
        self.assertEqual(result.returncode, 0)
        task = manager.get_active_tasks_list()[0]
        self.assertEqual(task['translationErrorCount'], 1)
        self.assertEqual(len(task['qualityWarnings']), 2)
        manager.complete_task('quality', 'success', file_list=['out.pdf'])
        history = manager.get_history()[0]
        self.assertEqual(history['translationErrorCount'], 1)
        self.assertIn('有警告', task_rows([], [history])[0][1][1])

    def test_recovered_http_retry_does_not_report_a_permanent_translation_failure(self):
        manager = TaskManager()
        manager.add_task('retry', {'taskId': 'retry', 'active': True, 'progress': 0})
        with patch('utils.task_manager.task_manager', manager):
            stream_process([sys.executable, '-c', "print('WARNING retrying request after HTTP 500'); print('INFO request succeeded')"], task_id='retry')
        self.assertFalse(manager.get_active_tasks_list()[0].get('qualityWarnings'))
