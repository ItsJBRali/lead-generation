from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from lead_generator.planning import enrichment


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


def test_enrichment_combines_agent_details_and_excludes_client_contacts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        form_path = folder / "APPLICATION_FORM.pdf"
        report_path = folder / "Design and Access Statement.pdf"
        form_path.touch()
        report_path.touch()
        documents = {
            form_path.name: _fake_pdf(form_path, APPLICATION_FORM_TEXT, application_form=True),
            report_path.name: _fake_pdf(report_path, PROFESSIONAL_REPORT_TEXT, application_form=False),
        }

        with patch.object(enrichment, "extract_pdf_text", side_effect=lambda path: documents[path.name]):
            result = enrichment.enrich_application_folder(
                folder,
                applicant_name="Adam Client",
                agent_name="Jane Smith",
                site_address="1 Application Site Road, London, N1 1AA",
            )

    row = result.to_csv_row()
    assert row["Architect / Company Name"] == "Jane Smith (Studio Arc Architects Ltd)"
    assert row["Phone Number"] == "020 7123 4567"
    assert row["Email Address"] == "projects@studioarc.co.uk"
    assert row["Company Address"] == "12 Design Road, London, SW1A 1AA"
    assert "Acme" not in " ".join(row.values())
    assert "client.private" not in row["Email Address"]
    assert "07700 900222" not in row["Phone Number"]


def test_application_form_never_supplies_phone_or_email() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        form_path = folder / "APPLICATION_FORM.pdf"
        form_path.touch()
        document = _fake_pdf(form_path, APPLICATION_FORM_TEXT, application_form=True)

        with patch.object(enrichment, "extract_pdf_text", return_value=document):
            row = enrichment.enrich_application_folder(folder).to_csv_row()

    assert row["Architect / Company Name"] == "Jane Smith (Studio Arc Architects Ltd)"
    assert row["Company Address"] == "12 Design Road, London, SW1A 1AA"
    assert row["Phone Number"] == "Failed"
    assert row["Email Address"] == "Failed"


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
