"""Independent full-lifecycle reproductions supplied by the second reviewer.

Only synthetic MIME, temporary stores and observable fake remote services.
"""
from copy import deepcopy
from email import policy
import json
from pathlib import Path
import tempfile
import unittest
import tinyhtml5

from gmail_cleanup.engine import plan_message, create, trash_original
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import parse, parts, body_text
from gmail_cleanup.storage import Store as ProductionStore
from .helpers import Store, FakeGmail, message


R1_SOURCE = '<html><body>\u2028xxxx\nAA&amp;BB</body></html>'


def html_original(source=R1_SOURCE):
    original, manifest = message('html')
    parsed = parse(original['raw_bytes'])
    part = next(p for _, p in parts(parsed) if p.get_content_type() == 'text/html')
    part.set_content(source, subtype='html', charset='utf-8', cte='base64')
    original['raw_bytes'] = parsed.as_bytes(policy=policy.SMTP)
    return original, manifest


def html_body(message):
    return next(body_text(p) for _, p in parts(parse(message['raw_bytes']))
                if p.get_content_type() == 'text/html')


def tree_signature(node):
    # Keep all text and meaningful tails, including comments. No strip/splitlines.
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or '',
            node.tail or '', tuple(tree_signature(child) for child in node))


def independent_original_tree(source, composed, complete=True):
    """tinyhtml5 oracle, deliberately independent of production transformation.

    Remove precisely the two appended note nodes and their one-LF wrappers;
    reconnect ALL surviving tail text instead of discarding it with the nodes.
    """
    def document(value):
        return value if complete else '<html><head></head><body>'+value+'</body></html>'
    before = tinyhtml5.parse(document(source), namespace_html_elements=False)
    after = tinyhtml5.parse(document(composed), namespace_html_elements=False)
    body_before, body_after = before.find('body'), after.find('body')
    original_count = len(body_before)
    children = list(body_after)
    if len(children) != original_count + 2 or [n.tag for n in children[-2:]] != ['hr', 'div']:
        raise AssertionError('Note is not the two exact appended top-level nodes')
    note = children[-1]
    slot = body_after if original_count == 0 else children[original_count-1]
    attribute = 'text' if original_count == 0 else 'tail'
    previous = getattr(slot, attribute) or ''
    following = note.tail or ''
    if not previous.endswith('\n') or not following.startswith('\n'):
        raise AssertionError('Intentional note whitespace is not exact')
    setattr(slot, attribute, previous[:-1] + following[1:] or None)
    for node in children[-2:]:
        body_after.remove(node)
    return before, after, note


def capture_journal(path):
    return {file.name: file.read_bytes() for file in path.iterdir()}


def replace_journal(path, saved):
    # Deliberate test-only rollback; production never rewrites journal records.
    for file in path.iterdir():
        file.unlink()
    for name, data in saved.items():
        file = path/name
        file.write_bytes(data)
        file.chmod(0o600)


def capture_sql(store):
    return {table: store.db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
            for table in ('operations', 'backups', 'intents', 'events', 'backup_events', 'recovery_meta')}


def r2_scenario(directory):
    path = Path(directory)/'state.sqlite'
    store = Store(path)
    original, manifest = message()
    api = FakeGmail({'abc123': original})
    record = plan_message(api, store, manifest, 'abc123')
    journal = Path(str(path)+'.recovery')
    planned_prefix = capture_journal(journal)
    api.lose_insert_response = True
    try:
        create(api, store, manifest, 'abc123', execute=True)
    except CleanupError:
        pass
    assert store.get('abc123')['state'] == 'insert_pending'
    assert len(api.inserts) == 1
    store.close()
    replace_journal(journal, planned_prefix)
    store = ProductionStore(path)
    assert store.recovery_error is not None
    return store, api, manifest, record


class IndependentRereviewTests(unittest.TestCase):
    def test_R1_exact_unicode_entity_lifecycle_preserves_interpreted_original(self):
        original, manifest = html_original()
        api = FakeGmail({'abc123': original})
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'state.sqlite')
            try:
                plan_message(api, store, manifest, 'abc123')
                record = create(api, store, manifest, 'abc123', execute=True)
                self.assertEqual(record['state'], 'verified')
                result = trash_original(api, store, manifest, 'abc123', execute=True, enable_trash=True,
                    approve_plan=record['plan']['plan_sha256'], approve_replacement=record['replacement_id'])
                self.assertEqual(result['state'], 'trashed')
                before, restored, note = independent_original_tree(html_body(original), html_body(api.messages[record['replacement_id']]))
                self.assertEqual(tree_signature(restored), tree_signature(before))
                self.assertIn('AA&BB', ''.join(restored.find('body').itertext()))
                choice = manifest['messages']['abc123'][0]
                self.assertEqual([a.attrib['href'] for a in note.iter('a')], [choice['drive_url']])
                self.assertIn(choice['decoded_sha256'], ''.join(note.itertext()))
                self.assertIn('Removed attachment: remove.bin', ''.join(note.itertext()))
                self.assertEqual(api.messages['abc123']['raw_bytes'], original['raw_bytes'])
            finally:
                store.close()

    def test_R2_journal_only_rollback_cannot_erase_uncertain_insert(self):
        with tempfile.TemporaryDirectory() as directory:
            store, api, manifest, _ = r2_scenario(directory)
            try:
                before_sql, before_journal = capture_sql(store), capture_journal(store.journal_path)
                rejected = False
                try:
                    store.restore_from_journal()
                except CleanupError:
                    rejected = True
                api.lose_insert_response = False
                try:
                    create(api, store, manifest, 'abc123', execute=True)
                except CleanupError:
                    pass
                self.assertEqual(len(api.inserts), 1, 'Recovery enabled a second insertion')
                self.assertTrue(rejected, 'Stale journal recovery was accepted')
                self.assertEqual(capture_sql(store), before_sql)
                self.assertEqual(capture_journal(store.journal_path), before_journal)
                self.assertEqual(store.get('abc123')['state'], 'insert_pending')
                self.assertIsNotNone(store.recovery_error)
                self.assertEqual(api.trashes+api.drive.allocated+api.drive.creates, [])
            finally:
                store.close()
