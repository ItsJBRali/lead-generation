from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from lead_generator.planning import enrichment
from lead_generator.planning.drawing_sources import (
    classify_drawing_source,
    preclassify_drawing_source,
)


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
            "extract_pdf_first_page_text",
            side_effect=lambda path: documents[path.name],
        ), patch.object(
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


def test_management_plan_never_enriches_even_with_drawing_markers() -> None:
    filename = "SANG_LANDSCAPE_AND_ECOLOGICAL_MANAGEMENT_PLAN.pdf"
    text = (
        "PROPOSED EXISTING PLAN\nDRAWING NUMBER M-101\nSCALE 1:100\nREVISION P1\n"
        "SLR Consulting Limited\n020 7123 4567\ncontact@slrconsulting.example\n"
        "1 Consultant Way\nLondon\nSW1A 1AA"
    )

    assert preclassify_drawing_source(filename).eligible is False
    assert preclassify_drawing_source(filename).needs_text is False
    assert classify_drawing_source(filename, text).eligible is False
    assert classify_drawing_source(
        "Document 123.pdf",
        "MANAGEMENT PLAN\nPROPOSED PLAN\nDRAWING NUMBER M-101\nSCALE 1:100",
    ).eligible is False
    assert classify_drawing_source(
        "Proposed Site Plan.pdf",
        "\n".join(["PROPOSED SITE PLAN DRAWING NUMBER P-101 SCALE 1:100 REV P1"] * 31)
        + "\nRefer to Landscape Management Plan",
    ).eligible is True

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / filename
        path.touch()
        document = _fake_pdf(path, text, application_form=False)

        with patch.object(
            enrichment,
            "extract_pdf_first_page_text",
            return_value=document,
        ):
            result = enrichment.enrich_application_folder(folder)

    assert result.to_csv_row() == {
        "Architect / Company Name": "Failed",
        "Phone Number": "Failed",
        "Email Address": "Failed",
        "Company Address": "Failed",
    }
    assert result.field_sources == {}
    assert result.eligible_documents == []
    assert result.rejected_documents == {filename: "narrative document title"}


@pytest.mark.parametrize(
    "filename",
    [
        "FRAMEWORK_TRAVEL_PLAN.pdf",
        "ENERGY_STRATEGY_AND_SUSTAINABILITY_STATEMENT.pdf",
        "LANDSCAPE_AND_VISUAL_IMPACT_ASSESSMENT.pdf",
        "ENVIRONMENTAL_STATEMENT_APPENDIX_12_PROPOSED_DRAWINGS.pdf",
        "GREEN_BELT_REVIEW.pdf",
        "EXTERNAL_MATERIALS.pdf",
        "DRAWING_REGISTER.pdf",
        "NMA_COMPARISON_PRESENTATION.pdf",
        "WORKING_METHOD_STATEMENT_WMS.pdf",
        "CONSTRUCTION_ENVIRONMENTAL_MANAGEMENT_PLAN_CEMP.pdf",
        "PLANNING_REPORT.pdf",
        "PLANNING_STATEMENT.pdf",
        "PLANNING_APPLICATION_APPENDIX.pdf",
        "PROPOSED_DRAWINGS_SUPPORTING_REPORTS.pdf",
        "PROPOSED_PLANS_TECHNICAL_ASSESSMENTS.pdf",
        "PROPOSED_LAYOUT_MATERIAL_SPECIFICATIONS.pdf",
    ],
)
def test_narrative_document_filenames_are_rejected_before_pdf_reading(
    filename: str,
) -> None:
    misleading_body = (
        "PROPOSED DRAWING\nDRAWING NUMBER A-101\nSCALE 1:100\nREVISION P1"
    )

    preliminary = preclassify_drawing_source(filename)

    assert preliminary.eligible is False
    assert preliminary.needs_text is False
    assert classify_drawing_source(filename, misleading_body).eligible is False


def test_narrative_document_is_not_read_or_ocr_processed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        filename = "FRAMEWORK_TRAVEL_PLAN.pdf"
        (folder / filename).touch()

        with patch.object(enrichment, "extract_pdf_text") as extract_pdf_text:
            result = enrichment.enrich_application_folder(folder)

    extract_pdf_text.assert_not_called()
    assert result.eligible_documents == []
    assert result.rejected_documents == {filename: "narrative document title"}


def test_later_report_body_drawing_references_cannot_make_document_eligible() -> None:
    text = "\n".join(
        [
            "GENERAL DEVELOPMENT NOTES",
            *(f"Narrative paragraph {index}" for index in range(35)),
            "PROPOSED SITE PLAN",
            "DRAWING NUMBER A-101",
            "SCALE 1:100",
            "REVISION P1",
        ]
    )

    assert classify_drawing_source("Document 123.pdf", text).eligible is False


def test_sparse_first_page_cannot_be_rescued_by_second_page_drawing_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / "Document 123.pdf"
        path.touch()
        first_page = _fake_pdf(
            path,
            "GENERAL DEVELOPMENT NOTES",
            application_form=False,
        )
        full_document = _fake_pdf(
            path,
            "GENERAL DEVELOPMENT NOTES\n\n"
            "PROPOSED SITE PLAN\nDRAWING NUMBER A-101\nSCALE 1:100",
            application_form=False,
        )

        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                return_value=first_page,
            ),
            patch.object(
                enrichment,
                "extract_pdf_text",
                return_value=full_document,
            ) as extract_pdf_text,
        ):
            result = enrichment.enrich_application_folder(folder)

    extract_pdf_text.assert_not_called()
    assert result.eligible_documents == []
    assert result.rejected_documents == {
        "Document 123.pdf": "drawing status/type evidence incomplete"
    }


