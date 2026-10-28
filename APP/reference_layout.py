"""Reference-only layout adapter for the project-managed BabelDOC runtime.

Keep citation entries independent and treat URLs as breakable text, not math.
No translation strings, LLM options or installed packages are rewritten.
"""
from __future__ import annotations
import bisect
import copy
import logging
import re
import statistics
from collections import Counter
from functools import wraps

LOG = logging.getLogger('pdf2zh.references')
NUMBER = re.compile(r'\[\s*(\d{1,4})\s*\]')
YEAR = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)')
HEADING = re.compile(r'^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:references|bibliography|参考文献|參考文獻)\s*$', re.I)
END = re.compile(r'^(?:appendix\b|附录|[A-Z](?:\.\d+)*\s+\S)', re.I)
URI = re.compile(r'(?:https?://|www\.|(?:doi\s*:\s*)?10\.\d{4,9}/)[^\s<>"\u3000\u3002\uff0c\uff1b]+', re.I)
URI_ONLY = re.compile(r'^(?:URL\s*[:：]\s*)?(?:https?://|www\.|(?:doi\s*:\s*)?10\.\d{4,9}/)\S+[。.,;]?$', re.I)
SOFT = '/?#&=._-'


def _text(paragraph):
    return re.sub(r'\(cid:[13]\)', ' ', (paragraph.unicode or '').replace('\x01', ' '))


def _box(char):
    return char.visual_bbox.box if char.visual_bbox and char.visual_bbox.box else char.box


def _state(config):
    if not hasattr(config, '_pdf2zh_reference_state'):
        config._pdf2zh_reference_state = {'active': False, 'meta': {}}
    return config._pdf2zh_reference_state


def _size(paragraph):
    if paragraph.pdf_style and paragraph.pdf_style.font_size:
        return paragraph.pdf_style.font_size
    sizes = [char.pdf_style.font_size for char in _characters(paragraph) if char.pdf_style and char.pdf_style.font_size]
    return statistics.median(sizes) if sizes else 9.0


def _characters(paragraph):
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_line:
            yield from composition.pdf_line.pdf_character
        elif composition.pdf_character:
            yield composition.pdf_character
        elif composition.pdf_same_style_characters:
            yield from composition.pdf_same_style_characters.pdf_character
        elif composition.pdf_formula:
            yield from composition.pdf_formula.pdf_character


def _line_numbers(paragraph):
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_line:
            text = ''.join(char.char_unicode or '' for char in composition.pdf_line.pdf_character).lstrip()
            match = NUMBER.match(text)
            if match:
                yield int(match.group(1))


def split_entries(finder, paragraph, previous_number=None):
    """Split at real line-start labels, or a consecutive label after a dated entry."""
    from babeldoc.format.pdf.document_il import PdfLine, PdfParagraphComposition
    lines = [composition.pdf_line for composition in paragraph.pdf_paragraph_composition if composition.pdf_line]
    if len(lines) != len(paragraph.pdf_paragraph_composition):
        return [paragraph]
    chars, starts, line_indices = [], set(), []
    for line in lines:
        starts.add(len(chars))
        chars.extend(line.pdf_character)
        line_indices.extend([line] * len(line.pdf_character))
    offsets, joined = [0], ''
    for char in chars:
        joined += char.char_unicode or ''
        offsets.append(len(joined))
    breaks, last_number, last_offset = [0], previous_number, 0
    for match in NUMBER.finditer(joined):
        index = bisect.bisect_right(offsets, match.start()) - 1
        number = int(match.group(1))
        is_start = index in starts or not joined[offsets[max(start for start in starts if start <= index)]:match.start()].strip()
        prefix = joined[last_offset:match.start()]
        consecutive = last_number is not None and number == last_number + 1 and (YEAR.search(prefix) or (previous_number == last_number and '/' in prefix))
        if is_start or consecutive:
            if index > 0:
                breaks.append(index)
            last_number, last_offset = number, match.start()
    if len(breaks) == 1:
        return [paragraph]
    breaks.append(len(chars)); result = []
    for counter, (begin, end) in enumerate(zip(breaks, breaks[1:])):
        if begin == end:
            continue
        item = copy.copy(paragraph)
        item.debug_id = f'{paragraph.debug_id}.ref{counter}'
        item.pdf_paragraph_composition = []
        cursor = begin
        while cursor < end:
            line = line_indices[cursor]; stop = cursor + 1
            while stop < end and line_indices[stop] is line:
                stop += 1
            fragment = PdfLine(pdf_character=chars[cursor:stop])
            finder.update_line_data(fragment)
            item.pdf_paragraph_composition.append(PdfParagraphComposition(pdf_line=fragment))
            cursor = stop
        finder.update_paragraph_data(item, update_unicode=True)
        item.first_line_indent = False
        result.append(item)
    return result


