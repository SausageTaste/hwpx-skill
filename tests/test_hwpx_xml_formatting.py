"""Regression checks for text-safe HWPX XML indentation."""
import argparse
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers.expat import ExpatError

from format_hwpx_xml import element_key, format_entries, format_xml
from hwpx_archive import MIMETYPE
from hwpx_directory import DirectoryEditor, read_directory

HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HH = 'http://www.hancom.co.kr/hwpml/2011/head'


def document(body):
    return f'<hp:p xmlns:hp="{HP}">{body}</hp:p>'.encode('utf-8')


class XmlFormattingTests(unittest.TestCase):
    def test_structural_indentation_and_repeatability(self):
        source = document('<hp:run><hp:t>  exact  text\n next </hp:t></hp:run>')
        result, report = format_xml(source)
        self.assertIn(b'\n    <hp:run>\n        <hp:t>', result)
        self.assertIn(b'<hp:t>  exact  text\n next </hp:t>', result)
        self.assertTrue(report['protected_bytes_equal'])
        self.assertEqual(format_xml(result)[0], result)

    def test_mixed_text_and_whitespace_only_text_stay_verbatim(self):
        body = '<hp:run><hp:t>  </hp:t><hp:t>A<hp:tab/>  B<hp:lineBreak/>\nC</hp:t></hp:run>'
        source = document(body)
        result, _ = format_xml(source)
        for text in (b'<hp:t>  </hp:t>', b'<hp:t>A<hp:tab/>  B<hp:lineBreak/>\nC</hp:t>'):
            self.assertIn(text, result)
        # A whitespace-only mixed-content text element is also protected.
        source = document('<hp:run><hp:t> <hp:tab/> </hp:t></hp:run>')
        self.assertIn(b'<hp:t> <hp:tab/> </hp:t>', format_xml(source)[0])

    def test_numbering_comments_entities_and_cdata_keep_exact_bytes(self):
        source = f'''<?xml version='1.0' encoding='UTF-8'?>
<h:head xmlns:h="{HH}"><h:numberings><h:numbering><h:paraHead>제  ^1  장</h:paraHead></h:numbering></h:numberings></h:head>'''.encode()
        self.assertIn('제  ^1  장'.encode(), format_xml(source)[0])
        payload = '<hp:t><![CDATA[A  <B>\n]]>&#13;&#x20;&amp;</hp:t>'
        source = document('<hp:run>' + payload + '</hp:run>')
        result, _ = format_xml(source)
        self.assertIn(payload.encode(), result)
        self.assertEqual(ET.fromstring(source).find('.//{'+HP+'}t').text,
                         ET.fromstring(result).find('.//{'+HP+'}t').text)
        for payload in ('<!-- keep -->', '<?keep test?>', '<![CDATA[ ]]>'):
            source = document(payload + '<hp:run><hp:t>x</hp:t></hp:run>')
            self.assertEqual(format_xml(source)[0], source)

    def test_attributes_prefixes_bom_and_declaration_are_not_reserialized(self):
        source = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\r\n' + document(
            '<hp:run value=\'a > b &quot; &#x9; &#xA; &#13;\'><hp:t/></hp:run>')
        result, _ = format_xml(source)
        self.assertTrue(result.startswith(source[:source.index(b'<hp:p')]))
        self.assertIn(b"value='a > b &quot; &#x9; &#xA; &#13;'", result)

    def test_preserve_space_unknown_namespaces_and_nonempty_mixed_containers(self):
        for payload in (
            '<hp:run xml:space="preserve"> <hp:t>A</hp:t> </hp:run>',
            '<hp:run xml:space="preserve"><hp:run xml:space="default"><hp:t>A</hp:t></hp:run></hp:run>',
            '<u:unknown xmlns:u="urn:unknown"><hp:run><hp:t>A</hp:t></hp:run></u:unknown>',
            '<hp:run>text<hp:t>A</hp:t> tail</hp:run>',
            '<hp:run>&#32;<hp:t>A</hp:t></hp:run>',
        ):
            with self.subTest(payload=payload):
                self.assertIn(payload.encode(), format_xml(document(payload))[0])

    def test_reject_invalid_xml_dtd_and_other_encodings(self):
        for source in (
            b'<broken>',
            b'<!DOCTYPE p [<!ENTITY value "expanded">]><p>&value;</p>',
            b'<?xml version="1.0" encoding="ISO-8859-1"?><p/>',
            '<?xml version="1.0" encoding="UTF-16"?><p/>'.encode('utf-16'),
        ):
            with self.subTest(source=source), self.assertRaises((ValueError, ExpatError)):
                format_xml(source)

    def test_style_comparison_ignores_indentation_but_preserves_text(self):
        source = f'<h:paraPr xmlns:h="{HH}" id="1"><h:heading type="NONE"/><h:margin/></h:paraPr>'.encode()
        plain = ET.fromstring(source)
        indented = ET.fromstring(format_xml(source)[0])
        indented.tail = '\n    '
        self.assertEqual(element_key(plain), element_key(indented))
        indented[0].set('type', 'OUTLINE')
        self.assertNotEqual(element_key(plain), element_key(indented))
        for before, after in (('  ', ' '), ('A  B', 'A B'), ('A\nB', 'A B')):
            a = ET.fromstring(document(f'<hp:run><hp:t>{before}</hp:t></hp:run>'))
            b = ET.fromstring(document(f'<hp:run><hp:t>{after}</hp:t></hp:run>'))
            self.assertNotEqual(element_key(a), element_key(b))

    def test_directory_check_apply_and_binary_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / 'package'
            package.mkdir()
            (package / 'mimetype').write_bytes(MIMETYPE)
            (package / 'section.xml').write_bytes(document('<hp:run><hp:t>  x  </hp:t></hp:run>'))
            (package / 'image.bin').write_bytes(bytes(range(256)))
            args = argparse.Namespace(package=package, report=root / 'report.json', check=True)
            editor = DirectoryEditor(args)
            original = read_directory(package)
            entries, report = format_entries(original)
            editor.finish(entries, report)
            self.assertEqual(read_directory(package), original)
            args.check = False
            DirectoryEditor(args).finish(entries, report)
            self.assertEqual(read_directory(package), entries)
            self.assertEqual(entries['image.bin'], original['image.bin'])
            self.assertEqual(format_entries(entries)[0], entries)
            self.assertEqual(json.loads(args.report.read_text())['changed_files'], ['section.xml'])


if __name__ == '__main__':
    unittest.main()
