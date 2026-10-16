#!/usr/bin/env python3
"""Indent HWPX structural XML without reserializing text, attributes, or tags.

Usage: python scripts/format_hwpx_xml.py package --check --report report.json
Remove --check to save. Already damaged text must be restored from a baseline;
this formatter never collapses, wraps, or repairs document text.
"""
import hashlib
from dataclasses import dataclass, field
from xml.parsers import expat

from hwpx_directory import DirectoryEditor, editor_arguments


# Only known element-only HWPX containers may acquire indentation. Unknown
# elements, mixed content, text leaves, and xml:space='preserve' stay verbatim.
STRUCTURAL = {
    'http://www.hancom.co.kr/hwpml/2011/head': set('''
        borderFill borderFills bullet bullets charPr charProperties
        compatibleDocument docOption font fontface fontfaces head margin
        memoProperties numbering numberings paraPr paraProperties refList
        styles tabPr tabProperties'''.split()),
    'http://www.hancom.co.kr/hwpml/2011/core': {'fillBrush'},
    'http://www.hancom.co.kr/hwpml/2011/paragraph': set('''
        autoNum caption case compose ctrl default endNotePr fieldBegin
        footNotePr footer header imgRect line linesegarray p pageBorderFill
        pagePr parameters pic presentation renderingInfo run secPr subList
        switch tbl tc tr'''.split()),
    'http://www.hancom.co.kr/hwpml/2011/section': {'sec'},
    'http://www.idpf.org/2007/opf/': {'manifest', 'metadata', 'package', 'spine'},
    'http://www.w3.org/1999/02/22-rdf-syntax-ns#': {'Description', 'RDF'},
    'urn:oasis:names:tc:opendocument:xmlns:container': {'container', 'rootfiles'},
    'http://www.hancom.co.kr/hwpml/2011/app': {'HWPApplicationSetting'},
    'urn:oasis:names:tc:opendocument:xmlns:config:1.0': {'config-item-set'},
}
XML_SPACE = 'http://www.w3.org/XML/1998/namespace}space'
XML_SUFFIXES = ('.xml', '.hpf', '.rdf')


@dataclass
class Element:
    name: str
    start: int
    open_end: int
    preserve: bool
    empty: bool
    close_start: int = 0
    end: int = 0
    children: list = field(default_factory=list)


def tag_end(data, start):
    """Find a tag boundary without mistaking quoted '>' for its end."""
    quote = None
    for index in range(start, len(data)):
        byte = data[index]
        if quote is not None:
            if byte == quote:
                quote = None
        elif byte in (34, 39):
            quote = byte
        elif byte == 62:
            return index + 1
    raise ValueError('Unterminated XML tag')


def structural_gaps(data):
    """Return byte spans eligible for indentation, with their nesting depths."""
    # Expat byte offsets are used directly, so only UTF-8 input is supported.
    data.decode('utf-8-sig')
    parser = expat.ParserCreate(namespace_separator='}')
    stack, roots = [], []

    def declaration(version, encoding, standalone):
        if version != '1.0' or (encoding and encoding.lower() not in ('utf-8', 'utf8')):
            raise ValueError('Only XML 1.0 encoded as UTF-8 is supported')

    def reject_doctype(*args):
        raise ValueError('DTDs and custom entities are unsupported')

    def start(name, attributes):
        offset = parser.CurrentByteIndex
        end = tag_end(data, offset)
        # Conservatively preserve an entire subtree even if a descendant resets
        # xml:space to default. This does not change its XML interpretation.
        preserve = bool(stack and stack[-1].preserve) or attributes.get(XML_SPACE) == 'preserve'
        node = Element(name, offset, end, preserve, data[end - 2:end] == b'/>')
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    def end(name):
        node = stack.pop()
        node.close_start = node.open_end if node.empty else parser.CurrentByteIndex
        node.end = node.open_end if node.empty else tag_end(data, node.close_start)

    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(data, True)
    gaps = []

    def visit(node, depth):
        uri, _, local = node.name.rpartition('}')
        if node.preserve or local not in STRUCTURAL.get(uri, ()) or not node.children:
            return
        boundaries = [node.open_end] + [child.end for child in node.children]
        ends = [child.start for child in node.children] + [node.close_start]
        spans = list(zip(boundaries, ends))
        # Any text, entity reference, CDATA, comment, or PI in a gap keeps this
        # subtree intact. In particular, never indent inside hp:t (not listed).
        if any(data[a:b].strip(b' \t\r\n') for a, b in spans):
            return
        gaps.extend((a, b, depth if i == len(spans) - 1 else depth + 1)
                    for i, (a, b) in enumerate(spans))
        for child in node.children:
            visit(child, depth + 1)

    visit(roots[0], 0)
    return sorted(gaps)


def protected_bytes(data, gaps):
    """All original bytes except explicitly eligible structural whitespace."""
    chunks, offset = [], 0
    for start, end, _ in gaps:
        chunks.append(data[offset:start])
        offset = end
    chunks.append(data[offset:])
    return b''.join(chunks)


def element_key(element, preserve=False):
    """Compare parsed subtrees while ignoring only known structural indentation.

    The root's tail belongs to its parent and is not part of the subtree.
    Child tails, mixed content, and text-only values remain significant.
    """
    uri, _, local = element.tag[1:].rpartition('}') if element.tag.startswith('{') else ('', '', element.tag)
    preserve = preserve or element.get('{http://www.w3.org/XML/1998/namespace}space') == 'preserve'
    structural = (not preserve and local in STRUCTURAL.get(uri, ()) and len(element)
                  and not (element.text or '').strip(' \t\r\n')
                  and all(not (child.tail or '').strip(' \t\r\n') for child in element))
    # Unknown or mixed-content parents are opaque to the formatter as well.
    opaque = preserve or not structural
    return (element.tag, tuple(sorted(element.attrib.items())),
            None if structural else element.text,
            tuple((element_key(child, opaque), None if structural else child.tail)
                  for child in element))


def format_xml(data):
    gaps = structural_gaps(data)
    chunks, offset = [], 0
    for start, end, depth in gaps:
        chunks.extend((data[offset:start], b'\n' + b'    ' * depth))
        offset = end
    chunks.append(data[offset:])
    result = b''.join(chunks)
    # Reparse the result and independently locate its structural gaps. All
    # remaining bytes must match, including lexical entity and CDATA spelling.
    before = protected_bytes(data, gaps)
    after = protected_bytes(result, structural_gaps(result))
    if before != after:
        raise ValueError('Formatting changed protected XML bytes')
    return result, {
        'structural_gap_count': len(gaps),
        'protected_bytes_equal': True,
        'protected_bytes_sha256': hashlib.sha256(before).hexdigest(),
        'source_sha256': hashlib.sha256(data).hexdigest(),
        'result_sha256': hashlib.sha256(result).hexdigest(),
    }


def format_entries(entries):
    result, verification = dict(entries), {}
    for name, data in entries.items():
        if name.endswith(XML_SUFFIXES):
            result[name], verification[name] = format_xml(data)
    return result, {'xml_files': verification, 'non_xml_bytes_unchanged': True}


def main():
    editor = DirectoryEditor(editor_arguments(__doc__))
    entries, report = format_entries(editor.original)
    editor.finish(entries, report)


if __name__ == '__main__':
    main()
