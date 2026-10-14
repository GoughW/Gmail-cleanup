"""R1: coordinates, exact source spans and independent interpreted-content oracle."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gmail_cleanup.html_profile import compose, insertion_offset, verify_parsed_preservation
from gmail_cleanup.errors import Unsupported
from gmail_cleanup.engine import plan_message, create, trash_original, reconcile
from gmail_cleanup.mime import transform, body_text, parts, parse
from gmail_cleanup.preflight import run
from .helpers import FakeGmail, Store, message
from .test_rereview_regressions import html_original, html_body, independent_original_tree, tree_signature, R1_SOURCE

NOTE = '<hr><div><p>Visible note</p><ul><li>file; <a href="https://drive.google.com/file/d/FAKE/view?x=1&amp;y=2">backup</a>; SHA-256: hash</li></ul></div>'
URL = 'https://drive.google.com/file/d/FAKE/view?x=1&y=2'


class HtmlPreservationTests(unittest.TestCase):
    def assert_preserved(self, source, complete):
        actual, offset, span = compose(source, NOTE)
        self.assertEqual(actual[:offset] + actual[offset+len(span):], source)
        self.assertEqual(span, '\r\n'+NOTE+'\r\n')
        self.assertEqual(offset, source.lower().index('</body') if complete else len(source))
        before, restored, note = independent_original_tree(source, actual, complete)
        self.assertEqual(tree_signature(restored), tree_signature(before))
        self.assertEqual([a.attrib['href'] for a in note.iter('a')], [URL])
        self.assertIn('Visible note', ''.join(note.itertext()))
        self.assertIn('file;', ''.join(note.itertext()))
        self.assertIn('SHA-256: hash', ''.join(note.itertext()))
        return actual

    def test_unicode_separators_comments_entities_and_line_endings(self):
        for separator in ('\u2028', '\u2029', '\f', '\r'):
            for line_endings in ('\n', '\r\n', '\n\r\n'):
                for complete in (True, False):
                    with self.subTest(separator=ascii(separator), endings=ascii(line_endings), complete=complete):
                        content = '<!-- separator '+separator+' comment -->'+line_endings
                        content += separator+'xxxx'+line_endings+'AA&amp;BB &#38;CC &#x26;DD'
                        content += '<p>before <b>bold</b> meaningful tail</p>after text'+line_endings
                        source = '<html><head></head><body>'+content+'</body>'+line_endings+'</html>' if complete else content
                        self.assert_preserved(source, complete)

    def test_existing_invalid_codepoints_are_rejected_without_mutations(self):
        # HTML5 reports invalid-codepoint for NEL and these C0 separators. Keep
        # that existing conservative policy; do not broaden it to hide R1.
        for separator in ('\u0085', '\v', '\x1c', '\x1d', '\x1e'):
            with self.subTest(separator=ascii(separator)):
                original, manifest = html_original(R1_SOURCE.replace('\u2028', separator))
                api = FakeGmail({'abc123': original})
                choices = {'version':2, 'allowlist':['abc123'], 'messages':{'abc123':[
                    {'part':c['part'], 'decoded_sha256':c['decoded_sha256']} for c in manifest['messages']['abc123']]}}
                with tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory)/'state.sqlite')
                    try:
                        report, resolved = run(api, store, choices, api.drive, allow_upload=True)
                        self.assertFalse(report['all_ready_to_create']); self.assertIsNone(resolved)
                        self.assertEqual(api.inserts+api.trashes+api.drive.allocated+api.drive.creates, [])
                    finally:store.close()

    def test_named_and_numeric_references_next_to_actual_closing_body_token(self):
        for reference in ('&amp;', '&#38;', '&#x26;', '&lt;', '&gt;', '&quot;'):
            with self.subTest(reference=reference):
                self.assert_preserved('<html><body>\u2028xxxx\nAA'+reference+'BB</body></html>', True)

    def test_closing_body_token_validation_rejects_injected_wrong_coordinates(self):
        for position in ((1,0), (999,0), (1, len('<html><body>x'))):
            with self.subTest(position=position), patch('gmail_cleanup.html_profile.SourceProfile.getpos', return_value=position):
                with self.assertRaises(Unsupported):insertion_offset('<html><body>x&amp;y</body></html>')
        self.assert_preserved('<HTML><BODY><p>x&amp;y</p></BODY \n></HTML>', True)

    def test_parsed_preservation_catches_entity_split_even_if_note_is_a_valid_child(self):
        bad_offset=R1_SOURCE.index('&')+1
        with patch('gmail_cleanup.html_profile.insertion_offset', return_value=bad_offset):
            with self.assertRaisesRegex(Unsupported, 'interpreted content'):
                compose(R1_SOURCE, NOTE)

    def test_parsed_preservation_keeps_meaningful_element_and_comment_tails(self):
        source='<html><body>before<p>paragraph</p>tail<!--comment-->after</body></html>'
        composed=self.assert_preserved(source,True)
        for bad in (composed.replace('paragraph','changed'), composed.replace('tail',''),
                    composed.replace('<!--comment-->after','<!--comment-->wrong'),
                    composed.replace('before',''), composed.replace('after','')):
            with self.subTest(bad=bad), self.assertRaises(Unsupported):
                verify_parsed_preservation(source,bad,NOTE,complete=True)

    def test_policy_two_record_is_inspection_only_and_immutable(self):
        original, manifest = message('html')
        api = FakeGmail({'abc123':original})
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite')
            try:
                plan_message(api,store,manifest,'abc123')
                record=create(api,store,manifest,'abc123',execute=True)
                # Construct a legacy fixture locally; production never rewrites a plan.
                from gmail_cleanup.mime import json_digest
                import json
                legacy=deepcopy(record)
                legacy['plan'].update(safety_policy=2,transformation_policy=2)
                plan=deepcopy(legacy['plan']);plan.pop('plan_sha256')
                legacy['plan']['plan_sha256']=json_digest(plan)
                store.db.execute('UPDATE operations SET record=? WHERE message_id=?',(json.dumps(legacy),'abc123'))
                store.recovery_error='legacy fixture'
                observed=reconcile(api,store,manifest,'abc123',record['replacement_id'],execute=True)
                self.assertFalse(observed['verification']['current_policy_eligible'])
                self.assertEqual(store.get('abc123'),legacy)
                from gmail_cleanup.errors import CleanupError
                with self.assertRaises(CleanupError):trash_original(api,store,manifest,'abc123',execute=True,enable_trash=True,
                    approve_plan=legacy['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
                self.assertEqual(api.trashes,[])
            finally:store.close()
