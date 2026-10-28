import contextlib
import io
import logging
import statistics
import sys
import unittest
from unittest.mock import patch

from runtime_bootstrap import logged_progress_handler
from runtime_progress import ProgressEmitter, decode_progress
from utils import execute
from utils.process_utils import KernelTranslationError, stream_process
from utils.task_manager import TaskManager


class KernelFailureTests(unittest.TestCase):
    def test_fatal_event_survives_progress_throttling_and_retains_exception_type(self):
        output=io.StringIO()
        emitter=ProgressEmitter(output,clock=lambda:10)
        emitter({'type':'progress_update','overall_progress':92.44,'stage':'Typesetting'})
        emitter({'type':'error','error':statistics.StatisticsError('no mode for empty data')})
        events=[decode_progress(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(events[-1]['type'],'error')
        self.assertEqual(events[-1]['errorType'],'StatisticsError')
        self.assertEqual(events[-1]['progress'],92.44)
        self.assertEqual(events[-1]['stage'],'Typesetting')

    def test_bootstrap_forwards_fatal_event_to_parent_pipe(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertLogs('pdf2zh.acceleration',logging.ERROR):
            _,handle=logged_progress_handler(None,None)
            handle({'type':'error','error':RuntimeError('native layout failed')})
        event=decode_progress(output.getvalue().strip())
        self.assertEqual(event['error'],'native layout failed')
        self.assertEqual(event['errorType'],'RuntimeError')

    def test_zero_exit_fatal_event_reaches_api_and_persisted_task_history(self):
        from flask import Flask
        from server import PDFTranslator
        manager=TaskManager(); manager.add_task('failure',{'active':True,'progress':92.44,'stage':'Typesetting'})
        output=io.StringIO()
        ProgressEmitter(output)({'type':'error','error':'no mode for empty data','errorType':'StatisticsError'})
        line=output.getvalue().strip()
        command=[sys.executable,'-u','-c',f'print({line!r})']
        with patch.object(execute,'task_manager',manager),self.assertRaises(KernelTranslationError) as raised:
            stream_process(command,on_line=lambda text:execute._parse_progress(text,'failure',structured_only=True))
        self.assertEqual(raised.exception.kernel_exit_code,0)
        translator=PDFTranslator.__new__(PDFTranslator); translator.app=Flask('failure-test')
        payload=translator._exception_payload(raised.exception)
        self.assertEqual(payload['errorType'],'StatisticsError')
        self.assertIn('no mode for empty data',payload['message'])
        manager.complete_task('failure','failed',payload['message'],error=payload['message'])
        self.assertIn('StatisticsError: no mode for empty data',manager.progress_history[0]['error'])

    def test_error_level_log_without_fatal_event_does_not_abort_recovered_job(self):
        result=stream_process([sys.executable,'-c',"print('ERROR temporary request failed'); print('INFO retry succeeded')"])
        self.assertEqual(result.returncode,0)
