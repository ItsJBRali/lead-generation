from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

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


def test_run_log_preserves_users_visible_line_while_new_messages_arrive() -> None:
    log_box = FakeLogBox(bottom_fraction=0.6)

    LeadGeneratorApp._append_log(SimpleNamespace(log_box=log_box), "Council complete")

    assert ("yview", "7.0") in log_box.calls
    assert ("see", "end") not in log_box.calls


def test_run_log_continues_following_latest_message_when_already_at_bottom() -> None:
    log_box = FakeLogBox(bottom_fraction=1.0)

    LeadGeneratorApp._append_log(SimpleNamespace(log_box=log_box), "Council complete")

    assert ("see", "end") in log_box.calls


def test_read_config_accepts_empty_keyword_input(tmp_path) -> None:
    geojson_path = tmp_path / "search-area.geojson"
    geojson_path.touch()
    app = SimpleNamespace(
        geojson_entry=SimpleNamespace(get=lambda: str(geojson_path)),
        output_entry=SimpleNamespace(get=lambda: str(tmp_path)),
        start_selector=SimpleNamespace(selected_date=lambda: date(2026, 6, 1)),
        end_selector=SimpleNamespace(selected_date=lambda: date(2026, 6, 30)),
        keyword_box=SimpleNamespace(get=lambda *_args: "\n"),
        download_files_var=SimpleNamespace(get=lambda: True),
        worker_count_menu=SimpleNamespace(get=lambda: "4"),
    )

    with patch(
        "lead_generator.planning.gui.history_csv_path",
        return_value=tmp_path / "search_history.csv",
    ):
        config = LeadGeneratorApp._read_config(app)

    assert config.keywords == []
