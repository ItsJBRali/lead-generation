from datetime import date
from types import SimpleNamespace

from lead_generator.planning.gui import LeadGeneratorApp, previous_week_date_range


def test_previous_week_date_range_from_midweek() -> None:
    assert previous_week_date_range(date(2026, 7, 15)) == (date(2026, 7, 6), date(2026, 7, 12))


def test_previous_week_date_range_from_monday() -> None:
    assert previous_week_date_range(date(2026, 7, 13)) == (date(2026, 7, 6), date(2026, 7, 12))


def test_previous_week_date_range_crosses_year_boundary() -> None:
    assert previous_week_date_range(date(2026, 1, 1)) == (date(2025, 12, 22), date(2025, 12, 28))


class FakeLogBox:
    def __init__(self, *, bottom_fraction: float, top_index: str = "7.0") -> None:
        self.bottom_fraction = bottom_fraction
        self.top_index = top_index
        self.calls: list[tuple[object, ...]] = []

    def index(self, value: str) -> str:
        assert value == "@0,0"
        return self.top_index

    def yview(self, *args):
        if args:
            self.calls.append(("yview", *args))
            return None
        return (0.25, self.bottom_fraction)

    def configure(self, **kwargs) -> None:
        self.calls.append(("configure", kwargs))

    def insert(self, index: str, value: str) -> None:
        self.calls.append(("insert", index, value))

    def see(self, index: str) -> None:
        self.calls.append(("see", index))


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


def test_run_log_preserves_users_visible_line_while_new_messages_arrive() -> None:
    log_box = FakeLogBox(bottom_fraction=0.6)

    LeadGeneratorApp._append_log(SimpleNamespace(log_box=log_box), "Council complete")

    assert ("yview", "7.0") in log_box.calls
    assert ("see", "end") not in log_box.calls


def test_run_log_continues_following_latest_message_when_already_at_bottom() -> None:
    log_box = FakeLogBox(bottom_fraction=1.0)

    LeadGeneratorApp._append_log(SimpleNamespace(log_box=log_box), "Council complete")

    assert ("see", "end") in log_box.calls


def test_enrichment_progress_shows_application_count() -> None:
    label = FakeLabel()

    LeadGeneratorApp._set_enrichment_progress(
        SimpleNamespace(enrichment_label=label),
        3,
        8,
        requested=True,
    )

    assert label.text == "3 of 8 applications enriched"


def test_document_progress_shows_application_count() -> None:
    label = FakeLabel()

    LeadGeneratorApp._set_document_progress(
        SimpleNamespace(enrichment_label=label),
        3,
        8,
        requested=True,
    )

    assert label.text == "3 of 8 applications downloaded"


def test_run_progress_ignores_out_of_order_callback_counts() -> None:
    captured_label = FakeLabel()
    captured_app = SimpleNamespace(captured_label=captured_label)
    LeadGeneratorApp._set_captured(captured_app, 4)
    LeadGeneratorApp._set_captured(captured_app, 2)

    document_label = FakeLabel()
    document_app = SimpleNamespace(enrichment_label=document_label)
    LeadGeneratorApp._set_document_progress(
        document_app,
        4,
        6,
        requested=True,
    )
    LeadGeneratorApp._set_document_progress(
        document_app,
        2,
        8,
        requested=True,
    )

    enrichment_label = FakeLabel()
    enrichment_app = SimpleNamespace(enrichment_label=enrichment_label)
    LeadGeneratorApp._set_enrichment_progress(
        enrichment_app,
        5,
        7,
        requested=True,
    )
    LeadGeneratorApp._set_enrichment_progress(
        enrichment_app,
        3,
        6,
        requested=True,
    )

    assert captured_label.text == "4 relevant applications captured"
    assert document_label.text == "4 of 8 applications downloaded"
    assert enrichment_label.text == "5 of 7 applications enriched"


def test_run_progress_reset_allows_zero_counts_for_a_new_run() -> None:
    captured_label = FakeLabel()
    progress_label = FakeLabel()
    app = SimpleNamespace(
        captured_label=captured_label,
        enrichment_label=progress_label,
    )
    LeadGeneratorApp._set_captured(app, 4)
    LeadGeneratorApp._set_document_progress(app, 4, 6, requested=True)
    LeadGeneratorApp._set_enrichment_progress(app, 5, 7, requested=True)

    LeadGeneratorApp._reset_run_progress_counts(app)
    LeadGeneratorApp._set_captured(app, 0)
    LeadGeneratorApp._set_document_progress(app, 0, 0, requested=True)

    assert captured_label.text == "0 relevant applications captured"
    assert progress_label.text == "0 of 0 applications downloaded"

    LeadGeneratorApp._set_enrichment_progress(app, 0, 0, requested=True)
    assert progress_label.text == "0 of 0 applications enriched"


def test_enrichment_progress_explains_when_not_requested() -> None:
    label = FakeLabel()

    LeadGeneratorApp._set_enrichment_progress(
        SimpleNamespace(enrichment_label=label),
        0,
        0,
        requested=False,
    )

    assert label.text == "PDF enrichment not requested"