def test_clear_drawing_classification_ignores_second_page_narrative_marker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / "Proposed Elevations.pdf"
        path.touch()
        first_page = _fake_pdf(
            path,
            "PROPOSED ELEVATIONS\nDRAWING NUMBER A-201\nSCALE 1:100",
            application_form=False,
        )
        full_document = _fake_pdf(
            path,
            "PROPOSED ELEVATIONS\nDRAWING NUMBER A-201\nSCALE 1:100\n\n"
            "PLANNING STATEMENT\nStudio Arc Architects Ltd",
            application_form=False,
        )

        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                return_value=first_page,
            ),
            patch.object(
                enrichment,
                "extract_pdf_text",
                return_value=full_document,
            ),
        ):
            result = enrichment.enrich_application_folder(folder)

    assert result.eligible_documents == ["Proposed Elevations.pdf"]
    assert result.rejected_documents == {}


def test_rejected_ambiguous_pdf_never_invokes_full_document_extraction() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / "Document 456.pdf"
        path.touch()
        first_page = _fake_pdf(path, "GENERAL NOTES", application_form=False)

        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                return_value=first_page,
            ),
            patch.object(enrichment, "extract_pdf_text") as extract_pdf_text,
        ):
            result = enrichment.enrich_application_folder(folder)

    extract_pdf_text.assert_not_called()
    assert result.eligible_documents == []


def test_first_page_reader_does_not_extract_or_ocr_later_pages() -> None:
    path = Path("Document 789.pdf")
    first_page = Mock()
    first_page.extract_text.return_value = ""
    second_page = Mock()
    reader = Mock()
    reader.is_encrypted = False
    reader.pages = [first_page, second_page]
    ocr_text = (
        "PROPOSED SITE PLAN\nDRAWING NUMBER A-101\nSCALE 1:100\n"
        "PROJECT TITLE RESIDENTIAL REDEVELOPMENT"
    )

    with (
        patch.object(enrichment, "PdfReader", return_value=reader),
        patch.object(
            enrichment,
            "_ocr_pdf_pages",
            return_value={0: ocr_text},
        ) as ocr_pdf_pages,
    ):
        document = enrichment.extract_pdf_first_page_text(path)

    first_page.extract_text.assert_called_once_with()
    second_page.extract_text.assert_not_called()
    ocr_pdf_pages.assert_called_once_with(path, [0])
    assert document.text == ocr_text
    assert document.ocr_pages == 1


def test_ambiguous_drawing_evidence_must_be_close_together_on_title_page() -> None:
    text = "\n".join(
        [
            "PROPOSED DEVELOPMENT",
            *(f"General note {index}" for index in range(15)),
            "SITE PLAN",
            *(f"Additional note {index}" for index in range(15)),
            "DRAWING NUMBER A-101",
            "SCALE 1:100",
        ]
    )

    assert classify_drawing_source("Document 123.pdf", text).eligible is False


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


def test_misnamed_application_form_cannot_contribute_contact_details() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        form_path = folder / "Proposed Site Plan.pdf"
        form_path.touch()
        document = _fake_pdf(form_path, APPLICATION_FORM_TEXT, application_form=True)

        with patch.object(
            enrichment,
            "extract_pdf_first_page_text",
            return_value=document,
        ):
            result = enrichment.enrich_application_folder(folder)

    assert result.to_csv_row() == {
        "Architect / Company Name": "Failed",
        "Phone Number": "Failed",
        "Email Address": "Failed",
        "Company Address": "Failed",
    }
    assert result.eligible_documents == []
    assert result.rejected_documents == {"Proposed Site Plan.pdf": "application form"}


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
        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                return_value=document,
            ),
            patch.object(enrichment, "extract_pdf_text", return_value=document),
        ):
            row = enrichment.enrich_application_folder(folder).to_csv_row()

    assert row == {
        "Architect / Company Name": "Example Studio Ltd",
        "Phone Number": "Failed",
        "Email Address": "studio@example.co.uk",
        "Company Address": "Failed",
    }


