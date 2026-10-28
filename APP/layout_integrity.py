"""Preserve scoped font encodings, structured rows and vector text semantics."""
from __future__ import annotations
from collections import Counter
import copy
from functools import wraps
import json
import logging
import re
import statistics

LOG=logging.getLogger('pdf2zh.layout')
DECOR=re.compile(r'%PDF2ZH_DECOR:(\{[^\n]+\})')


def _box(char):
    return char.visual_bbox.box if char.visual_bbox and char.visual_bbox.box else char.box


def _characters(composition):
    if composition.pdf_line: return composition.pdf_line.pdf_character
    if composition.pdf_character: return [composition.pdf_character]
    if composition.pdf_same_style_characters: return composition.pdf_same_style_characters.pdf_character
    if composition.pdf_formula: return composition.pdf_formula.pdf_character
    return []


def _text(chars):
    return re.sub(r'\(cid:[13]\)',' ',''.join(c.char_unicode or '' for c in chars)).replace('\x01',' ')


def _update_line(line):
    from babeldoc.format.pdf.document_il import Box
    boxes=[_box(c) for c in line.pdf_character]
    line.box=Box(min(b.x for b in boxes),min(b.y for b in boxes),max(b.x2 for b in boxes),max(b.y2 for b in boxes))


def _horizontal(page):
    return [c for c in page.pdf_curve if c.box and c.box.x2-c.box.x>1 and abs(c.box.y2-c.box.y)<.8]


def preserve_algorithm_rows(finder,page):
    """Split a multi-step algorithm paragraph into logical rows inside its rules."""
    from babeldoc.format.pdf.document_il import Box, PdfParagraphComposition
    rules=_horizontal(page)
    regions=[]
    for paragraph in page.pdf_paragraph:
        for comp in paragraph.pdf_paragraph_composition:
            if not comp.pdf_line: continue
            line=comp.pdf_line
            if not re.match(r'^\s*(?:Algorithm|算法)\s*\d+',_text(line.pdf_character)): continue
            widths=[c for c in rules if c.box.x<=line.box.x+3 and c.box.x2>=line.box.x2-3 and c.box.x2-c.box.x>120]
            size=statistics.median(c.pdf_style.font_size for c in line.pdf_character if c.pdf_style and c.pdf_style.font_size)
            top=[c for c in widths if -size*.35<=c.box.y-line.box.y2<=size*1.5]
            below=sorted((c for c in widths if c.box.y<line.box.y),key=lambda c:-c.box.y)
            if top and len(below)>=2 and line.box.y-below[0].box.y<size*1.5:
                regions.append(Box(below[0].box.x,below[1].box.y,below[0].box.x2,below[0].box.y))
    changed=0; result=[]
    for paragraph in page.pdf_paragraph:
        lines=[c.pdf_line for c in paragraph.pdf_paragraph_composition if c.pdf_line]
        if len(lines)!=len(paragraph.pdf_paragraph_composition) or len(lines)<2:
            result.append(paragraph); continue
        region=next((r for r in regions if paragraph.box and paragraph.box.x>=r.x-3 and paragraph.box.x2<=r.x2+3
                     and paragraph.box.y>=r.y-3 and paragraph.box.y2<=r.y2+3),None)
        if not region or sum(bool(re.match(r'^\s*\d{1,3}\s*:',_text(line.pdf_character))) for line in lines)<2:
            result.append(paragraph); continue
        groups=[]
        for line in lines:
            text=_text(line.pdf_character).lstrip()
            start=bool(re.match(r'^(?:\d{1,3}\s*:|/\*|Run\s*by\b|procedure\b)',text,re.I))
            if not groups or start: groups.append([])
            groups[-1].append(line)
        for index,group in enumerate(groups):
            item=copy.copy(paragraph)
            item.debug_id=f'{paragraph.debug_id}.row{index}'
            item.pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=line) for line in group]
            finder.update_paragraph_data(item,update_unicode=True)
            item.first_line_indent=False
            item.layout_label='algorithm'
            result.append(item)
        changed+=1
    if changed:
        before=Counter(c.char_unicode for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition for c in _characters(comp))
        after=Counter(c.char_unicode for p in result for comp in p.pdf_paragraph_composition for c in _characters(comp))
        if before!=after: raise RuntimeError('算法行拆分的字符完整性检查失败')
        page.pdf_paragraph=result
        finder._set_paragraph_render_order(page)
        LOG.info('算法行结构已保留：页 %s，拆分 %s 个合并段落',page.page_number+1,changed)


