from __future__ import annotations

import argparse
import json
from datetime import date

from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper
from lead_generator.planning.http import CouncilHttpClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape UK council planning application data.")
    subparsers = parser.add_subparsers(dest="portal", required=True)

    idox = subparsers.add_parser("idox", help="Scrape an Idox PublicAccess council portal.")
    idox.add_argument("--authority", required=True, help="Council or planning authority name.")
    idox.add_argument("--base-url", required=True, help="Council PublicAccess base URL.")
    idox.add_argument("--listing-url", help="Specific listing/search results URL to parse.")
    idox.add_argument("--start-date", type=date.fromisoformat, help="Start date as YYYY-MM-DD.")
    idox.add_argument("--end-date", type=date.fromisoformat, help="End date as YYYY-MM-DD.")
    idox.add_argument("--limit", type=int, help="Maximum applications to fetch.")
    idox.add_argument("--ca-file", help="Custom CA bundle for TLS verification.")
    idox.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Disable TLS certificate verification. Use only for local diagnostics.",
    )
    idox.add_argument(
        "--fetch-details",
        action="store_true",
        help="Fetch each detail page after discovering IDs.",
    )

    args = parser.parse_args()

    if args.portal == "idox":
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(authority=args.authority, base_url=args.base_url),
            http_client=CouncilHttpClient(
                verify_tls=not args.insecure_skip_tls_verify,
                ca_file=args.ca_file,
            ),
        )
        discovery = scraper.discover_ids(
            listing_url=args.listing_url,
            start_date=args.start_date,
            end_date=args.end_date,
            limit=args.limit,
        )
        if args.fetch_details:
            discovery.applications = [
                scraper.fetch_application(application.uid, application.url)
                for application in discovery.applications
            ]
        print(json.dumps(discovery.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