def merge_label_fragments(finder, paragraphs):
    """Rejoin a detached '[' with the matching first-line '10] Author...' glyphs."""
    from babeldoc.format.pdf.document_il import PdfLine, PdfParagraphComposition
    consumed = set()
    for fragment in paragraphs:
        if _text(fragment).strip() != '[':
            continue
        if any(not composition.pdf_line for composition in fragment.pdf_paragraph_composition):
            continue
        candidates = []
        for target in paragraphs:
            if target is fragment or target.xobj_id != fragment.xobj_id or not re.match(r'^\s*\d{1,4}\]', _text(target)):
                continue
            first = target.pdf_paragraph_composition[0].pdf_line if target.pdf_paragraph_composition else None
            if not first or not first.box:
                continue
            overlap = min(fragment.box.y2, first.box.y2)-max(fragment.box.y, first.box.y)
            gap = first.box.x-fragment.box.x2
            if overlap >= min(fragment.box.y2-fragment.box.y, first.box.y2-first.box.y)*.5 and 0 <= gap <= _size(target):
                candidates.append((gap, target, first))
        if len(candidates) != 1:
            continue
        _, target, first = candidates[0]
        line = PdfLine(pdf_character=list(_characters(fragment))+list(first.pdf_character))
        finder.update_line_data(line)
        target.pdf_paragraph_composition[0] = PdfParagraphComposition(pdf_line=line)
        finder.update_paragraph_data(target, update_unicode=True)
        consumed.add(id(fragment))
    return [paragraph for paragraph in paragraphs if id(paragraph) not in consumed]