def _math(chars):
    text=re.sub(r'\s+','',_text(chars))
    return bool(text) and not re.search(r'[A-Za-z]{5,}|[\u3400-\u9fff]',text)


def _nearest_row(chars,y,size):
    if not chars: return []
    anchor=min(chars,key=lambda c:abs((_box(c).y+_box(c).y2)/2-y))
    center=(_box(anchor).y+_box(anchor).y2)/2
    return [c for c in chars if abs((_box(c).y+_box(c).y2)/2-center)<=size*.7]


def protect_vector_semantics(page):
    """Build atomic fractions/variables before native style classification."""
    from babeldoc.format.pdf.document_il import PdfFormula, PdfLine, PdfParagraphComposition, PdfStyle, GraphicState
    from babeldoc.format.pdf.document_il.utils.formular_helper import update_formula_data
    all_chars=[c for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition for c in _characters(comp)]
    drawable=[c for c in all_chars if c.pdf_character_id is not None and c.pdf_style and c.pdf_style.font_size and c.char_unicode and not c.char_unicode.isspace()]
    groups=[]; removed=set(); decorations=0
    # TeX accents are often actual glyphs, not vector strokes (e.g. U+00AF DB).
    # Preserve their relative glyph positions and prevent variables becoming words.
    accents={'\u00af','\u203e','\u0304','\u0305','\u0332','\u0333','\u0335','\u0336','\u0302','\u0303','\u0307'}
    for paragraph in page.pdf_paragraph:
        for comp in paragraph.pdf_paragraph_composition:
            if not comp.pdf_line: continue
            chars=comp.pdf_line.pdf_character
            for index,accent in enumerate(chars):
                if accent.char_unicode not in accents: continue
                base=[]
                for char in chars[index+1:]:
                    value=char.char_unicode or ''
                    if not base and value.isspace(): continue
                    if not re.fullmatch('[A-Za-z0-9_]',value): break
                    if abs(_box(char).x-_box(accent).x)>accent.pdf_style.font_size*2: break
                    base.append(char)
                if base and len(base)<=4:
                    groups.append({'ids':{id(c) for c in [accent]+base},'curves':[]})
    for curve in _horizontal(page):
        # Embedded plots use the same primitives for error bars and legend marks.
        # Their font scopes are repaired independently; chart marks stay anchored.
        if curve.xobj_id not in (None,0): continue
        b=curve.box; y=(b.y+b.y2)/2; width=b.x2-b.x
        nearby=[c for c in drawable if c.xobj_id==curve.xobj_id and _box(c).x>=b.x-.8 and _box(c).x2<=b.x2+.8
                and abs((_box(c).y+_box(c).y2)/2-y)<=c.pdf_style.font_size*1.8]
        if not nearby: continue
        size=statistics.median(c.pdf_style.font_size for c in nearby)
        above=_nearest_row([c for c in nearby if _box(c).y>y+.1 and _box(c).y-y<size*1.5],y,size)
        below=_nearest_row([c for c in nearby if _box(c).y2<y-.1 and y-_box(c).y2<size*1.5],y,size)
        crossing=[c for c in nearby if _box(c).y<=y+.1 and _box(c).y2>=y-.1]
        selected=[]
        if width<=size*18 and above and below and _math(above) and _math(below):
            selected=above+below
        elif width<=size*5 and below and not crossing:
            compact=re.sub(r'\s+','',_text(sorted(below,key=lambda c:_box(c).x)))
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,3}',compact): selected=below
        if selected:
            ids={id(c) for c in selected}
            shared=[g for g in groups if g['ids']&ids]
            group={'ids':ids,'curves':[curve]}
            for old in shared:
                group['ids'].update(old['ids']);group['curves'].extend(old['curves']);groups.remove(old)
            groups.append(group);removed.add(id(curve));continue
        # Natural-language underlines/strikes become character style markers.
        # A rule must tightly span the painted text, so table borders stay fixed.
        candidates=crossing or (above if not below else [])
        if not candidates: continue
        span=max(_box(c).x2 for c in candidates)-min(_box(c).x for c in candidates)
        if span<width*.92 or width>size*22: continue
        baseline=statistics.median(c.box.y for c in candidates)
        ratio=(y-baseline)/size
        if not (-.35<=ratio<=.65): continue
        for char in candidates:
            state=char.pdf_style.graphic_state
            ops=str(state.passthrough_per_char_instruction or '') if state else ''
            payload={'offset':ratio,'width':.04,'ops':str(curve.graphic_state.passthrough_per_char_instruction or '')}
            char.pdf_style=PdfStyle(font_id=char.pdf_style.font_id,font_size=char.pdf_style.font_size,
                graphic_state=GraphicState(passthrough_per_char_instruction=(ops or '')+'\n%PDF2ZH_DECOR:'+json.dumps(payload,separators=(',',':'))+'\n'))
        removed.add(id(curve));decorations+=1
    attachments=[]
    for group in groups:
        owners=[(sum(id(c) in group['ids'] for comp in p.pdf_paragraph_composition for c in _characters(comp)),p) for p in page.pdf_paragraph]
        owners=[(count,p) for count,p in owners if count]
        if not owners: continue
        owner=max(owners,key=lambda item:item[0])[1]
        selected=[c for c in all_chars if id(c) in group['ids']]
        for char in selected:
            char.formula_layout_id=-100000-len(attachments)
        formula=PdfFormula(pdf_character=selected,line_id=-1000-len(attachments))
        update_formula_data(formula)
        inserted=False
        for paragraph in page.pdf_paragraph:
            compositions=[]
            for composition in paragraph.pdf_paragraph_composition:
                chars=_characters(composition)
                chosen=[c for c in chars if id(c) in group['ids']]
                if not chosen:
                    compositions.append(composition); continue
                # Before this stage, native paragraphs contain PdfLine objects.
                if not composition.pdf_line:
                    raise RuntimeError('图形公式保护遇到不支持的源文字结构')
                run=[]
                for char in chars:
                    if id(char) in group['ids']:
                        if run:
                            line=PdfLine(pdf_character=run);_update_line(line)
                            compositions.append(PdfParagraphComposition(pdf_line=line));run=[]
                        if paragraph is owner and not inserted:
                            compositions.append(PdfParagraphComposition(pdf_formula=formula));inserted=True
                    else: run.append(char)
                if run:
                    line=PdfLine(pdf_character=run);_update_line(line)
                    compositions.append(PdfParagraphComposition(pdf_line=line))
            paragraph.pdf_paragraph_composition=compositions
        attachments.append((set(map(id,selected)),group['curves']))
    before=Counter(c.char_unicode for c in all_chars)
    after=Counter(c.char_unicode for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition for c in _characters(comp))
    if before!=after:
        raise RuntimeError('文字图形保护的字符完整性检查失败，已停止生成以避免内容缺失')
    page.pdf_curve=[c for c in page.pdf_curve if id(c) not in removed]
    return attachments,decorations