def test_enrichment_stops_reading_pdfs_once_all_fields_are_complete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        first_path = folder / "A Proposed Plan.pdf"
        later_path = folder / "B Proposed Plan.pdf"
        first_path.touch()
        later_path.touch()
        first_page = _fake_pdf(
            first_path,
            "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:100",
            application_form=False,
        )
        complete_document = _fake_pdf(
            first_path,
            "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:100\n"
            "Architect: Studio Arc Architects Ltd\n"
            "020 7123 4567\nstudio@studioarc.co.uk\n"
            "12 Design Road\nLondon\nSW1A 1AA",
            application_form=False,
        )

        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                return_value=first_page,
            ) as first_page_reader,
            patch.object(
                enrichment,
                "extract_pdf_text",
                return_value=complete_document,
            ) as full_document_reader,
        ):
            result = enrichment.enrich_application_folder(folder)

    first_page_reader.assert_called_once_with(first_path)
    full_document_reader.assert_called_once_with(first_path)
    assert result.eligible_documents == ["A Proposed Plan.pdf"]
    assert result.field_sources == {
        "Architect / Company Name": ["A Proposed Plan.pdf"],
        "Phone Number": ["A Proposed Plan.pdf"],
        "Email Address": ["A Proposed Plan.pdf"],
        "Company Address": ["A Proposed Plan.pdf"],
    }


def test_enrichment_continues_reading_pdfs_while_fields_are_missing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        first_path = folder / "A Proposed Plan.pdf"
        second_path = folder / "B Proposed Plan.pdf"
        first_path.touch()
        second_path.touch()
        first_pages = {
            path.name: _fake_pdf(
                path,
                "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:100",
                application_form=False,
            )
            for path in (first_path, second_path)
        }
        full_documents = {
            first_path.name: _fake_pdf(
                first_path,
                "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:100\n"
                "Architect: Studio Arc Architects Ltd\n"
                "020 7123 4567\nstudio@studioarc.co.uk",
                application_form=False,
            ),
            second_path.name: _fake_pdf(
                second_path,
                "PROPOSED PLAN\nDRAWING NUMBER P02\nSCALE 1:100\n"
                "Architect: Studio Arc Architects Ltd\n"
                "12 Design Road\nLondon\nSW1A 1AA",
                application_form=False,
            ),
        }

        with (
            patch.object(
                enrichment,
                "extract_pdf_first_page_text",
                side_effect=lambda path: first_pages[path.name],
            ) as first_page_reader,
            patch.object(
                enrichment,
                "extract_pdf_text",
                side_effect=lambda path: full_documents[path.name],
            ) as full_document_reader,
        ):
            result = enrichment.enrich_application_folder(folder)

    assert [call.args[0] for call in first_page_reader.call_args_list] == [
        first_path,
        second_path,
    ]
    assert [call.args[0] for call in full_document_reader.call_args_list] == [
        first_path,
        second_path,
    ]
    assert result.to_csv_row() == {
        "Architect / Company Name": "Studio Arc Architects Ltd",
        "Phone Number": "020 7123 4567",
        "Email Address": "studio@studioarc.co.uk",
        "Company Address": "12 Design Road, London, SW1A 1AA",
    }
    assert result.field_sources == {
        "Architect / Company Name": ["A Proposed Plan.pdf"],
        "Phone Number": ["A Proposed Plan.pdf"],
        "Email Address": ["A Proposed Plan.pdf"],
        "Company Address": ["B Proposed Plan.pdf"],
    }


def test_decimal_coordinates_are_not_phone_numbers() -> None:
    assert enrichment._normalise_phone("064646.00001") == ""
    assert enrichment._normalise_phone("0.0306 0.557 0") == ""


@pytest.mark.parametrize(
    "value",
    [
        "0)1494 123 456",
        "(01494 123456",
        "07.202613.07",
        "021 1234 5678",
        "022 1234 5678",
        "025 1234 5678",
        "026 1234 5678",
        "027 1234 5678",
    ],
)
def test_malformed_date_like_and_invalid_area_phone_numbers_are_rejected(
    value: str,
) -> None:
    assert enrichment._normalise_phone(value) == ""


def test_phone_parentheses_must_be_balanced_in_order() -> None:
    assert enrichment._normalise_phone("0)1494 (123456") == ""


@pytest.mark.parametrize(
    "value",
    [
        "020 7123 4567",
        "023 8012 3456",
        "024 7612 3456",
        "028 9012 3456",
        "029 2012 3456",
    ],
)
def test_valid_uk_02_area_phone_numbers_remain_accepted(value: str) -> None:
    assert enrichment._normalise_phone(value) == value


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