def prepare_reference_page(finder, page):
    from babeldoc.format.pdf.document_il import Box
    state = _state(finder.translation_config)
    crop = page.cropbox.box; width = crop.x2 - crop.x; middle = (crop.x + crop.x2) / 2
    usable = [p for p in page.pdf_paragraph if p.box and p.box.y2 > crop.y + (crop.y2-crop.y)*.055 and p.layout_label != 'abandon']
    if not usable:
        return
    texts = ' '.join(_text(p) for p in usable)
    line_numbers = sorted({number for p in usable for number in _line_numbers(p)})
    # An unnumbered diagram/algorithm page after the bibliography must retain
    # its original layout, even when its appendix caption is labelled abandon.
    if state['active'] and not line_numbers and not URI.search(texts) and len(YEAR.findall(texts)) < 2:
        state['active'] = False
    consecutive_labels = any(line_numbers[index:index+3] == list(range(line_numbers[index],line_numbers[index]+3)) for index in range(max(0,len(line_numbers)-2)))
    # A page can contain the last body paragraphs before its References heading.
    # A bibliography signature must never mark those preceding paragraphs.
    auto_start = (not state['active'] and not any(HEADING.fullmatch(_text(p).strip()) for p in usable)
                  and consecutive_labels and len(YEAR.findall(texts)) >= 2 and URI.search(texts))
    narrow = [p for p in usable if p.box.x2-p.box.x < width*.58]
    left = [p for p in narrow if p.box.x < middle-width*.1]
    right = [p for p in narrow if p.box.x >= middle]
    two_columns = len(left) >= 2 and len(right) >= 2
    ordered = sorted(usable, key=lambda p: (int(two_columns and p.box.x >= middle), -p.box.y2, p.box.x))
    numbered_sizes = [_size(p) for p in usable if NUMBER.match(_text(p).lstrip())]
    body_size = statistics.median(numbered_sizes or [_size(p) for p in usable])
    references = []
    for paragraph in ordered:
        text = _text(paragraph).strip()
        if HEADING.fullmatch(text):
            state['active'] = True
            continue
        if auto_start and (NUMBER.match(text) or any(_line_numbers(paragraph))):
            state['active'] = True
            auto_start = False
        section_title = (paragraph.layout_label == 'title' and len(text)<140 and not NUMBER.match(text) and not URI.search(text) and _size(paragraph)>=body_size*1.08)
        if section_title or (END.match(text) and len(text)<140 and _size(paragraph)>=body_size*1.1):
            state['active'] = False
        if state['active']:
            references.append(paragraph)
    if not references:
        return
    identities = {id(p) for p in references}
    expected_chars = Counter(char.char_unicode for p in references for char in _characters(p))
    metadata_before = state['meta'].copy()
    expanded = []
    previous_number = None
    working = []
    for original in references:
        paragraph = copy.copy(original)
        paragraph.box = copy.copy(original.box)
        paragraph.pdf_paragraph_composition = list(original.pdf_paragraph_composition)
        working.append(paragraph)
    # Glyphs/styles are immutable here. Copy only containers that we edit;
    # parsed documents can hold deeply linked shared glyph metadata.
    for paragraph in merge_label_fragments(finder, working):
        expanded.extend(split_entries(finder, paragraph, previous_number))
        numbers = NUMBER.findall(_text(paragraph))
        if numbers:
            previous_number = int(numbers[-1])
    groups = {}
    for paragraph in expanded:
        groups.setdefault(int(two_columns and paragraph.box.x >= middle), []).append(paragraph)
    merged = []
    margin = max(20, min(p.box.x for p in usable)-crop.x)
    for column, paragraphs in groups.items():
        context = right if column else left
        if not two_columns:
            context = usable
        x = min(p.box.x for p in paragraphs)
        x2 = max(p.box.x2 for p in context or paragraphs)
        if two_columns and column == 0:
            x2 = min(x2, min(p.box.x for p in right) - 8)
        x2 = min(x2, crop.x2-margin)
        if x2-x < 60:
            state['meta'] = metadata_before
            LOG.warning('参考文献栏宽无法可靠确定，保留页 %s 的原始段落', page.page_number+1)
            return
        base_size = statistics.median(_size(p) for p in paragraphs if NUMBER.search(_text(p))) if any(NUMBER.search(_text(p)) for p in paragraphs) else statistics.median(_size(p) for p in paragraphs)
        previous = None
        for paragraph in paragraphs:
            text = _text(paragraph).strip()
            compact = re.sub(r'\s+', '', text)
            is_url = bool(URI_ONLY.fullmatch(compact)) or compact in {'URL:', 'URL：', 'U', 'R', 'L:', 'L：', 'h', 't', 'p', 's://', 'w'}
            if previous and re.match(r'^(?:tps?|ps|s)://', compact) and re.search(r'(?:h|ht|htt|http|https)$', re.sub(r'\s+', '', _text(previous))):
                is_url = True
            if previous and is_url and paragraph.xobj_id == previous.xobj_id and previous.box.y-paragraph.box.y2 <= max(base_size*2.2, 6):
                previous.pdf_paragraph_composition.extend(paragraph.pdf_paragraph_composition)
                finder.update_paragraph_data(previous, update_unicode=True)
            else:
                merged.append(paragraph); previous = paragraph
            state['meta'][previous.debug_id] = {'column': column, 'left': x, 'right': x2, 'base_size': base_size, 'page': page.page_number,
                'original_box': (previous.box.x, previous.box.y, previous.box.x2, previous.box.y2)}
    if Counter(char.char_unicode for p in merged for char in _characters(p)) != expected_chars:
        state['meta'] = metadata_before
        raise RuntimeError('参考文献条目拆分的文本完整性检查未通过，已停止以避免丢失内容')
    page.pdf_paragraph = [p for p in page.pdf_paragraph if id(p) not in identities] + merged
    for paragraph in merged:
        meta = state['meta'][paragraph.debug_id]
        paragraph.box = Box(meta['left'], paragraph.box.y, meta['right'], paragraph.box.y2)
        paragraph.first_line_indent = False
    finder._set_paragraph_render_order(page)
    LOG.info('参考文献条目边界已整理：页 %s，%s 个独立文本块', page.page_number + 1, len(merged))


