from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortalSignature:
    family: str
    markers: tuple[str, ...]


PORTAL_SIGNATURES: tuple[PortalSignature, ...] = (
    PortalSignature("idox", ("publicaccess", "applicationdetails.do", "online-applications")),
    PortalSignature("ocella", ("ocella", "ocellaweb", "ocella planning")),
    PortalSignature("northgate", ("northgate", "planning explorer", "general_search.aspx")),
    PortalSignature("civica", ("civica", "authority public access", "planningexplorer", "webforms/planning/details.html")),
    PortalSignature("agile", ("agile applications", "wphappdetail.displayurl", "/apas/run/")),
    PortalSignature("achieveforms", ("achieveforms", "fs.formdefinition", "form_uri=sandbox-publish", "/fillform/")),
    PortalSignature("atrium", ("/search/advanced", "/search/results", "/planning/display/", "list of planning cases - search results")),
)


def detect_portal_family(html_text: str, url: str = "") -> str | None:
    haystack = f"{url}\n{html_text}".lower()
    for signature in PORTAL_SIGNATURES:
        if any(marker in haystack for marker in signature.markers):
            return signature.family
    return None
