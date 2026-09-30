#!/usr/bin/env python3
"""Validate a Hugo build and the permanent URL migration, without network access.

Run: python3 scripts/check_site.py /path/to/hugo/output
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parents[1]
BUILD = Path(sys.argv[1]).resolve()


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids, self.links, self.canonicals = set(), [], []
        self.resource_urls = []
        self.refresh = False
        self.description = None
        self.toc_depth = 0
        self.toc_emphasis = False
        self.json_ld = []
        self.in_json_ld = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for attribute in ('href', 'src', 'poster', 'data'):
            if attribute in a:
                self.resource_urls.append(a[attribute].strip())
        if 'id' in a:
            self.ids.add(a['id'])
        if tag == 'a' and 'href' in a:
            self.links.append(a['href'])
        if tag == 'link' and a.get('rel') == 'canonical':
            self.canonicals.append(a['href'])
        if tag == 'meta':
            self.refresh |= a.get('http-equiv', '').lower() == 'refresh'
            if a.get('name') == 'description':
                self.description = a.get('content', '')
        if tag == 'nav' and a.get('id') == 'TableOfContents':
            self.toc_depth = 1
        elif tag == 'nav' and self.toc_depth:
            self.toc_depth += 1
        if self.toc_depth and tag in {'strong', 'b', 'em', 'i', 'mark'}:
            self.toc_emphasis = True
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.in_json_ld = True

    def handle_endtag(self, tag):
        if tag == 'nav' and self.toc_depth:
            self.toc_depth -= 1
        if tag == 'script':
            self.in_json_ld = False

    def handle_data(self, data):
        if self.in_json_ld:
            self.json_ld.append(json.loads(data))


def local_file(path):
    result = BUILD / unquote(path).lstrip('/')
    return result / 'index.html' if result.is_dir() else result


pages = {p: Page(p.read_text()) for p in BUILD.rglob('*.html')}
rules = {}
for line in (BUILD / '_redirects').read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    assert len(line) <= 1000, 'Cloudflare rule exceeds line limit'
    source, target, status = line.split()
    source = unquote(source)
    assert status == '301' and source not in rules, ('duplicate or non-301 rule', source)
    assert source != target, ('redirect loop', source)
    assert local_file(target).exists(), ('missing redirect target', target)
    rules[source] = target
assert len(rules) <= 2000, 'Cloudflare static redirect limit exceeded'
assert not set(rules).intersection(rules.values()), 'redirect chain'

for row in json.loads((REPO / 'data/url-migrations.json').read_text()):
    for source in (row['old'], row['old'].rstrip('/'), row['old'] + 'index.html'):
        assert rules[source] == row['new'], ('migration mismatch', source)

links = 0
canonical_pages = 0
for file, page in pages.items():
    if page.refresh or file.name == '404.html':
        continue
    assert len(page.canonicals) == 1, ('canonical count', file)
    canonical = urlsplit(page.canonicals[0])
    for value in page.resource_urls:
        resource = urlsplit(urljoin(page.canonicals[0], value))
        if resource.path.startswith('/archives/') or resource.path == '/archives':
            assert resource.scheme == 'https' and resource.netloc == 'motss-forum.github.io', ('archive resource uses wrong origin', file)
    assert re.fullmatch(r'/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)*', canonical.path), ('non-English permalink', canonical.path)
    assert not page.toc_emphasis, ('emphasis in TOC', file)
    assert page.description and '<' not in page.description and len(page.description) <= 170, ('invalid description', file)
    assert len(page.json_ld) == 1, ('missing structured data', file)
    canonical_pages += 1
    for href in page.links:
        u = urlsplit(urljoin(page.canonicals[0], href))
        if u.netloc != canonical.netloc or u.scheme not in {'http', 'https'}:
            continue
        assert unquote(u.path) not in rules, ('internal link uses legacy URL', file, href)
        target = local_file(u.path)
        assert target.exists(), ('missing internal page', file, href)
        if u.fragment and target in pages:
            assert unquote(u.fragment) in pages[target].ids, ('missing anchor', file, href)
        links += 1

sitemap = ElementTree.parse(BUILD / 'sitemap.xml')
for loc in sitemap.findall('.//{*}loc'):
    path = urlsplit(loc.text).path
    assert unquote(path) not in rules, ('legacy URL in sitemap', path)
    assert local_file(path).exists(), ('missing sitemap target', path)
    assert re.fullmatch(r'/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)*', path), ('non-English sitemap URL', path)
print(f'Passed: {canonical_pages} pages, {links} local links, {len(rules)} 301 rules; TOC, metadata and sitemap valid.')
