from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from lead_generator.planning import enrichment
from lead_generator.planning.drawing_sources import classify_drawing_source


APPLICATION_FORM_TEXT = """
Planning Portal Reference: PP-12345678
Applicant Details
Name/Company
Title
Mr
First name
Adam
Surname
Client
Company Name
Acme Homes Ltd
Address
Address line 1
1 Application Site Road
Town/City
London
Postcode
N1 1AA
Applicant Contact Details
Primary number
07700 900111
Email address
adam.client@example.com
Agent Details
Name/Company
Title
Ms
First name
Jane
Surname
Smith
Company Name
Studio Arc Architects Ltd
Address
Address line 1
12 Design Road
Town/City
London
Postcode
SW1A 1AA
Contact Details
Primary number
020 7000 0000
Email address
jane@studioarc.co.uk
Description of Proposed Works
New entrance gates
"""


PROFESSIONAL_REPORT_TEXT = """
Design and Access Statement
Prepared by: Jane Smith
Studio Arc Architects Ltd
12 Design Road
London
SW1A 1AA
Telephone: 020 7123 4567
Email: projects@studioarc.co.uk

Client: Acme Homes Ltd
client.private@example.com
07700 900222
"""


def _fake_pdf(path: Path, text: str, *, application_form: bool) -> enrichment._PdfText:
    return enrichment._PdfText(
        path=path,
        text=text,
        application_form=application_form,
    )


def test_enrichment_uses_only_proposed_or_existing_drawings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        paths = {
            name: folder / name
            for name in (
                "APPLICATION_FORM.pdf",
                "Design and Access Statement.pdf",
                "Existing Elevations.pdf",
                "Proposed Plan.pdf",
            )
        }
        for path in paths.values():
            path.touch()
        documents = {
            "APPLICATION_FORM.pdf": _fake_pdf(
                paths["APPLICATION_FORM.pdf"], APPLICATION_FORM_TEXT, application_form=True
            ),
            "Design and Access Statement.pdf": _fake_pdf(
                paths["Design and Access Statement.pdf"],
                PROFESSIONAL_REPORT_TEXT,
                application_form=False,
            ),
            "Existing Elevations.pdf": _fake_pdf(
                paths["Existing Elevations.pdf"],
                "EXISTING ELEVATIONS\nDRAWING NUMBER E01\nSCALE 1:100\n"
                "Architect: Existing Studio Architects Ltd",
                application_form=False,
            ),
            "Proposed Plan.pdf": _fake_pdf(
                paths["Proposed Plan.pdf"],
                "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:50\n"
                "Proposed Design Architects Ltd\n020 7123 4567\n"
                "studio@proposed-design.co.uk\n12 Design Road\nLondon\nSW1A 1AA",
                application_form=False,
            ),
        }

        with patch.object(
            enrichment,
            "extract_pdf_text",
            side_effect=lambda path: documents[path.name],
        ):
            result = enrichment.enrich_application_folder(
                folder,
                applicant_name="Adam Client",
                agent_name="Portal Agent Ltd",
                site_address="1 Application Site Road, London, N1 1AA",
            )

    row = result.to_csv_row()
    assert row["Architect / Company Name"] == (
        "Existing Studio Architects Ltd; Proposed Design Architects Ltd"
    )
    assert row["Phone Number"] == "020 7123 4567"
    assert row["Email Address"] == "studio@proposed-design.co.uk"
    assert row["Company Address"] == "12 Design Road, London, SW1A 1AA"
    assert "Portal Agent" not in " ".join(row.values())
    assert "Studio Arc Architects Ltd" not in " ".join(row.values())
    assert result.field_sources == {
        "Architect / Company Name": ["Existing Elevations.pdf", "Proposed Plan.pdf"],
        "Phone Number": ["Proposed Plan.pdf"],
        "Email Address": ["Proposed Plan.pdf"],
        "Company Address": ["Proposed Plan.pdf"],
    }
    assert result.eligible_documents == ["Existing Elevations.pdf", "Proposed Plan.pdf"]
    assert result.rejected_documents == {
        "APPLICATION_FORM.pdf": "narrative document title",
        "Design and Access Statement.pdf": "narrative document title",
    }


def test_application_form_alone_produces_failed_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        form_path = folder / "APPLICATION_FORM.pdf"
        form_path.touch()
        document = _fake_pdf(form_path, APPLICATION_FORM_TEXT, application_form=True)

        with patch.object(enrichment, "extract_pdf_text", return_value=document):
            row = enrichment.enrich_application_folder(folder).to_csv_row()

    assert row == {
        "Architect / Company Name": "Failed",
        "Phone Number": "Failed",
        "Email Address": "Failed",
        "Company Address": "Failed",
    }


def test_partial_drawing_contact_keeps_only_available_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / "Proposed Elevations.pdf"
        path.touch()
        document = _fake_pdf(
            path,
            "PROPOSED ELEVATIONS\nDRAWING NUMBER A-201\nSCALE 1:100\n"
            "Architect: Example Studio Ltd\nstudio@example.co.uk",
            application_form=False,
        )
        with patch.object(enrichment, "extract_pdf_text", return_value=document):
            row = enrichment.enrich_application_folder(folder).to_csv_row()

    assert row == {
        "Architect / Company Name": "Example Studio Ltd",
        "Phone Number": "Failed",
        "Email Address": "studio@example.co.uk",
        "Company Address": "Failed",
    }