def finish_vector_semantics(page,attachments):
    from babeldoc.format.pdf.document_il.utils.formular_helper import update_formula_data
    formulas=[comp.pdf_formula for p in page.pdf_paragraph for comp in p.pdf_paragraph_composition if comp.pdf_formula]
    for ids,curves in attachments:
        candidates=[f for f in formulas if ids<={id(c) for c in f.pdf_character}]
        if len(candidates)!=1:
            raise RuntimeError('公式结构保护未能保留完整字符和线条，已停止生成以避免静默损失')
        formula=candidates[0]
        formula.pdf_curve.extend(curves)
        update_formula_data(formula)


def install_layout_integrity():
    from babeldoc.format.pdf.document_il.backend.pdf_creater import RenderContext, CharacterRenderUnit, PDFCreater
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas
    if getattr(RenderContext,'_pdf2zh_layout_integrity',False): return
    context_init=RenderContext.__init__
    @wraps(context_init)
    def scoped_fonts(self,*args,**kwargs):
        context_init(self,*args,**kwargs)
        # Local resource names shadow page resources. Reversing this precedence
        # emits one-byte codes into two-byte XObject fonts and paints .notdef.
        for xobject in self.page.pdf_xobject:
            self.xobj_encoding_length_map.setdefault(xobject.xobj_id,{}).update(
                {font.font_id:font.encoding_length for font in xobject.pdf_font if font.encoding_length})
    RenderContext.__init__=scoped_fonts
    find=ParagraphFinder.process_page
    @wraps(find)
    def find_rows(self,page):
        result=find(self,page);preserve_algorithm_rows(self,page);return result
    ParagraphFinder.process_page=find_rows
    styles=StylesAndFormulas.process_page
    @wraps(styles)
    def vector_styles(self,page):
        attachments,decorations=protect_vector_semantics(page)
        result=styles(self,page)
        finish_vector_semantics(page,attachments)
        if attachments or decorations:
            LOG.info('文字图形结构已保护：页 %s，%s 个公式/上划线组，%s 处文本装饰',page.page_number+1,len(attachments),decorations)
        return result
    StylesAndFormulas.process_page=vector_styles
    render=CharacterRenderUnit.render
    @wraps(render)
    def render_decorations(self,draw_op,context):
        result=render(self,draw_op,context)
        char=self.char
        state=char.pdf_style.graphic_state if char.pdf_style else None
        instructions=state.passthrough_per_char_instruction if state else None
        if not isinstance(instructions,str) or '%PDF2ZH_DECOR:' not in instructions:
            return result
        for match in DECOR.finditer(instructions):
            decoration=json.loads(match.group(1));size=char.pdf_style.font_size
            width=char.advance or char.box.x2-char.box.x
            if char.vertical or width<=0: continue
            x=char.box.x;y=char.box.y+decoration['offset']*size
            draw_op.append(f"q {decoration['ops']} {decoration['width']*size:.6f} w {x:.6f} {y:.6f} m {x+width:.6f} {y:.6f} l S Q\n".encode())
        return result
    CharacterRenderUnit.render=render_decorations
    write=PDFCreater.write
    @wraps(write)
    def checked_write(self,*args,**kwargs):
        result=write(self,*args,**kwargs)
        # Some upstream font-scope failures return a successful PDF full of .notdef.
        # Surface these semantic failures as quality warnings, even on exit zero.
        import pymupdf
        seen=set()
        for name in ('mono_pdf_path','dual_pdf_path','no_watermark_mono_pdf_path','no_watermark_dual_pdf_path'):
            path=getattr(result,name,None)
            if not path or str(path) in seen: continue
            seen.add(str(path))
            try:
                with pymupdf.open(path) as document:
                    bad=[(page.number+1,page.get_text().count('\x00')) for page in document]
                bad=[(page,count) for page,count in bad if count]
                if bad:
                    LOG.warning('Layout integrity warning: 输出 PDF 的页 %s 含 %s 个空字形，请检查图表及字体',
                                ','.join(str(page) for page,_ in bad),sum(count for _,count in bad))
            except Exception as exc:
                LOG.warning('Layout integrity warning: 无法完成输出 PDF 的字体完整性检查：%s',exc)
        return result
    PDFCreater.write=checked_write
    RenderContext._pdf2zh_layout_integrity=True
