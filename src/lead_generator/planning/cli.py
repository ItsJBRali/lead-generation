from __future__ import annotations

import argparse
import json
from datetime import date

from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.civica import CivicaCouncilConfig, CivicaPlanningScraper
from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper
from lead_generator.planning.adapters.northgate import NorthgateCouncilConfig, NorthgatePlanningScraper
from lead_generator.planning.adapters.ocella import OcellaCouncilConfig, OcellaPlanningScraper
from lead_generator.planning.http import CouncilHttpClient


def add_labelled_portal_parser(subparsers: argparse._SubParsersAction, name: str, help_text: str) -> None:
    portal = subparsers.add_parser(name, help=help_text)
    portal.add_argument("--authority", required=True, help="Council or planning authority name.")
    portal.add_argument("--base-url", required=True, help="Council planning-register base URL.")
    portal.add_argument("--listing-url", required=True, help="Listing/search results URL to parse.")
    portal.add_argument("--limit", type=int, help="Maximum applications to return.")
    portal.add_argument(
        "--fetch-details",
        action="store_true",
        help="Fetch each detail page after discovering IDs.",
    )
    portal.add_argument(
        "--fetch-documents",
        action="store_true",
        help="Include document attachment metadata from application pages.",
    )


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
    idox.add_argument(
        "--fetch-documents",
        action="store_true",
        help="Include document attachment metadata from each application.",
    )

    ocella = subparsers.add_parser("ocella", help="Scrape an Ocella-style planning register.")
    ocella.add_argument("--authority", required=True, help="Council or planning authority name.")
    ocella.add_argument("--base-url", required=True, help="Council planning-register base URL.")
    ocella.add_argument("--listing-url", required=True, help="Listing/search results URL to parse.")
    ocella.add_argument("--limit", type=int, help="Maximum applications to return.")
    ocella.add_argument(
        "--fetch-details",
        action="store_true",
        help="Fetch each detail page after discovering IDs.",
    )
    ocella.add_argument(
        "--fetch-documents",
        action="store_true",
        help="Include document attachment metadata from application pages.",
    )
    add_labelled_portal_parser(
        subparsers,
        "civica",
        "Scrape a Civica / Authority Public Access planning register.",
    )
    add_labelled_portal_parser(
        subparsers,
        "agile",
        "Scrape an Agile Applications / APAS planning register.",
    )
    add_labelled_portal_parser(
        subparsers,
        "northgate",
        "Scrape a Northgate Planning Explorer planning register.",
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
                scraper.fetch_application(
                    application.uid,
                    application.url,
                    include_documents=args.fetch_documents,
                )
                for application in discovery.applications
            ]
        elif args.fetch_documents:
            for application in discovery.applications:
                application.documents = scraper.fetch_documents(application.uid)
        print(json.dumps(discovery.to_dict(), indent=2, sort_keys=True))

    if args.portal == "ocella":
        scraper = OcellaPlanningScraper(
            OcellaCouncilConfig(authority=args.authority, base_url=args.base_url)
        )
        discovery = scraper.discover_ids(listing_url=args.listing_url, limit=args.limit)
        if args.fetch_details:
            discovery.applications = [
                scraper.fetch_application(
                    application.uid,
                    application.url,
                    include_documents=args.fetch_documents,
                )
                for application in discovery.applications
            ]
        print(json.dumps(discovery.to_dict(), indent=2, sort_keys=True))

    if args.portal in ("civica", "agile", "northgate"):
        scraper_classes = {
            "civica": (CivicaCouncilConfig, CivicaPlanningScraper),
            "agile": (AgileCouncilConfig, AgilePlanningScraper),
            "northgate": (NorthgateCouncilConfig, NorthgatePlanningScraper),
        }
        config_class, scraper_class = scraper_classes[args.portal]
        scraper = scraper_class(config_class(authority=args.authority, base_url=args.base_url))
        discovery = scraper.discover_ids(listing_url=args.listing_url, limit=args.limit)
        if args.fetch_details:
            discovery.applications = [
                scraper.fetch_application(
                    application.uid,
                    application.url,
                    include_documents=args.fetch_documents,
                )
                for application in discovery.applications
            ]
        print(json.dumps(discovery.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
