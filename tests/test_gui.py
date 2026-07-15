from datetime import date

from lead_generator.planning.gui import previous_week_date_range


def test_previous_week_date_range_from_midweek() -> None:
    assert previous_week_date_range(date(2026, 7, 15)) == (date(2026, 7, 6), date(2026, 7, 12))


def test_previous_week_date_range_from_monday() -> None:
    assert previous_week_date_range(date(2026, 7, 13)) == (date(2026, 7, 6), date(2026, 7, 12))


def test_previous_week_date_range_crosses_year_boundary() -> None:
    assert previous_week_date_range(date(2026, 1, 1)) == (date(2025, 12, 22), date(2025, 12, 28))
