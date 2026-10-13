"""Deliberately small HTML profile: validation, never general repair/serialization."""
from html.parser import HTMLParser
from urllib.parse import urlsplit
import html5lib

from .errors import Unsupported

TAGS = {'html', 'head', 'body', 'p', 'div', 'span', 'br', 'hr', 'ul', 'ol', 'li',
        'b', 'strong', 'i', 'em', 'u', 'blockquote', 'pre', 'code', 'a', 'img'}
VOID = {'br', 'hr', 'img'}
ATTRIBUTES = {'a': {'href', 'title'}, 'img': {'src', 'alt', 'width', 'height'},
              'html': {'lang'}, 'div': {'lang'}, 'p': {'lang'}, 'span': {'lang'}}


class SourceProfile(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.stack = []
        self.body_end = None
        self.complete = False
        self.counts = {}

    def fail(self, reason):
        raise Unsupported('Unsupported HTML profile: ' + reason)

    def handle_starttag(self, tag, attributes):
        if tag not in TAGS:
            self.fail('active/raw-text/style/unknown element ' + tag)
        names = [name for name, _ in attributes]
        if len(names) != len(set(names)) or set(names) - ATTRIBUTES.get(tag, set()):
            self.fail('unsupported or duplicate attributes (including styling/hiding)')
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag in {'html', 'head', 'body'}:
            if self.counts[tag] != 1:
                self.fail('duplicate document structure')
            if tag == 'html' and self.stack:
                self.fail('nested document')
            if tag in {'head', 'body'} and self.stack != ['html']:
                self.fail('document element outside html root')
            self.complete = True
        for name, value in attributes:
            if value is None or any(ord(c) < 32 for c in value):
                self.fail('malformed attribute')
            if name == 'href' and urlsplit(value).scheme not in {'https', 'http', 'mailto'}:
                self.fail('unsupported link scheme')
            if name == 'src' and not value.casefold().startswith('cid:'):
                self.fail('only retained CID images are supported')
            if name in {'width', 'height'} and (not value.isdecimal() or int(value) < 1):
                self.fail('image dimensions cannot hide a resource')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID:
            self.fail('non-void self-closing element')
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.fail('unusual/ambiguous nesting or implied closures')
        if tag == 'body':
            line, column = self.getpos()
            self.body_end = sum(len(x) for x in self.source.splitlines(keepends=True)[:line - 1]) + column
        self.stack.pop()

    def handle_decl(self, declaration):
        if declaration.casefold() != 'doctype html' or self.stack:
            self.fail('unsupported declaration')

    def handle_pi(self, data):
        self.fail('processing instruction')

    def unknown_decl(self, data):
        self.fail('unknown declaration')


def insertion_offset(source):
    parser = SourceProfile(source)
    parser.feed(source)
    parser.close()
    if parser.stack:
        parser.fail('unclosed elements')
    if parser.complete and (parser.counts.get('html') != 1 or parser.counts.get('body') != 1 or parser.body_end is None):
        parser.fail('incomplete document')
    standard = html5lib.HTMLParser(namespaceHTMLElements=False)
    tree = standard.parse(source) if parser.complete else standard.parseFragment(source)
    allowed_error = {'expected-doctype-but-got-start-tag'} if parser.complete else set()
    if any(error[1] not in allowed_error for error in standard.errors):
        parser.fail('HTML5 parse error/ambiguous context')
    for element in tree.iter():
        if isinstance(element.tag, str) and element.tag not in TAGS | {'DOCUMENT_FRAGMENT'}:
            parser.fail('unexpected HTML5 tree element')
    return parser.body_end if parser.complete else len(source)


def compose(source, note):
    offset = insertion_offset(source)
    span = '\r\n' + note + '\r\n'
    result = source[:offset] + span + source[offset:]
    insertion_offset(result)
    # Inspect the standards-parsed tree, not merely source substring presence.
    # The complete note subtree must appear as one new top-level body/fragment
    # child with its exact text and decoded anchor attributes intact.
    parser = html5lib.HTMLParser(namespaceHTMLElements=False)
    fragment = parser.parseFragment(note)
    expected_div = next(child for child in fragment if child.tag == 'div')
    def signature(node):
        return (node.tag, tuple(sorted(node.attrib.items())), node.text,
                tuple((signature(child), child.tail) for child in node))
    expected = signature(expected_div)
    complete = offset != len(source)
    before = parser.parse(source) if complete else parser.parseFragment(source)
    after = parser.parse(result) if complete else parser.parseFragment(result)
    before_root = before.find('body') if complete else before
    after_root = after.find('body') if complete else after
    old_count = sum(child.tag == 'div' and signature(child) == expected for child in before_root)
    new_count = sum(child.tag == 'div' and signature(child) == expected for child in after_root)
    if new_count != old_count + 1:
        raise Unsupported('HTML note/anchors are not exact usable top-level content')
    return result, offset, span
