import json
import queue
import threading
from pathlib import Path
from importlib import resources
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lead_generator.planning import leads
from lead_generator.planning.gui import LeadGeneratorApp


def test_country_catalogue_selects_all_areas_without_polygon_comparisons():
    catalogue = leads.load_authority_catalogue()
    boundary = json.loads(json.dumps(catalogue))
    # Authority properties are not a trusted shortcut: only geometry may match.
    for feature in boundary['features']:
        feature['properties'] = {'authority': 'search area'}
    with patch.object(leads, 'geometries_intersect', side_effect=AssertionError('Exact boundary comparison should be unnecessary')):
        targets = leads.select_overlapping_authorities(boundary, catalogue)
    assert [t.authority for t in targets] == [f['properties']['authority'] for f in catalogue['features']]


def test_gui_yields_before_processing_entire_message_backlog():
    messages = queue.Queue()
    for number in range(1000):
        messages.put(('log', f'Message {number}'))
    logged = []
    app = SimpleNamespace(messages=messages, _append_log=logged.append, after=Mock(), _poll_messages=Mock())
    LeadGeneratorApp._poll_messages(app)
    assert 0 < len(logged) < 1000
    assert logged == [f'Message {number}' for number in range(len(logged))]
    assert not messages.empty()
    app.after.assert_called_once()


def test_start_search_with_country_geojson_keeps_cancel_responsive(tmp_path):
    boundary = tmp_path / 'authorities.geojson'
    boundary.write_text(resources.files('lead_generator.planning.data').joinpath('planning_authorities.geojson').read_text(encoding='utf-8'), encoding='utf-8')
    app = LeadGeneratorApp()
    app.withdraw()
    app._set_entry(app.geojson_entry, str(boundary))
    app._set_entry(app.output_entry, str(tmp_path))
    search_started = threading.Event()
    release_search = threading.Event()
    cancelled_from_gui = []
    warnings = []

    def discover(*args, **kwargs):
        search_started.set()
        release_search.wait(timeout=5)
        return []

    def cancel_when_search_starts():
        if search_started.is_set():
            app._cancel_run()
            cancelled_from_gui.append(True)
            release_search.set()
        else:
            app.after(20, cancel_when_search_starts)

    def finish(title, message):
        warnings.append(title)
        app.quit()

    try:
        with patch.object(leads, 'discover_portal_applications', side_effect=discover), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]), patch('lead_generator.planning.gui.messagebox.showwarning', side_effect=finish), patch('lead_generator.planning.gui.messagebox.showerror', side_effect=finish):
            app.after(20, cancel_when_search_starts)
            app.after(10000, app.quit)
            app.run_button.invoke()
            app.mainloop()
        assert cancelled_from_gui == [True]
        assert warnings == ['Search cancelled']
        assert app.run_button.cget('state') == 'normal'
        assert list(tmp_path.rglob('applications.csv'))
    finally:
        app.cancel_requested = True
        release_search.set()
        if app.worker:
            app.worker.join(timeout=5)
        app.destroy()