def reference_units(typesetter, paragraph, fonts, original):
    from babeldoc.format.pdf.document_il import PdfSameStyleUnicodeCharacters, PdfParagraphComposition
    meta = _state(typesetter.translation_config)['meta'].get(paragraph.debug_id)
    if not meta:
        return original(typesetter, paragraph, fonts)
    temporary = copy.copy(paragraph); temporary.pdf_paragraph_composition = []
    for composition in paragraph.pdf_paragraph_composition:
        formula = composition.pdf_formula
        text = ''.join(c.char_unicode or '' for c in formula.pdf_character) if formula else ''
        compact = re.sub(r'\s+', '', text)
        if formula and URI_ONLY.fullmatch(compact) and not formula.pdf_curve and not formula.pdf_form:
            style = copy.copy(formula.pdf_character[0].pdf_style)
            style.font_size = max(style.font_size, meta['base_size']*.9)
            temporary.pdf_paragraph_composition.append(PdfParagraphComposition(pdf_same_style_unicode_characters=PdfSameStyleUnicodeCharacters(unicode=compact, pdf_style=style)))
        else:
            temporary.pdf_paragraph_composition.append(composition)
    units = original(typesetter, temporary, fonts)
    # Formula units deliberately have no text font_size. Native rendering can
    # pass them through; native line measurement calls mode([]) and cannot.
    if not units or all(unit.formular for unit in units):
        meta['passthrough'] = True
        if 'original_box' in meta:
            from babeldoc.format.pdf.document_il import Box
            paragraph.box = Box(*meta['original_box'])
        return units
    factor = max(1.0, meta['base_size']*.9/_size(paragraph))
    if factor > 1.001:
        units = [unit.relocate(unit.box.x, unit.box.y, factor) for unit in units]
    strings = [re.sub(r'\(cid:[13]\)', ' ', unit.try_get_unicode() or '\ufffc') for unit in units]
    text = ''.join(strings)
    offsets = [0]
    for string in strings:
        offsets.append(offsets[-1]+len(string))
    for match in URI.finditer(text):
        begin = bisect.bisect_right(offsets, match.start())-1
        end = bisect.bisect_left(offsets, match.end())
        start = begin
        for index in range(begin, end):
            unit = units[index]
            size = unit.font_size or (unit.char.pdf_style.font_size if unit.char and unit.char.pdf_style else None)
            if size and size < meta['base_size']*.9 and not unit.formular:
                unit = units[index] = unit.relocate(unit.box.x, unit.box.y, meta['base_size']*.9/size)
            unit._pdf2zh_uri = True
            unit.can_break_line_cache = True
            unit.is_cjk_char_cache = False
            unit.is_hung_punctuation_cache = False
            unit.is_cannot_appear_in_line_end_punctuation_cache = False
            if (unit.try_get_unicode() or '') in SOFT or index == end-1:
                units[start]._pdf2zh_uri_segment = sum(item.width for item in units[start:index+1])
                start = index+1
    for unit in units:
        size = unit.font_size or (unit.char.pdf_style.font_size if unit.char and unit.char.pdf_style else None) or meta['base_size']
        # Glyph ink bounds (e.g. '.' or 'a') are not typographic line heights.
        unit.height_cache = max(unit.height, size)
        if unit.try_get_unicode() in (' ', '\x01', '(cid:1)', '(cid:3)'):
            unit.is_space_cache = True
            unit.can_break_line_cache = True
            advance = unit.char.advance if unit.char and unit.char.advance else 0
            unit.width_cache = max(unit.width, advance, size*.25)
    return units


