import copy
from collections import Counter
from types import SimpleNamespace
import unittest
from test_reference_layout import paragraph
from babeldoc.format.pdf.document_il import (Box,Cropbox,Page,PdfCurve,GraphicState,
    PdfParagraphComposition,PdfStyle,PdfCharacter)
from babeldoc.format.pdf.document_il.backend.pdf_creater import RenderContext,CharacterRenderUnit
from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
from layout_integrity import (install_layout_integrity,preserve_algorithm_rows,protect_vector_semantics,
                              finish_vector_semantics,_characters,DECOR)


class LayoutIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): install_layout_integrity()

    def page(self,paragraphs,curves=()):
        return Page(page_number=0,cropbox=Cropbox(Box(0,0,612,792)),
                    pdf_paragraph=paragraphs,pdf_curve=list(curves))

    def curve(self,x,y,x2):
        return PdfCurve(box=Box(x,y,x2,y),graphic_state=GraphicState('0 g 0 G'),stroke_path=True,xobj_id=0)

    def test_xobject_encoding_shadows_page_font_with_same_resource_name(self):
        page=SimpleNamespace(pdf_xobject=[SimpleNamespace(xobj_id=7,pdf_font=[SimpleNamespace(font_id='F1',encoding_length=2)])])
        context=RenderContext(pdf_creator=SimpleNamespace(render_graphic_state=lambda *args:None),page=page,
            available_font_list={'F1'},page_encoding_length_map={'F1':1},all_encoding_length_map={'F1':1},
            xobj_available_fonts={7:{'F1'}},xobj_encoding_length_map={7:{'F1':1}},ctm_for_ops=b'',check_font_exists=False)
        char=PdfCharacter(char_unicode='1',pdf_character_id=49,pdf_style=PdfStyle(font_id='F1',font_size=9),
            box=Box(0,0,5,9),xobj_id=7,vertical=True)
        output=[];CharacterRenderUnit(char,0).render(output,context)
        self.assertIn(b'<0031>',b''.join(output))
        self.assertEqual(context.page_encoding_length_map['F1'],1)

    def test_fraction_glyphs_and_vector_rule_form_one_atomic_unit(self):
        top=paragraph('12','numerator',x=54,top=514,size=6)
        bottom=paragraph('34','denominator',x=54,top=503,size=6)
        chars=[c for p in (top,bottom) for comp in p.pdf_paragraph_composition for c in _characters(comp)]
        curve=self.curve(53.5,505,max(c.box.x2 for c in chars)+.5)
        page=self.page([top,bottom],[curve])
        expected=Counter(c.char_unicode for c in chars)
        attachments,_=protect_vector_semantics(page)
        finish_vector_semantics(page,attachments)
        formulas=[comp.pdf_formula for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition if comp.pdf_formula]
        self.assertEqual(len(formulas),1)
        self.assertEqual(Counter(c.char_unicode for c in formulas[0].pdf_character),expected)
        self.assertEqual(formulas[0].pdf_curve,[curve])
        self.assertTrue(all(c.formula_layout_id for c in formulas[0].pdf_character))

    def test_font_accent_and_variable_are_not_split_into_translatable_words(self):
        source=paragraph('\u00afDB,bkt','variable')
        page=self.page([source]);attachments,_=protect_vector_semantics(page)
        finish_vector_semantics(page,attachments)
        formula=source.pdf_paragraph_composition[0].pdf_formula
        self.assertIsNotNone(formula)
        self.assertEqual(''.join(c.char_unicode for c in formula.pdf_character),'\u00afDB')

    def test_underline_moves_with_rendered_characters(self):
        source=paragraph('important','decor',x=54,top=500,size=10)
        chars=[c for comp in source.pdf_paragraph_composition for c in _characters(comp)]
        curve=self.curve(chars[0].box.x,chars[0].box.y-1,chars[-1].box.x2)
        page=self.page([source],[curve]);attachments,decorations=protect_vector_semantics(page)
        self.assertEqual(decorations,1);self.assertFalse(attachments)
        style=chars[0].pdf_style
        self.assertIsNotNone(DECOR.search(style.graphic_state.passthrough_per_char_instruction))
        translated=PdfCharacter(char_unicode='x',pdf_character_id=1,pdf_style=style,
            box=Box(200,300,212,310),advance=12,xobj_id=0,vertical=False)
        context=SimpleNamespace(xobj_encoding_length_map={},page_encoding_length_map={style.font_id:1},
            all_encoding_length_map={},check_font_exists=False,
            pdf_creator=SimpleNamespace(render_graphic_state=lambda *args:None))
        output=[];CharacterRenderUnit(translated,0).render(output,context)
        self.assertIn(b'200.000000 299.000000 m 212.000000 299.000000 l',b''.join(output))

    def test_strike_style_is_retained_instead_of_a_fixed_original_line(self):
        source=paragraph('remove','strike',x=54,top=500,size=10)
        chars=[c for comp in source.pdf_paragraph_composition for c in _characters(comp)]
        curve=self.curve(chars[0].box.x,chars[0].box.y+5,chars[-1].box.x2)
        page=self.page([source],[curve]);_,decorations=protect_vector_semantics(page)
        self.assertEqual(decorations,1)
        self.assertEqual(page.pdf_curve,[])
        self.assertTrue(all(DECOR.search(c.pdf_style.graphic_state.passthrough_per_char_instruction) for c in chars))

    def test_plot_legend_rules_are_not_bound_to_nearby_text(self):
        source=paragraph('DB','plot',x=54,top=500,size=10)
        for comp in source.pdf_paragraph_composition:
            for char in _characters(comp):char.xobj_id=7
        curve=self.curve(54,503,67);curve.xobj_id=7
        page=self.page([source],[curve]);attachments,decorations=protect_vector_semantics(page)
        self.assertFalse(attachments);self.assertEqual(decorations,0)
        self.assertEqual(page.pdf_curve,[curve])

    def test_algorithm_steps_retain_separate_rows_and_all_characters(self):
        heading=paragraph('Algorithm 3 Query procedure','heading',x=54,top=730,size=10)
        body=paragraph('1: Input the ciphertext.\n2: Return the result.','body',x=60,top=700,size=8)
        page=self.page([heading,body],[self.curve(54,729.5,300),self.curve(54,716,300),self.curve(54,650,300)])
        finder=ParagraphFinder.__new__(ParagraphFinder)
        expected=Counter(c.char_unicode for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition for c in _characters(comp))
        preserve_algorithm_rows(finder,page)
        rows=[p for p in page.pdf_paragraph if p.layout_label=='algorithm']
        self.assertEqual(len(rows),2)
        self.assertTrue(rows[0].unicode.startswith('1:'));self.assertTrue(rows[1].unicode.startswith('2:'))
        self.assertEqual(Counter(c.char_unicode for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition for c in _characters(comp)),expected)
