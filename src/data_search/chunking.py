"""Bounded structural spans over extracted text, never invented source offsets."""
from __future__ import annotations

import re
from bisect import bisect_right

_BLOCK = re.compile(r'\n[ \t]*\n+|(?m:^[ \t]{0,3}#{1,6}[ \t]+)')
_HEADING = re.compile(r'(?m:^[ \t]{0,3}#{1,6}[ \t]+([^\n]+))')
_SENTENCE = re.compile(r'[。！？!?；;](?:["”’\')）]*)(?:[ \t]*|$)|[.](?:["\')]*)(?:\s+|$)|\n')


def _spans(text: str, maximum: int, overlap: int):
    """Prefer paragraph/heading boundaries, then sentences, then word breaks."""
    boundaries = [0]
    for match in _BLOCK.finditer(text):
        boundary = match.start() if match.group().lstrip().startswith('#') else match.end()
        if boundary > boundaries[-1]:
            boundaries.append(boundary)
    boundaries.append(len(text))
    start = 0
    while start < len(text):
        ceiling = min(len(text), start + maximum)
        # Headings begin new chunks unless already at this chunk's start.
        heading = _HEADING.search(text, start + 1, ceiling)
        if heading:
            ceiling = heading.start()
        last_boundary = boundaries[bisect_right(boundaries, ceiling)-1]
        hard = False
        if last_boundary > start:
            end = last_boundary
        elif ceiling == len(text):
            end = ceiling
        else:
            sentences = list(_SENTENCE.finditer(text, start, ceiling))
            end = sentences[-1].end() if sentences else ceiling
            # A very short sentence must not cause endless tiny windows.
            if end - start < min(80, maximum // 3):
                end = ceiling
            if end == ceiling and ceiling < len(text):
                whitespace = list(re.finditer(r'\s+', text[start:ceiling]))
                if whitespace and whitespace[-1].end() >= maximum // 2:
                    end = start + whitespace[-1].end()
                hard = not (sentences and end == sentences[-1].end())
        if end <= start:
            end = min(len(text), start + maximum)
        yield start, end
        if hard and end < len(text) and overlap:
            next_start = max(start + 1, end - overlap)
            # Start overlap at a whole word when it fits in the bounded suffix.
            word = re.search(r'\s+', text[next_start:end])
            start = next_start + word.end() if word and next_start + word.end() < end else next_start
        else:
            start = end


def _groups(chunks, max_chars):
    consumed, text, location, segments = 0, '', None, []
    for number, chunk in enumerate(chunks):
        if consumed >= max_chars or number >= 20_000:
            break
        part = chunk['text'][:max_chars-consumed]
        consumed += len(part)
        if not part:
            continue
        if text and (location != chunk['locator'] or len(text)+len(part)+1 > 1200):
            yield text, location, segments
            text, segments = '', []
        begin = len(text) + int(bool(text))
        text += ('\n' if text else '') + part
        location = chunk['locator']
        segments.append((number, begin, len(text)))
        if consumed >= max_chars or number >= 19_999:
            break
    if text:
        yield text, location, segments


def split_chunks(chunks, *, max_chars=2_000_000, maximum=350, overlap=50):
    """Yield exact extracted substrings with source-local char and line ranges.

    Parser blocks from the same page/slide/record retain nearby context; blocks
    with different locators never merge. Source spans map each piece back to
    the exact parser block substring (inserted separator newlines are omitted).
    The input limit is checked before slicing to bound even an oversized caller.
    """
    if maximum < 32 or overlap < 0 or overlap >= maximum or max_chars < 1:
        raise ValueError('invalid chunk bounds')
    for text, original, segments in _groups(chunks, max_chars):
        lines = [match.start() for match in re.finditer('\n', text)] if 'line_start' in original else []
        heading = None
        for start, end in _spans(text, maximum, overlap):
            part = text[start:end]
            if not part.strip():
                continue
            title = _HEADING.search(part)
            if title:
                heading = title.group(1).strip()[:160]
            locator = dict(original)
            spans = [{'source_chunk':number, 'char_start':max(start,begin)-begin,
                      'char_end':min(end,finish)-begin,
                      'piece_start':max(start,begin)-start, 'piece_end':min(end,finish)-start}
                     for number, begin, finish in segments if begin < end and finish > start]
            locator.update(char_start=start, char_end=end,
                           offset_basis='extraction_chunk' if len(segments)==1 else 'grouped_extraction',
                           source_spans=spans)
            if len(segments)==1:
                locator['source_chunk'] = segments[0][0]
            if heading:
                locator['heading'] = heading
            if 'line_start' in original and len(segments)==1:
                first = original['line_start'] + bisect_right(lines, start-1)
                locator['line_start'] = first
                locator['line_end'] = first + part.count('\n') - int(part.endswith('\n'))
            yield {'text': part, 'locator': locator}
