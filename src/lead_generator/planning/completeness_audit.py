"""Read-only portal contract checks; matching counts alone cannot prove completeness."""
from __future__ import annotations
import argparse
import json
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.parsing import usable_application_date


def response_evidence(payload):
    evidence = {'totals': [], 'continuation_advertised': False}
    if isinstance(payload, dict):
        is_dataset = any(key in payload for key in ('records', 'results', 'items', 'applications'))
        for key, value in payload.items():
            if is_dataset and key.casefold() in {'total', 'totalcount', 'totalrecords', 'recordcount'}:
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    evidence['totals'].append(value)
            if is_dataset and key.casefold() in {'hasmore', 'hasnextpage', 'nextpage', 'nexturl', 'next'} and value:
                evidence['continuation_advertised'] = True
            if key == 'returnValue' and isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, (dict, list)):
                nested = response_evidence(value)
                evidence['totals'].extend(nested['totals'])
                evidence['continuation_advertised'] |= nested['continuation_advertised']
    elif isinstance(payload, list):
        # Lists of records have no authoritative total by themselves.
        for value in payload:
            if isinstance(value, dict) and 'returnValue' in value:
                nested = response_evidence(value)
                evidence['totals'].extend(nested['totals'])
                evidence['continuation_advertised'] |= nested['continuation_advertised']
    return evidence


class AuditClient(CouncilHttpClient):
    def __init__(self):
        super().__init__(timeout_seconds=25, retries=1, min_delay_seconds=0.5)
        self.responses = []

    def _send(self, request, url):
        response = super()._send(request, url)
        parts = urlsplit(response.url)
        safe_query = [(k, v) for k, v in parse_qsl(parts.query) if k.casefold() not in {'sid', 'token', 'session', 'auth-session'}]
        try:
            evidence = response_evidence(json.loads(response.text))
        except ValueError:
            evidence = {'totals': [], 'continuation_advertised': False}
        self.responses.append({'url': urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), '')),
                               'status': response.status_code, 'sha256': sha256(response.text.encode()).hexdigest(), **evidence})
        return response


def classify_completeness(count, evidence, *, family):
    if evidence and evidence[-1]['continuation_advertised']:
        return 'Incomplete: response advertises further results'
    totals = [total for item in evidence for total in item['totals']]
    if totals and any(total > count for total in totals):
        return 'Incomplete: fewer unique records than advertised total'
    if totals and all(total == count for total in totals) and family != 'achieveforms':
        return 'Complete against advertised total'
    return 'Unknown: no authoritative total or date-window contract'


def audit_council(target, end_date: date):
    from lead_generator.planning.leads import planning_scraper_for_target
    large_start = end_date - timedelta(days=29)
    middle = large_start + timedelta(days=14)
    windows = [('small', end_date, end_date), ('large', large_start, end_date),
               ('large_left', large_start, middle), ('large_right', middle + timedelta(days=1), end_date),
               ('historical', large_start - timedelta(days=365), end_date - timedelta(days=365))]
    report = {'council': target.authority, 'portal': target.listing_url, 'windows': []}
    references = {}
    for label, start, end in windows:
        client = AuditClient()
        scraper = planning_scraper_for_target(target)
        original_client = getattr(scraper, 'http', None)
        if original_client and callable(getattr(original_client, 'close', None)):
            original_client.close()
        scraper.http = client
        started = monotonic()
        entry = {'window': label, 'start': start.isoformat(), 'end': end.isoformat()}
        try:
            discovery = scraper.discover_ids(listing_url=target.listing_url, start_date=start, end_date=end)
            apps = discovery.applications
            references[label] = {app.reference or app.uid for app in apps}
            entry.update(unique_records=len(references[label]),
                         undated=sum(usable_application_date(app.date_received, app.date_validated) is None for app in apps),
                         inferred_dates=sum(bool(app.raw.get('date_inferred_from_search_window')) for app in apps),
                         outside_window=sum(bool((parsed := usable_application_date(app.date_received, app.date_validated)) and not start <= parsed <= end) for app in apps),
                         completeness=classify_completeness(len(references[label]), client.responses, family=str(target.portal_family).casefold()))
        except Exception as exc:
            entry.update(completeness='Unavailable', error=str(exc))
        finally:
            scraper.close()
        entry.update(seconds=round(monotonic() - started, 3), responses=client.responses)
        report['windows'].append(entry)
        print(f'{target.authority}: {label}: {entry["completeness"]}', flush=True)
    if all(key in references for key in ('large', 'large_left', 'large_right')):
        combined = references['large_left'] | references['large_right']
        report['partition_comparison'] = {'large_count': len(references['large']), 'partition_count': len(combined),
                                          'missing_from_large': len(combined - references['large']),
                                          'missing_from_partitions': len(references['large'] - combined)}
    return report


def main():
    from lead_generator.planning.leads import CouncilTarget, load_authority_catalogue
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--councils', nargs='+', required=True)
    parser.add_argument('--end', type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalogue-portals', action='store_true', help='Audit original catalogue URLs, including portals superseded by runtime overrides')
    args = parser.parse_args()
    if args.catalogue_portals:
        from importlib import resources
        features = json.loads(resources.files('lead_generator.planning.data').joinpath('planning_authorities.geojson').read_text(encoding='utf-8'))['features']
    else:
        features = load_authority_catalogue()['features']
    targets = []
    for name in args.councils:
        feature = next((f for f in features if f['properties']['authority'].casefold() == name.casefold()), None)
        if feature is None:
            parser.error(f'Unknown council: {name}')
        p = feature['properties']
        targets.append(CouncilTarget(name, p['portal_family'], p['scraper_type'], p['base_url'], p['listing_url'], feature['geometry']))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'purpose': 'Live contract evidence, not a guarantee of all published applications', 'councils': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for target in targets:
        report['councils'].append(audit_council(target, args.end))
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