def plan_reference_columns(typesetter, page):
    from babeldoc.format.pdf.document_il import Box
    state = _state(typesetter.translation_config)
    groups = {}
    for paragraph in page.pdf_paragraph:
        meta = state['meta'].get(paragraph.debug_id)
        if meta and meta['page'] == page.page_number:
            groups.setdefault(meta['column'], []).append(paragraph)
    if not groups:
        return
    fonts = {font.font_id: font for font in page.pdf_font if font.font_id}
    for key, value in typesetter.font_mapper.fontid2font.items():
        fonts[key] = value
    for xobject in page.pdf_xobject:
        if xobject.xobj_id is not None:
            fonts[xobject.xobj_id] = {**{font.font_id:font for font in page.pdf_font if font.font_id}, **{font.font_id:font for font in xobject.pdf_font if font.font_id}}
    for paragraphs in groups.values():
        paragraphs.sort(key=lambda p:-p.box.y2)
        meta = state['meta'][paragraphs[0].debug_id]
        top = max(p.box.y2 for p in paragraphs)
        bottom = page.cropbox.box.y+(page.cropbox.box.y2-page.cropbox.box.y)*.06
        last_bottom = min(p.box.y for p in paragraphs)
        identities = {id(p) for p in paragraphs}
        for other in page.pdf_paragraph:
            if id(other) not in identities and other.box and other.box.x2 > meta['left'] and other.box.x < meta['right'] and other.box.y2 <= last_bottom+1:
                bottom = max(bottom, other.box.y2+5)
        originals = [typesetter.create_typesetting_units(p, fonts) for p in paragraphs]
        if any(state['meta'][p.debug_id].get('passthrough') for p in paragraphs):
            # Keep vertical anchors in a column containing independent formulas.
            # Ordinary reference text still receives URL wrapping in its own box.
            LOG.warning('页 %s 的参考文献包含独立公式或空块，保留该栏各段落的纵向定位', page.page_number+1)
            continue
        chosen = None
        for scale in (1.0,.98,.96,.94,.92,.9,.88,.86,.84,.82,.8,.78,.76):
            cursor, boxes = top, []
            for paragraph, units in zip(paragraphs, originals):
                if not units:
                    boxes.append(Box(meta['left'],cursor,meta['right'],cursor)); continue
                trial = Box(meta['left'],-10000,meta['right'],cursor)
                arranged, fits = typesetter._layout_typesetting_units(units, trial, scale, 1.5 if typesetter.is_cjk else 1.3, paragraph)
                if not fits or not arranged:
                    break
                low = min(unit.box.y for unit in arranged)-.25
                boxes.append(Box(meta['left'],low,meta['right'],cursor))
                cursor = low-3.5
            if len(boxes)==len(paragraphs) and cursor+3.5 >= bottom:
                chosen = scale, boxes
                break
        if chosen:
            scale, boxes = chosen
            for paragraph, box in zip(paragraphs, boxes):
                paragraph.box = box; paragraph.optimal_scale = scale
            LOG.info('参考文献按列连续排版：页 %s，%s 条，字号比例 %.2f',page.page_number+1,len(paragraphs),scale)
        else:
            LOG.warning('参考文献超过当前栏可用空间，保留内核排版；请检查页 %s 的排版', page.page_number+1)


def install_reference_layout():
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
    if getattr(Typesetting, '_pdf2zh_reference_adapter', False):
        return
    required = ('create_typesetting_units','_get_width_before_next_break_point',
                '_layout_typesetting_units','render_page','render_paragraph',
                'retypeset_with_precomputed_scale','_update_paragraph_render_order')
    if not all(callable(getattr(Typesetting,name,None)) for name in required):
        LOG.warning('当前 BabelDOC 版本的排版接口不兼容参考文献修复，保留内核原排版')
        return
    find = ParagraphFinder.process_page
    @wraps(find)
    def find_page(self, page):
        result = find(self, page); prepare_reference_page(self, page); return result
    create = Typesetting.create_typesetting_units
    @wraps(create)
    def create_units(self, paragraph, fonts):
        return reference_units(self, paragraph, fonts, create)
    width = Typesetting._get_width_before_next_break_point
    @wraps(width)
    def next_width(self, units, scale):
        if units and getattr(units[0], '_pdf2zh_uri', False):
            segment = getattr(units[0], '_pdf2zh_uri_segment', 0)*scale
            limit = getattr(self, '_pdf2zh_current_width', float('inf'))
            return max(0, segment-units[0].width*scale) if segment<=limit else 0
        return width(self, units, scale)
    layout = Typesetting._layout_typesetting_units
    @wraps(layout)
    def layout_units(self, units, box, *args, **kwargs):
        self._pdf2zh_current_width = box.x2-box.x
        return layout(self, units, box, *args, **kwargs)
    render = Typesetting.render_page
    @wraps(render)
    def render_page(self, page):
        plan_reference_columns(self, page); return render(self, page)
    render_paragraph = Typesetting.render_paragraph
    @wraps(render_paragraph)
    def render_reference(self, paragraph, page, fonts):
        meta = _state(self.translation_config)['meta'].get(paragraph.debug_id)
        if not meta:
            return render_paragraph(self, paragraph, page, fonts)
        units = self.create_typesetting_units(paragraph, fonts)
        if meta.get('passthrough') or not units:
            return render_paragraph(self, paragraph, page, fonts)
        paragraph.pdf_paragraph_composition = []
        self.retypeset_with_precomputed_scale(paragraph, page, units, paragraph.optimal_scale or 1.0)
        if not paragraph.pdf_paragraph_composition:
            raise RuntimeError('参考文献排版未能保留完整内容，请检查日志')
        self._update_paragraph_render_order(paragraph)
    ParagraphFinder.process_page = find_page
    Typesetting.create_typesetting_units = create_units
    Typesetting._get_width_before_next_break_point = next_width
    Typesetting._layout_typesetting_units = layout_units
    Typesetting.render_page = render_page
    Typesetting.render_paragraph = render_reference
    Typesetting._pdf2zh_reference_adapter = True
