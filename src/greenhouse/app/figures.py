"""Plotly figures built from day DTOs. NULL values must remain gaps: the
chart never draws a missing day as zero and never recomputes any formula.

Floats are display-only projections of exact Decimals; tables and exports keep
the contract values. This module loads only in the UI image (plotly there).
"""
from datetime import timedelta

import plotly.graph_objects as go

GREEN_IRR = '#2e7d5b'
GREEN_DRAIN = '#93b474'
AMBER_RATIO = '#c08a3e'
HARVEST_A = '#2e7d5b'
HARVEST_B = '#b6784a'
HARVEST_CUM = '#4f6b8f'
HARVEST_TOTAL = '#93a8c4'
HARVEST_SHARE = '#8a6420'


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


def _harvest_series(rows, read):
    """None stays None: unknown cells are gaps, never drawn as zero."""
    return [None if (value := read(row)) is None else float(value) for row in rows]


def _harvest_axis(rows, read):
    return [read(row) for row in rows]


def harvest_day_figure(days, summary):
    """Stacked A/B kg/m² bars with cumulative-from-origin lines and the B
    share. Day rows with no record leave bars empty (NULL is a gap); verified
    zero days draw an explicit 0 bar."""
    x = _harvest_axis(days, lambda day: day.harvest_date)
    figure = go.Figure()
    figure.add_trace(go.Bar(
        x=x, y=_harvest_series(days, lambda day: day.prod_a), name='ProdA observed (kg/m²)',
        marker_color=HARVEST_A,
        hovertemplate='%{x|%d/%m/%Y}<br>ProdA: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Bar(
        x=x, y=_harvest_series(days, lambda day: day.prod_b), name='ProdB observed (kg/m²)',
        marker_color=HARVEST_B,
        hovertemplate='%{x|%d/%m/%Y}<br>ProdB: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(days, lambda day: day.cumulative_a), name='Lũy kế A từ đầu vụ (kg/m²)',
        mode='lines+markers', connectgaps=False, line={'color': HARVEST_CUM, 'width': 2},
        yaxis='y2', hovertemplate='%{x|%d/%m/%Y}<br>Lũy kế A: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(days, lambda day: day.cumulative_total),
        name='Lũy kế total từ đầu vụ (kg/m²)',
        mode='lines+markers', connectgaps=False, line={'color': HARVEST_TOTAL, 'width': 2, 'dash': 'dot'},
        yaxis='y2', hovertemplate='%{x|%d/%m/%Y}<br>Lũy kế total: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(days, lambda day: day.class_b_share_pct), name='Share B ngày (%)',
        mode='lines+markers', connectgaps=False,
        line={'color': HARVEST_SHARE, 'width': 2, 'dash': 'dot'}, yaxis='y3',
        hovertemplate='%{x|%d/%m/%Y}<br>Share B: %{y} %<extra></extra>'))
    figure.update_layout(
        barmode='stack',
        legend={'orientation': 'h', 'y': 1.16}, margin={'l': 10, 'r': 60, 't': 34, 'b': 10},
        paper_bgcolor='white', plot_bgcolor='white', hovermode='x unified',
        xaxis={'title': {'text': 'Ngày nguồn (khoanh theo cycle nguồn)'}, 'showgrid': True},
        yaxis={'title': {'text': 'Khối lượng ngày (kg/m², stacked A+B)'},
               'rangemode': 'tozero', 'zeroline': True},
        yaxis2={'title': {'text': f'Lũy kế từ đầu vụ (kg/m²) · origin '
                                  f'{summary.cumulative_origin.isoformat() if summary.cumulative_origin else "—"}'},
                'overlaying': 'y', 'side': 'right', 'rangemode': 'tozero', 'showgrid': False},
        yaxis3={'title': {'text': 'Share B ngày (%)'}, 'overlaying': 'y', 'side': 'right',
                'position': 0.92, 'rangemode': 'tozero', 'showgrid': False})
    return figure


def harvest_week_figure(weekly):
    """Stacked per-week observed A/B; share-B line; cumulative carried by the
    weekly rows (unknown weeks are NULL gaps, never 0)."""
    x = _harvest_axis(weekly, lambda s: f'{s.iso_year}-W{s.iso_week:02d}' if s.iso_year else
                      s.window_start.isoformat() + '→' + (s.window_end - timedelta(days=1)).isoformat())
    figure = go.Figure()
    figure.add_trace(go.Bar(
        x=x, y=_harvest_series(weekly, lambda s: s.prod_a_observed), name='ProdA observed (kg/m²)',
        marker_color=HARVEST_A, hovertemplate='%{x}<br>ProdA: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Bar(
        x=x, y=_harvest_series(weekly, lambda s: s.prod_b_observed), name='ProdB observed (kg/m²)',
        marker_color=HARVEST_B, hovertemplate='%{x}<br>ProdB: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(weekly, lambda s: s.cumulative_a),
        name='Lũy kế A từ đầu vụ (kg/m²)', mode='lines+markers', connectgaps=False,
        line={'color': HARVEST_CUM, 'width': 2}, yaxis='y2',
        hovertemplate='%{x}<br>Lũy kế A: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(weekly, lambda s: s.cumulative_total),
        name='Lũy kế total từ đầu vụ (kg/m²)', mode='lines+markers', connectgaps=False,
        line={'color': HARVEST_TOTAL, 'width': 2, 'dash': 'dot'}, yaxis='y2',
        hovertemplate='%{x}<br>Lũy kế total: %{y} kg/m²<extra></extra>'))
    figure.add_trace(go.Scatter(
        x=x, y=_harvest_series(weekly, lambda s: s.class_b_share_pct), name='Share B tuần (%)',
        mode='lines+markers', connectgaps=False,
        line={'color': HARVEST_SHARE, 'width': 2, 'dash': 'dot'}, yaxis='y3',
        hovertemplate='%{x}<br>Share B: %{y} %<extra></extra>'))
    figure.update_layout(
        barmode='stack',
        legend={'orientation': 'h', 'y': 1.16}, margin={'l': 10, 'r': 60, 't': 34, 'b': 10},
        paper_bgcolor='white', plot_bgcolor='white', hovermode='x unified',
        xaxis={'title': {'text': 'Tuần ISO (kg/m², tuần là tổng observed)'}, 'showgrid': True},
        yaxis={'title': {'text': 'Khối lượng tuần (kg/m², stacked A+B)'},
               'rangemode': 'tozero', 'zeroline': True},
        yaxis2={'title': {'text': 'Lũy kế từ đầu vụ (kg/m²)'}, 'overlaying': 'y', 'side': 'right',
                'rangemode': 'tozero', 'showgrid': False},
        yaxis3={'title': {'text': 'Share B tuần (%)'}, 'overlaying': 'y', 'side': 'right',
                'position': 0.92, 'rangemode': 'tozero', 'showgrid': False})
    return figure