@pytest.mark.parametrize(
    "value",
    [
        "ALL RIGHTS RESERVED COPYRIGHT \u00c2\u00a9 2026 XL PLANNING LIMITED",
        "This drawing is the property of CUTLERURCH LTD",
        "checked and NEO Architects",
        "of NEO Architects",
        "STUDIO",
        "GROUP LIMITED",
    ],
)
def test_drawing_note_and_generic_company_names_are_rejected(value: str) -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_name(value)

    assert accumulator.result.architect_company_names == []


def test_valid_company_name_remains_accepted_after_noise_filtering() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_name("NEO Architects")

    assert accumulator.result.architect_company_names == ["NEO Architects"]


def test_company_address_is_safely_trimmed_after_first_postcode() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_address(
        "Motion, Quadrant House, Broad Street Mall, Reading, RG1 7QE "
        "Tel: 0118 467 4498 www.motion.co.uk"
    )

    assert accumulator.result.company_addresses == [
        "Motion, Quadrant House, Broad Street Mall, Reading, RG1 7QE"
    ]


@pytest.mark.parametrize(
    "value",
    [
        "This drawing is the property of CUTLERURCH LTD, 1 Design Road, "
        "London, SW1A 1AA",
        "construction. This drawing, 2 Design Road, London, SW1A 1AA",
        "Studio Arc Ltd, www.studioarc.co.uk, 3 Design Road, London, SW1A 1AA",
        "Studio Arc Ltd, www studioarc co uk, 3 Design Road, London, SW1A 1AA",
        "Studio Arc Ltd, Tel: 020 7123 4567, 4 Design Road, London, SW1A 1AA",
        "Studio Arc Ltd, Tel, 4 Design Road, London, SW1A 1AA",
        "Copyright notice SW1A 1AA Telephone 020 7123 4567",
    ],
)
def test_company_addresses_containing_drawing_or_contact_prose_are_rejected(
    value: str,
) -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_address(value)

    assert accumulator.result.company_addresses == []


@pytest.mark.parametrize(
    "value",
    [
        "246317sionthaysen@yahoo.co.uk",
        "hello@example.co.uk.co.uk",
        "hello@example.co.ukinfo",
        "hello@example.comcontact",
        "studio@practice.co.ukinfo@other.co.uk",
        "michael@@neoarchitects.co.uk",
    ],
)
def test_damaged_email_addresses_are_rejected(value: str) -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_email(value)

    assert accumulator.result.email_addresses == []


def test_one_character_ocr_email_variants_are_deduplicated_per_domain() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_email("michael@neoarchitects.co.uk")
    accumulator.add_email("nichael@neoarchitects.co.uk")

    assert accumulator.result.email_addresses == ["michael@neoarchitects.co.uk"]


def test_legitimate_close_same_domain_emails_remain_distinct() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())

    accumulator.add_email("john@example.com")
    accumulator.add_email("joan@example.com")

    assert accumulator.result.email_addresses == [
        "john@example.com",
        "joan@example.com",
    ]


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


def test_design_label_starts_a_professional_block_after_client_context() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())
    enrichment.extract_professional_details(
        "CLIENT\nCroudace Homes Ltd\nDESIGN\nPenchard Architects Ltd",
        "Proposed Site Plan.pdf",
        accumulator,
    )

    assert accumulator.result.architect_company_names == ["Penchard Architects Ltd"]


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
    "filename",
    [
        "Proposed Travel Plans.pdf",
        "Existing Travel Plans.pdf",
        "Proposed Drawing Registers.pdf",
        "Existing Drawing Registers.pdf",
        "Proposed External Material Finishes Plan.pdf",
        "Existing External Material Finishes Plan.pdf",
        "Proposed ApplicationForm Drawing.pdf",
        "Existing ApplicationForm Drawing.pdf",
        "Proposed PlanningApplicationAppendix Drawings.pdf",
        "Existing PlanningApplicationAppendix Drawings.pdf",
    ],
)
def test_compact_and_plural_narrative_filenames_are_rejected(
    filename: str,
) -> None:
    decision = preclassify_drawing_source(filename)

    assert decision.eligible is False
    assert decision.needs_text is False
    assert classify_drawing_source(
        filename,
        "PROPOSED DRAWING\nDRAWING NUMBER A-101\nSCALE 1:100",
    ).eligible is False


def test_ambiguous_car_park_accepts_late_first_page_title_block_evidence() -> None:
    text = "\n".join(
        [
            *(f"Project header {index}" for index in range(12)),
            "DRAWING NUMBER 2411039",
            "SCALE 1:500",
        ]
    )

    assert classify_drawing_source("PROPOSED_CAR_PARK.pdf", text).eligible is True


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
