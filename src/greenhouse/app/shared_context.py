"""Transfer explicit historical filters across screens without mixing scopes."""
from datetime import date

KEY = '_historical_context'
PENDING = '_historical_context_pending'
SCREEN = '_historical_context_screen'


def begin_screen(state, screen, *, historical=True):
    if state.get(SCREEN) != screen:
        state[SCREEN] = screen
        state[PENDING] = {'values': state[KEY], 'publication_matched': False, 'cycle_matched': False} if historical and KEY in state else None


def adopt_publication(state, widget_key, options, warn):
    pending = state.get(PENDING)
    if not pending:
        return
    old = pending['values']
    found = next((item for item in options if item.publication_id == old['publication_id']
                  and item.scope_id == old['scope_id'] and item.snapshot_id == old['snapshot_id']), None)
    if found is None:
        state.pop(widget_key, None)
        warn('Publication trước không có ở màn hình này; hãy chọn lại scope và kiểm tra version.')
        return
    state[widget_key] = found
    pending['publication_matched'] = True


def adopt_cycle(state, widget_key, options, warn):
    pending = state.get(PENDING)
    if not pending or not pending['publication_matched']:
        return
    old = pending['values']
    found = next((item for item in options if item.cycle_id == old['cycle_id']
                  and item.compartment_id == old['compartment_id']), None)
    if found is None:
        state.pop(widget_key, None)
        warn('Khoang/vụ trước không có trong publication này; hãy chọn lại.')
        return
    state[widget_key] = found
    pending['cycle_matched'] = True


def adopt_window(state, widget_key, cycle, warn):
    pending = state.get(PENDING)
    if not pending or not pending['cycle_matched']:
        return
    start, end = pending['values']['window']
    if not (cycle.start <= start <= end < cycle.end):
        state.pop(widget_key, None)
        warn('Khoảng ngày trước nằm ngoài vụ này; hãy chọn lại khoảng ngày.')
        return
    state[widget_key] = (start, end)


def remember(state, publication, cycle, window):
    if not isinstance(window, (tuple, list)) or len(window) != 2:
        return
    start, end = window
    if type(start) is not date or type(end) is not date or not (cycle.start <= start <= end < cycle.end):
        return
    state[KEY] = {'publication_id': publication.publication_id, 'scope_id': publication.scope_id,
                  'snapshot_id': publication.snapshot_id, 'cycle_id': cycle.cycle_id,
                  'compartment_id': cycle.compartment_id, 'window': (start, end)}
    state[PENDING] = None


def begin_backend(state,identity):
    """A different warehouse namespace starts fresh filter/intent state."""
    previous=state.get('_warehouse_identity')
    if previous!=identity:
        for key in list(state):
            if key not in ('screen_choice','_warehouse_identity'):
                del state[key]
    state['_warehouse_identity']=identity
