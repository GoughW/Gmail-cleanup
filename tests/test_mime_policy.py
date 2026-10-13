"""Independent input-policy, source-span and renderer assertions for F1/F3/F6."""
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import html5lib

from gmail_cleanup.errors import Unsupported
from gmail_cleanup.mime import parse, parts, transform, body_text, removable_attachment, digest
from gmail_cleanup.preflight import run
from .helpers import FakeGmail, message, Store


class MimePolicyTests(unittest.TestCase):
    def assert_rejected_before_writes(self, original, manifest):
        valid, _ = message()
        api = FakeGmail({'abc123': valid})
        api.messages['abc123'] = deepcopy(original)
        choices = {"version": 2, "allowlist": ['abc123'], 'messages': {'abc123': [
            {'part': s['part'], 'decoded_sha256': s['decoded_sha256']} for s in manifest['messages']['abc123']]}}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                report, resolved = run(api, store, choices, api.drive, allow_upload=True)
                self.assertFalse(report['all_ready_to_create'])
                self.assertIsNone(resolved)
                self.assertEqual(api.inserts + api.trashes + api.drive.creates + api.drive.allocated, [])
                self.assertEqual(api.messages['abc123'], original)
            finally:
                store.close()

    def test_flowed_delsp_unknown_and_malformed_body_parameters_fail_closed(self):
        for parameters in ({'format': 'flowed'}, {'format': 'flowed', 'delsp': 'yes'},
                           {'format': 'fixed'}, {'x-body-semantics': 'unknown'}, {'charset': ''}):
            with self.subTest(parameters=parameters):
                original, manifest = message()
                parsed = parse(original['raw_bytes'])
                leaf = parsed.get_payload()[0]
                leaf.set_content('invoice 12 \n345\n> quoted \n> line\n stuffed\n-- \nsignature\n')
                for name, value in parameters.items():
                    leaf.set_param(name, value)
                original['raw_bytes'] = parsed.as_bytes()
                self.assert_rejected_before_writes(original, manifest)

    def test_duplicate_body_parameters_rejected_without_serialization_repair(self):
        original, manifest = message()
        original['raw_bytes'] = original['raw_bytes'].replace(b'charset="utf-8"', b'charset="utf-8"; charset="iso-8859-1"', 1)
        self.assert_rejected_before_writes(original, manifest)

    def test_all_content_ids_and_locations_protect_selected_resource_without_reference_heuristics(self):
        for header, value in (('Content-ID','<ordinary>'),('Content-ID','<unreferenced>'),
                              ('Content-Location','images/chart.png'),('Content-Location','https://example.invalid/chart.png')):
            for reference in ('cid:ordinary','cid%3Aordinary','cid&#58;ordinary',r'cid:ord\69nary'):
                with self.subTest(header=header,reference=reference):
                    original, manifest = message()
                    parsed = parse(original['raw_bytes'])
                    selected = dict(parts(parsed))[manifest['messages']['abc123'][0]['part']]
                    selected[header] = value
                    parsed.get_payload()[0].set_content(reference + '\n')
                    self.assertFalse(removable_attachment(selected, parsed))
                    original['raw_bytes'] = parsed.as_bytes()
                    self.assert_rejected_before_writes(original, manifest)

    def test_duplicate_resource_ids_and_unknown_metadata_are_conservative(self):
        original, manifest = message()
        parsed = parse(original['raw_bytes'])
        selected = dict(parts(parsed))[manifest['messages']['abc123'][0]['part']]
        selected['Content-ID'] = '<duplicate>'
        selected['Content-ID'] = '<duplicate>'
        original['raw_bytes'] = parsed.as_bytes()
        self.assert_rejected_before_writes(original, manifest)

    def test_html_comments_raw_text_style_hiding_and_unusual_nesting_block_before_writes(self):
        unsupported = ['<!-- unfinished', '<p>x</p><!-- unfinished', '<style>body {display:none}',
            '<script>unfinished', '<textarea>unfinished', '<div style="display:none">x</div>',
            '<html hidden><body>x</body></html>', '<div><p>x</div>', '<p>x<div>y</div></p>',
            '<html><body><span>never closed</body></html>', '<style>body{display:none}</style><p>x</p>']
        for html in unsupported:
            for case in ('html', 'plain'):
                with self.subTest(html=html,case=case):
                    original, manifest = message(case)
                    parsed = parse(original['raw_bytes'])
                    leaf = next(p for _,p in parts(parsed) if p.get_content_type() == ('text/html' if case == 'html' else 'text/plain'))
                    leaf.set_content(html, subtype='html')
                    original['raw_bytes'] = parsed.as_bytes()
                    self.assert_rejected_before_writes(original, manifest)

    def test_supported_complete_document_and_fragment_preserve_source_outside_exact_span(self):
        for source in ('<html><head></head><body><p>original Café</p></body></html>\r\n',
                       '<p>fragment Café</p><!-- trailing complete comment -->\r\n'):
            with self.subTest(source=source):
                original, manifest = message('html')
                parsed = parse(original['raw_bytes'])
                leaf = next(p for _,p in parts(parsed) if p.get_content_type() == 'text/html')
                leaf.set_content(source, subtype='html')
                original['raw_bytes'] = parsed.as_bytes()
                old_html = body_text(next(p for _,p in parts(parse(original['raw_bytes'])) if p.get_content_type() == 'text/html'))
                old_plain = next(p.get_payload(decode=True) for _,p in parts(parsed) if p.get_content_type() == 'text/plain')
                _, removed, result = transform(original['raw_bytes'], manifest['messages']['abc123'], 'policy-test')
                composed = next(body_text(p) for _,p in parts(result) if p.get_content_type() == 'text/html')
                # Independent lexical oracle: stripping only the new note restores original source.
                start = composed.index('\r\n<hr>')
                end = composed.index('</ul></div>',start) + len('</ul></div>\r\n')
                self.assertEqual(composed[:start] + composed[end:], old_html)
                if '</body>' in old_html:
                    self.assertLess(start, composed.index('</body>'))
                tree = html5lib.parse(composed, namespaceHTMLElements=False)
                anchors = list(tree.iter('a'))
                self.assertEqual([a.attrib['href'] for a in anchors], [removed[0]['drive_url']])
                self.assertIn(removed[0]['filename'], ''.join(tree.itertext()))
                self.assertIn(removed[0]['retained_sha256'], ''.join(tree.itertext()))
                plain = next(p.get_payload(decode=True) for _,p in parts(result) if p.get_content_type() == 'text/plain')
                self.assertTrue(plain.startswith(old_plain))

    def test_independent_offline_pdf_renderer_note_is_visible_and_anchor_exact(self):
        from io import BytesIO
        from weasyprint import HTML
        from pypdf import PdfReader
        original, manifest = message('html')
        _, removed, transformed = transform(original['raw_bytes'], manifest['messages']['abc123'], 'renderer')
        composed = next(body_text(p) for _,p in parts(transformed) if p.get_content_type() == 'text/html')
        def deny_network(url):
            raise AssertionError('Offline renderer cannot fetch any URL')
        pdf = PdfReader(BytesIO(HTML(string=composed, url_fetcher=deny_network).write_pdf()))
        text = '\n'.join(page.extract_text() for page in pdf.pages)
        self.assertIn('Removed attachment: '+removed[0]['filename'], text)
        self.assertIn(removed[0]['retained_sha256'], text.replace('\n','').replace(' ',''))
        uris = [annotation.get_object()['/A']['/URI'] for page in pdf.pages
                for annotation in page.get('/Annots', []) if '/A' in annotation.get_object()]
        self.assertIn(removed[0]['drive_url'], uris)
