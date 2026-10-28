import copy
import os
import unittest
from collections import Counter, defaultdict
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pymupdf
profile = Path(__file__).resolve().parents[1] / 'cache/tmp/reference-tests-profile'
profile.mkdir(parents=True,exist_ok=True)
with patch.dict(os.environ, {'USERPROFILE':str(profile)}), patch('pathlib.Path.home',return_value=profile):
    from babeldoc.format.pdf.document_il import (Box, Cropbox, Page, PdfCharacter, PdfFont,
        PdfFormula, PdfLine, PdfParagraph, PdfParagraphComposition, PdfSameStyleUnicodeCharacters,
        PdfStyle, VisualBbox)
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
from reference_layout import (_characters, _state, install_reference_layout, prepare_reference_page,
                              split_entries, merge_label_fragments, plan_reference_columns)


def paragraph(text, key='entry', x=54, top=700, size=9, label='plain text'):
    font = pymupdf.Font('helv')
    style = PdfStyle(font_id='base', font_size=size)
    lines = []
    for number, row in enumerate(text.split('\n')):
        cursor = x; chars = []
        for index, letter in enumerate(row):
            width = max(font.char_lengths(letter, size)[0], size*.3)
            box = Box(cursor,top-number*13-size,cursor+width,top-number*13)
            chars.append(PdfCharacter(pdf_style=style,box=copy.copy(box),visual_bbox=VisualBbox(box),
                char_unicode=letter,pdf_character_id=font.has_glyph(ord(letter)),advance=width,
                xobj_id=0,vertical=False,render_order=index))
            cursor += width
        lines.append(PdfLine(box=Box(x,top-number*13-size,cursor,top-number*13),pdf_character=chars))
    return PdfParagraph(box=Box(x,top-len(lines)*13,max(line.box.x2 for line in lines),top),
        pdf_style=style,pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=line) for line in lines],
        unicode=text,debug_id=key,first_line_indent=False,xobj_id=0,layout_label=label,render_order=0)


class ReferenceLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_reference_layout()

    def finder(self):
        finder = ParagraphFinder.__new__(ParagraphFinder)
        finder.translation_config = SimpleNamespace()
        return finder

    def typesetter(self, text, key='uri', tracked=True):
        font = pymupdf.Font('helv'); font.font_id = 'base'
        cjk = pymupdf.Font('china-s'); cjk.font_id = 'base'
        config = SimpleNamespace()
        typesetter = Typesetting.__new__(Typesetting)
        typesetter.translation_config = config; typesetter.is_cjk = True
        typesetter.font_mapper = SimpleNamespace(base_font=cjk,fontid2font={'base':font},
            map=lambda original, char: font if ord(char)<128 else cjk)
        style = PdfStyle(font_id='base',font_size=9)
        para = PdfParagraph(box=Box(0,0,210,700),pdf_style=style,debug_id=key,xobj_id=0,
            first_line_indent=False,pdf_paragraph_composition=[PdfParagraphComposition(
                pdf_same_style_unicode_characters=PdfSameStyleUnicodeCharacters(unicode=text,pdf_style=style))])
        if tracked:
            _state(config)['meta'][key]={'base_size':9,'left':0,'right':210,'page':0,'column':0}
        return typesetter,para,{'base':PdfFont(font_id='base')}

    def test_numbered_entries_split_before_translation_without_losing_any_characters(self):
        finder = self.finder()
        original = paragraph('[79] Author. Title. 2018年。 [80] Other author. 2023年。\n[81] Third author. 2025年。')
        before = Counter(char.char_unicode for char in _characters(original))
        result = split_entries(finder, original)
        self.assertEqual(len(result),3)
        self.assertEqual(Counter(char.char_unicode for item in result for char in _characters(item)),before)
        self.assertTrue(result[0].unicode.startswith('[79]'))
        self.assertTrue(result[1].unicode.startswith('[80]'))

    def test_inline_citations_without_a_dated_entry_are_not_split(self):
        original=paragraph('[1] A title mentioning [2] and symbols; no publication date here.')
        self.assertEqual(len(split_entries(self.finder(),original)),1)

    def test_detached_open_bracket_joins_only_its_matching_first_line(self):
        finder=self.finder()
        fragment=paragraph('[','bracket',x=54,top=698,size=7)
        entry=paragraph('10] Author. Title.\nJournal, 2025.','entry',x=58,top=701)
        other_column=paragraph('11] Another author. 2025.','other',x=318,top=701)
        expected=Counter(char.char_unicode for p in (fragment,entry,other_column) for char in _characters(p))
        result=merge_label_fragments(finder,[entry,fragment,other_column])
        self.assertEqual(len(result),2)
        self.assertRegex(entry.unicode,r'^\[\s*10\]')
        self.assertEqual(Counter(char.char_unicode for p in result for char in _characters(p)),expected)

    def test_bracket_on_a_different_line_is_not_attached_to_an_entry(self):
        fragment=paragraph('[','bracket',x=54,top=640,size=7)
        entry=paragraph('10] Author. 2025.','entry',x=58,top=701)
        self.assertEqual(len(merge_label_fragments(self.finder(),[entry,fragment])),2)

    def test_urls_wrap_without_scaling_ladders_or_changing_characters(self):
        address='https://www.example.org/'+('longcomponent'*28)+'?query=abc%20def&lang=zh#part'
        typesetter,para,fonts=self.typesetter(address)
        units=typesetter.create_typesetting_units(para,fonts)
        arranged,fits=typesetter._layout_typesetting_units(units,para.box,1.0,1.5,para)
        self.assertTrue(fits)
        self.assertEqual(''.join(unit.try_get_unicode() for unit in arranged),address)
        rows=defaultdict(int)
        for unit in arranged:
            rows[round(unit.y,4)]+=1
            self.assertLessEqual(unit.box.x2,para.box.x2+.01)
        counts=list(rows.values())
        self.assertGreater(len(counts),2)
        self.assertTrue(all(count>=2 for count in counts[:-1]))
        self.assertTrue(all(unit.font_size==9 for unit in arranged))

    def test_url_formula_becomes_text_but_real_math_is_preserved(self):
        address='https://example.org/papers/2025/very-long-reference'
        typesetter,para,fonts=self.typesetter(address)
        source=paragraph(address)
        chars=list(_characters(source))
        formula=PdfFormula(box=copy.copy(source.box),pdf_character=chars,x_offset=0,y_offset=0)
        para.pdf_paragraph_composition=[PdfParagraphComposition(pdf_formula=formula)]
        units=typesetter.create_typesetting_units(para,fonts)
        self.assertEqual(''.join(unit.try_get_unicode() for unit in units),address)
        self.assertFalse(any(unit.formular for unit in units))
        math_source=paragraph('x+y')
        math=PdfFormula(box=copy.copy(math_source.box),pdf_character=list(_characters(math_source)),x_offset=0,y_offset=0)
        para.pdf_paragraph_composition=[PdfParagraphComposition(pdf_formula=math)]
        self.assertIs(typesetter.create_typesetting_units(para,fonts)[0].formular,math)

    def test_body_units_and_their_line_break_rules_are_unchanged(self):
        typesetter,para,fonts=self.typesetter('https://example.org/somewhere',tracked=False)
        units=typesetter.create_typesetting_units(para,fonts)
        self.assertFalse(hasattr(units[0],'_pdf2zh_uri'))
        self.assertFalse(units[0].can_break_line)

    def test_formula_only_reference_retains_its_box_and_glyphs_without_font_statistics(self):
        typesetter,para,fonts=self.typesetter('unused')
        source=paragraph('[','uri',x=54,top=190,size=7)
        chars=list(_characters(source))
        formula=PdfFormula(box=copy.copy(source.box),pdf_character=chars,x_offset=0,y_offset=0)
        original=copy.deepcopy(formula)
        para.pdf_paragraph_composition=[PdfParagraphComposition(pdf_formula=formula)]
        meta=_state(typesetter.translation_config)['meta']['uri']
        meta['original_box']=(source.box.x,source.box.y,source.box.x2,source.box.y2)
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=[para],pdf_font=[PdfFont(font_id='base')])
        with patch.object(typesetter,'_layout_typesetting_units',side_effect=AssertionError('formula must not enter text measurement')):
            plan_reference_columns(typesetter,page)
            typesetter.render_paragraph(para,page,fonts)
        self.assertEqual(para.box,source.box)
        self.assertEqual(para.pdf_paragraph_composition[0].pdf_formula,original)

    def test_empty_page_does_not_trigger_font_statistics(self):
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=[])
        prepare_reference_page(self.finder(),page)
        self.assertEqual(page.pdf_paragraph,[])

    def test_algorithm_page_after_references_is_not_reflowed(self):
        finder=self.finder(); _state(finder.translation_config)['active']=True
        diagram=[paragraph('Construction: Example protocol.','protocol'),
                 paragraph('ClientSetup(parameters) -> public values','algorithm',top=650)]
        caption=paragraph('Appendix Figure A1: Protocol construction.','caption',top=100,label='abandon')
        before=copy.deepcopy(diagram+[caption])
        page=Page(page_number=19,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=diagram+[caption])
        prepare_reference_page(finder,page)
        self.assertEqual(page.pdf_paragraph,before)
        self.assertFalse(_state(finder.translation_config)['meta'])

    def test_scope_stops_at_appendix_with_pdf_encoded_spaces(self):
        references=[paragraph(f'[{87+i}] Author. Title. 2025. https://example.org/{i}',str(i),top=710-i*90) for i in range(3)]
        title=paragraph('A(cid:1)Appendix','appendix',top=400,size=12,label='title')
        title.pdf_style=None  # ParagraphFinder has not assigned paragraph styles yet.
        body=paragraph('Body content and equations remain here.','body',top=365)
        right=[paragraph('Right column body.','right'+str(i),x=318,top=700-i*90) for i in range(3)]
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=references+[title,body]+right)
        original=copy.deepcopy(body); finder=self.finder()
        prepare_reference_page(finder,page)
        meta=_state(finder.translation_config)['meta']
        self.assertNotIn('appendix',meta); self.assertNotIn('body',meta)
        self.assertTrue(all(key not in meta for key in ('right0','right1','right2')))
        self.assertEqual(next(p for p in page.pdf_paragraph if p.debug_id=='body'),original)
        self.assertTrue(all(value['right']-value['left']>=60 for value in meta.values()))

    def test_inline_body_citations_do_not_start_reference_mode(self):
        body=paragraph('A discussion of papers [1], [2], [3] from 2023 and 2024; https://example.org.','body')
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=[body])
        finder=self.finder(); before=copy.deepcopy(body)
        prepare_reference_page(finder,page)
        self.assertFalse(_state(finder.translation_config)['meta'])
        self.assertEqual(page.pdf_paragraph,[before])

    def test_body_above_reference_heading_is_not_reflowed(self):
        body=paragraph('Concluding remarks before the bibliography.','conclusion',top=740)
        heading=paragraph('References','heading',top=690,size=12,label='title')
        references=[paragraph(f'[{i+1}] Author. Title. 2025. https://example.org/{i}',str(i),top=650-i*90) for i in range(3)]
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=[body,heading]+references)
        finder=self.finder(); before=copy.deepcopy(body)
        prepare_reference_page(finder,page)
        meta=_state(finder.translation_config)['meta']
        self.assertNotIn('conclusion',meta); self.assertNotIn('heading',meta)
        self.assertTrue(all(str(i) in meta for i in range(3)))
        self.assertEqual(next(p for p in page.pdf_paragraph if p.debug_id=='conclusion'),before)

    def test_signature_starts_at_first_entry_instead_of_preceding_body(self):
        body=paragraph('Concluding remarks without a printed reference heading.','conclusion',top=740)
        references=[paragraph(f'[{i+1}] Author. Title. 2025. https://example.org/{i}',str(i),top=650-i*90) for i in range(3)]
        page=Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),pdf_paragraph=[body]+references)
        finder=self.finder()
        prepare_reference_page(finder,page)
        self.assertNotIn('conclusion',_state(finder.translation_config)['meta'])
        self.assertTrue(all(str(i) in _state(finder.translation_config)['meta'] for i in range(3)))

    def test_url_idn_does_not_insert_mixed_script_spacing(self):
        typesetter,para,fonts=self.typesetter('https://示例.中国/路径?键=值')
        units=typesetter.create_typesetting_units(para,fonts)
        self.assertTrue(all(not unit.is_cjk_char for unit in units))

    def test_original_pdf_glyphs_reflow_with_font_height_and_still_render(self):
        typesetter,_,fonts=self.typesetter('unused')
        para=paragraph('a'*100,'uri',x=0,top=600)
        para.box=Box(0,0,100,600)
        for char in _characters(para):
            char.visual_bbox.box.y2=char.visual_bbox.box.y+3
        units=typesetter.create_typesetting_units(para,fonts)
        self.assertTrue(all(unit.height>=9 for unit in units))
        arranged,fits=typesetter._layout_typesetting_units(units,para.box,1,1.5,para,False)
        self.assertTrue(fits)
        self.assertTrue(all(unit.render()[0] for unit in arranged))
        rows=sorted({unit.y for unit in arranged},reverse=True)
        self.assertTrue(all(a-b>=13.49 for a,b in zip(rows,rows[1:])))
