"""Plotly figures built from day DTOs. NULL values must remain gaps: the
chart never draws a missing day as zero and never recomputes any formula.

Floats are display-only projections of exact Decimals; tables and exports keep
the contract values. This module loads only in the UI image (plotly there).
"""
import plotly.graph_objects as go

GREEN_IRR = '#2e7d5b'
GREEN_DRAIN = '#93b474'
AMBER_RATIO = '#c08a3e'


def _series(days, read):
    """None stays None so Plotly breaks the line instead of dropping to zero."""
    return [None if (value := read(day)) is None else float(value) for day in days]


def daily_figure(days):
    dates = [day.resource_date for day in days]
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=dates, y=_series(days, lambda day: day.irrigation.value), name='Irrigation (L/m²)',
        mode='lines+markers', connectgaps=False, line={'color': GREEN_IRR, 'width': 2},
        hovertemplate='%{x|%d/%m/%Y}<br>Irr: %{y} L/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=dates, y=_series(days, lambda day: day.drain.value), name='Drain (L/m²)',
        mode='lines+markers', connectgaps=False, line={'color': GREEN_DRAIN, 'width': 2},
        hovertemplate='%{x|%d/%m/%Y}<br>Drain: %{y} L/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=dates, y=_series(days, lambda day: day.drain_fraction_pct), name='Tỷ lệ drain (%)',
        mode='lines+markers', connectgaps=False, yaxis='y2',
        line={'color': AMBER_RATIO, 'width': 2, 'dash': 'dot'},
        hovertemplate='%{x|%d/%m/%Y}<br>Tỷ lệ: %{y} %<extra></extra>'))
    figure.update_layout(
        legend={'orientation': 'h', 'y': 1.12}, margin={'l': 10, 'r': 10, 't': 28, 'b': 10},
        paper_bgcolor='white', plot_bgcolor='white', hovermode='x unified',
        xaxis={'title': {'text': 'Ngày nguồn (chưa xác minh timezone)'}, 'showgrid': True},
        yaxis={'title': {'text': 'Lượng ngày (L/m²)'}, 'rangemode': 'tozero', 'zeroline': True},
        yaxis2={'title': {'text': 'Tỷ lệ drain (%)'}, 'overlaying': 'y', 'side': 'right',
                'rangemode': 'tozero', 'showgrid': False})
    return figure
