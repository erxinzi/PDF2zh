"""Retain permanent kernel translation failures even if it exits successfully."""
import re


class TranslationQuality:
    def __init__(self, task_id, manager):
        self.task_id, self.manager = task_id, manager
        self.paragraphs, self.terms = set(), set()
        self.layout = set()

    def feed(self, line):
        paragraph = re.search(r'Error translating paragraph\. Paragraph: (.*?)\. Error:', line)
        term = 'Error during automatic terms extract:' in line
        layout = line.split('Layout integrity warning:',1)[1].strip() if 'Layout integrity warning:' in line else None
        if not paragraph and not term and not layout:
            return
        before = len(self.paragraphs), len(self.terms), len(self.layout)
        if paragraph:
            self.paragraphs.add(paragraph.group(1))
        if term:
            self.terms.add(line.split('Error during automatic terms extract:', 1)[1].strip())
        if layout:
            self.layout.add(layout)
        if before == (len(self.paragraphs), len(self.terms), len(self.layout)):
            return
        warnings = []
        if self.paragraphs:
            warnings.append(f'{len(self.paragraphs)} 段文本翻译失败，生成文件可能含有未译原文，请检查或重新翻译')
        if self.terms:
            warnings.append('自动术语提取出现错误，术语表可能不完整')
        warnings.extend('PDF 内容完整性检查：'+message for message in sorted(self.layout))
        self.manager.update_task(self.task_id, {'qualityWarnings': warnings, 'translationErrorCount': len(self.paragraphs)})
