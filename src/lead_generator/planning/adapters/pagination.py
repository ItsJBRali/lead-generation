"""Follow pagination controls actually advertised by HTML listing responses."""
from __future__ import annotations

from collections import deque
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import html

from lead_generator.planning.http import CouncilFetchError
from lead_generator.planning.parsing import clean_text


MAX_LISTING_PAGES = 1000

_POSTBACK = re.compile(r"__doPostBack\(\s*(['\"])(.*?)\1\s*,\s*(['\"])(.*?)\3\s*\)")


def _form_values(form):
    data = {}
    for node in form.xpath('.//input[@name] | .//select[@name] | .//textarea[@name]'):
        kind = (node.get('type') or '').lower()
        if kind in {'submit', 'button', 'image', 'reset', 'file'}:
            continue
        if kind in {'radio', 'checkbox'} and node.get('checked') is None:
            continue
        if node.tag == 'select':
            options = node.xpath('.//option[@selected]') or node.xpath('.//option')[:1]
            value = options[0].get('value', options[0].text_content()) if options else ''
        elif node.tag == 'textarea':
            value = node.text_content()
        else:
            value = node.get('value') or ''
        data[node.get('name')] = value
    return data


def _controls(text, page_url):
    if not text.strip():
        return []
    document = html.fromstring(text)
    controls = []
    page_match = re.search(r'\bPage\s+(\d+)\s+of\s+(\d+)\b', ' '.join(document.itertext()), re.I)
    page_number = int(page_match.group(1)) if page_match else None
    for anchor in document.xpath('//a[@href] | //link[@href]'):
        if anchor.xpath("ancestor-or-self::*[@disabled or @aria-disabled='true' or contains(concat(' ', normalize-space(@class), ' '), ' disabled ')]"):
            continue
        label = clean_text(' '.join(anchor.itertext())) or ''
        labels = list(filter(None, [label, anchor.get('title'), anchor.get('aria-label'), *anchor.xpath('.//img/@alt')]))
        is_next = 'next' in (anchor.get('rel') or '').lower().split() or any(
            re.fullmatch(r'[\s>»›→]*next(?:\s+(?:page|results?))?[\s>»›→]*', value, re.I)
            for value in labels
        )
        in_pager = anchor.xpath("ancestor::*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'pag') or contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'pag')]")
        href = anchor.get('href') or ''
        postback = _POSTBACK.search(href)
        numbered = label.isdigit() and (in_pager or (page_number is not None and postback))
        if not is_next and not numbered:
            continue
        if numbered:
            current = [int(clean_text(node.text_content()) or '0')
                       for node in (in_pager[-1].xpath('.//span[not(.//a) and not(ancestor::a)] | .//*[@aria-current="page"]') if in_pager else [])
                       if (clean_text(node.text_content()) or '').isdigit()]
            if page_number is not None:
                current.append(page_number)
            if current and int(label) <= max(current):
                continue
        if postback:
            form = next(iter(anchor.xpath('ancestor::form[1]')), None)
            if form is None:
                form = next(iter(document.xpath('//form')), None)
            if form is None:
                raise CouncilFetchError('Listing advertises a postback page without a form')
            data = _form_values(form)
            data['__EVENTTARGET'] = postback.group(2)
            data['__EVENTARGUMENT'] = postback.group(4)
            url = urljoin(page_url, form.get('action') or page_url)
            controls.append((url, data, is_next))
        elif href and not href.startswith('#') and urlsplit(urljoin(page_url, href)).scheme in {'http', 'https'}:
            controls.append((urldefrag(urljoin(page_url, href))[0], None, is_next))
    # CCED uses a forward ellipsis to open the next numeric window. A preceding
    # page number distinguishes it from the backward ellipsis at the left edge.
    if not controls and page_match and page_number < int(page_match.group(2)):
        for anchor in document.xpath('//a[@href]'):
            if (clean_text(anchor.text_content()) or '') not in {'...', '…'}:
                continue
            preceding = anchor.xpath('preceding-sibling::a | preceding-sibling::span')
            if not any((clean_text(node.text_content()) or '').isdigit() for node in preceding):
                continue
            match = _POSTBACK.search(anchor.get('href') or '')
            form = next(iter(anchor.xpath('ancestor::form[1]')), None)
            if not match or form is None:
                continue
            data = _form_values(form)
            data['__EVENTTARGET'], data['__EVENTARGUMENT'] = match.group(2), match.group(4)
            controls.append((urljoin(page_url, form.get('action') or page_url), data, True))
            break
    # A Next control avoids stale state when several numeric postbacks are present.
    controls = [control for control in controls
                if urlsplit(control[0]).netloc.casefold() == urlsplit(page_url).netloc.casefold()]
    next_controls = [control for control in controls if control[2]]
    return next_controls[:1] if next_controls else controls


def collect_listing_pages(http, response, parse_listing, *, limit=None, pagination_urls=None):
    """Collect unique results; parse_listing may also filter before the limit is counted.

    The optional callback accepts (HTML, URL) and supplies the portal-specific GET
    page URLs, replacing generic GET detection while retaining postback controls.
    It is called on every page, so later pager windows are discovered too.
    """
    if limit is not None and limit <= 0:
        return []
    applications = []
    seen_apps = set()
    seen_pages = set()
    requested = {('get', urldefrag(response.url)[0])}
    pending = deque()
    queued = set()
    page_count = 0
    while True:
        page_count += 1
        fingerprint = response.text.strip()
        if fingerprint in seen_pages and fingerprint:
            raise CouncilFetchError('Pagination returned a repeated listing page before completion')
        seen_pages.add(fingerprint)
        if fingerprint:
            for application in parse_listing(response.text, response.url):
                key = (application.reference or application.uid).strip().casefold()
                if key in seen_apps:
                    continue
                seen_apps.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return applications[:limit]
        controls = _controls(response.text, response.url)
        if pagination_urls is not None and fingerprint:
            next_urls = {url for url, data, is_next in controls if data is None and is_next}
            controls = [control for control in controls if control[1] is not None]
            for url in pagination_urls(response.text, response.url):
                absolute_url = urldefrag(urljoin(response.url, url))[0]
                controls.append((absolute_url, None, absolute_url in next_urls))
        postback_queued = False
        for url, data, is_next in controls:
            if urlsplit(url).netloc.casefold() != urlsplit(response.url).netloc.casefold():
                continue
            # Numeric postbacks identify an absolute page independently of viewstate.
            if data is None:
                key = ('get', url)
            elif not is_next:
                key = ('post', url, data['__EVENTTARGET'], data['__EVENTARGUMENT'])
            else:
                key = ('post', url, tuple(sorted(data.items())))
            if key in requested:
                if is_next:
                    raise CouncilFetchError('Pagination Next control points to an already requested page')
                continue
            if key not in queued:
                if data is not None:
                    if postback_queued:
                        continue
                    postback_queued = True
                pending.append((url, data, key))
                queued.add(key)
        if not pending:
            return applications
        if page_count >= MAX_LISTING_PAGES:
            raise CouncilFetchError(f'Pagination exceeded {MAX_LISTING_PAGES} pages with more results advertised')
        url, data, key = pending.popleft()
        requested.add(key)
        response = http.post_form(url, data) if data is not None else http.get(url)
