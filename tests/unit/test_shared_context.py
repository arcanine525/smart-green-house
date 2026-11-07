from datetime import date
from types import SimpleNamespace

from greenhouse.app import shared_context as context


def fixtures():
    publication = SimpleNamespace(publication_id='pub', scope_id='scope', snapshot_id='snapshot')
    cycle = SimpleNamespace(cycle_id='cycle', compartment_id='compartment',
                            start=date(2020, 1, 1), end=date(2020, 2, 1))
    return publication, cycle


def test_navigation_preserves_exact_publication_cycle_and_window():
    publication, cycle = fixtures()
    state, warnings = {}, []
    window = (date(2020, 1, 3), date(2020, 1, 7))
    context.remember(state, publication, cycle, window)
    context.begin_screen(state, 'climate')
    context.adopt_publication(state, 'pub', [publication], warnings.append)
    context.adopt_cycle(state, 'cycle', [cycle], warnings.append)
    context.adopt_window(state, 'window', cycle, warnings.append)
    assert (state['pub'], state['cycle'], state['window']) == (publication, cycle, window)
    assert warnings == []


def test_new_publication_or_scope_never_silently_inherits_window():
    publication, cycle = fixtures()
    state, warnings = {}, []
    context.remember(state, publication, cycle, (cycle.start, cycle.start))
    context.begin_screen(state, 'water')
    replacement = SimpleNamespace(publication_id='pub', scope_id='other', snapshot_id='snapshot')
    context.adopt_publication(state, 'pub', [replacement], warnings.append)
    context.adopt_cycle(state, 'cycle', [cycle], warnings.append)
    context.adopt_window(state, 'window', cycle, warnings.append)
    assert 'window' not in state
    assert len(warnings) == 1


def test_replay_scope_does_not_adopt_historical_context():
    publication, cycle = fixtures()
    state = {}
    context.remember(state, publication, cycle, (cycle.start, cycle.start))
    context.begin_screen(state, 'replay', historical=False)
    context.adopt_publication(state, 'pub', [publication], lambda _: None)
    assert 'pub' not in state


def test_unavailable_transfer_does_not_reset_explicit_replacement_after_failed_render():
    publication, cycle = fixtures()
    replacement = SimpleNamespace(publication_id='replacement', scope_id='other', snapshot_id='snapshot')
    state, warnings = {}, []
    context.remember(state, publication, cycle, (cycle.start, cycle.start))
    context.begin_screen(state, 'climate')
    context.adopt_publication(state, 'pub', [replacement], warnings.append)
    state['pub'] = replacement  # User chooses after a failed initial render.
    context.adopt_publication(state, 'pub', [replacement], warnings.append)
    assert state['pub'] == replacement
    assert state[context.PENDING] is None
    assert len(warnings) == 1


def test_failed_pin_does_not_reapply_available_transfer_over_user_selection():
    publication, cycle = fixtures()
    replacement = SimpleNamespace(publication_id='replacement', scope_id='other', snapshot_id='another')
    state = {}
    context.remember(state, publication, cycle, (cycle.start, cycle.start))
    context.begin_screen(state, 'climate')
    context.adopt_publication(state, 'pub', [publication, replacement], lambda _: None)
    state['pub'] = replacement  # No remember() call: the original pin failed.
    context.adopt_publication(state, 'pub', [publication, replacement], lambda _: None)
    context.adopt_cycle(state, 'cycle', [cycle], lambda _: None)
    context.adopt_window(state, 'window', cycle, lambda _: None)
    assert state['pub'] == replacement
    assert state[context.PENDING] is None
    assert 'window' not in state