def test_decimal_coordinates_are_not_phone_numbers() -> None:
    assert enrichment._normalise_phone("064646.00001") == ""
    assert enrichment._normalise_phone("0.0306 0.557 0") == ""


def test_copyright_and_drawing_labels_are_not_names() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())
    enrichment.extract_professional_details(
        "XL Planning LTD\n01884 38662\ninfo@xlplanning.co.uk\n"
        "1 Fore Street\nCullompton\nDevon\nEX15 1JW\n"
        "ALL RIGHTS RESERVED Copyright (c) 2026 XL PLANNING LIMITED.\n"
        "CHECKED BY: APPROVED BY: SCALE: REV: DATE",
        "Proposed Block Plan.pdf",
        accumulator,
    )
    row = accumulator.result.to_csv_row()
    assert row["Architect / Company Name"] == "XL Planning LTD"
    assert "COPYRIGHT" not in row["Architect / Company Name"].upper()


def test_client_company_and_coordinates_are_rejected_from_drawing() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())
    enrichment.extract_professional_details(
        "PROPOSED CAR PARK\nDRAWING NUMBER 2411039-18G\nSCALE 1:500\n"
        "CLIENT\nCroudace Homes Ltd\nPROJECT\nLand at Hitcham Farm\n"
        "064646.00001\n065898.00001\n066916.00001",
        "2411039-18G - PROPOSED CAR PARK.pdf",
        accumulator,
    )

    row = accumulator.result.to_csv_row()
    assert row["Architect / Company Name"] == "Failed"
    assert row["Phone Number"] == "Failed"


def test_site_address_similarity_rejects_reformatted_site_address() -> None:
    exclusions = enrichment._Exclusions()
    exclusions.add_address("Umbrook Farm, Ashill, Cullompton EX15 3LZ")
    accumulator = enrichment._Accumulator(exclusions)

    accumulator.add_address("David and Tamsyn Cowie, Umbrook Farm, Ashill, EX15 3LZ")

    assert accumulator.result.company_addresses == []


def test_missing_values_are_marked_failed_individually() -> None:
    result = enrichment.ContactEnrichment(
        architect_company_names=["Studio Arc Architects Ltd"],
        email_addresses=["hello@studioarc.co.uk"],
    )

    assert result.to_csv_row() == {
        "Architect / Company Name": "Studio Arc Architects Ltd",
        "Phone Number": "Failed",
        "Email Address": "hello@studioarc.co.uk",
        "Company Address": "Failed",
    }


def test_long_scanned_pdf_ocr_prioritises_first_and_last_pages() -> None:
    assert enrichment._preferred_ocr_pages(20) == [0, 1, 2, 3, 18, 19]


def test_compact_application_form_filename_is_protected() -> None:
    assert enrichment.is_application_form(Path("ApplicationFormRedacted.pdf"), "")


def test_ocr_title_block_credentials_support_professional_contact() -> None:
    text = """
    MICHAEL
    SMITH
    MASI
    MCIOB
    MRICS
    139 Ballydugan
    Road
    Downpatrick
    BT30 8HG
    Tel/Fax 07802 671577
    e-mail: Info@mscbc.co.uk
    Client: Mr and Mrs Smith
    """
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    enrichment.extract_professional_details(text, "Proposed Site Plan.pdf", accumulator)

    row = accumulator.result.to_csv_row()
    assert row["Architect / Company Name"] == "MICHAEL SMITH"
    assert row["Phone Number"] == "07802 671577"
    assert row["Email Address"] == "info@mscbc.co.uk"
    assert row["Company Address"] == "139 Ballydugan, Road, Downpatrick, BT30 8HG"


def test_ocr_company_spelling_variants_are_deduplicated() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_name("Bucks Plant Care Ltd")
    accumulator.add_name("Bucks Plo.nt Co.re Ltd")

    assert accumulator.result.architect_company_names == ["Bucks Plant Care Ltd"]


@pytest.mark.parametrize(
    ("filename", "text", "eligible"),
    [
        ("Proposed Elevations.pdf", "", True),
        ("Existing Site Plan.pdf", "", True),
        ("Existing and Proposed Plans.pdf", "", True),
        ("PROPOSED_CAR_PARK.pdf", "DRAWING NUMBER 2411039 SCALE 1:500 REV G", True),
        ("PROPOSED_CAR_PARK.pdf", "Car park design summary", False),
        ("2 Drawings.pdf", "PROPOSED GATE DRAWING NUMBER 102 SCALE 1:50", True),
        ("2 Drawings.pdf", "Two drawings are enclosed", False),
        ("Drawings.pdf", "Proposed design discussed below", False),
        ("Existing Planning Statement.pdf", "DRAWING NUMBER 1 SCALE 1:100", False),
        ("Design and Access Statement.pdf", "Proposed plans are described below", False),
        ("Application Form.pdf", "Proposed elevation", False),
        ("Site Location Plan.pdf", "LOCATION PLAN SCALE 1:1250", False),
    ],
)
def test_drawing_source_boundary(filename: str, text: str, eligible: bool) -> None:
    assert classify_drawing_source(filename, text).eligible is eligible
