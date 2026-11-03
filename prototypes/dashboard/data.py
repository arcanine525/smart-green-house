"""Deterministic UI fixtures. Not AGC observations or an implementation of six BRs."""
from datetime import datetime, timedelta
from decimal import Decimal
import csv
import io
import math

import plotly.graph_objects as go

HOUSES = {"GH01": "Khoang 01", "GH02": "Khoang 02", "GH03": "Khoang 03"}
DAYS = ["2020-04-12", "2020-04-13"]
METRICS = {
    "temperature": {"label": "Nhiệt độ", "unit": "°C", "low": 20, "high": 25, "range": [18, 29]},
    "co2": {"label": "CO₂", "unit": "ppm", "low": 650, "high": 950, "range": [450, 1200]},
    "ec": {"label": "EC drain", "unit": "dS/m", "low": 2, "high": 5, "range": [1, 6]},
    "ph": {"label": "pH drain", "unit": "pH", "low": 5.5, "high": 6.5, "range": [4.8, 7]},
}
GREEN = "#23664e"
AMBER = "#bc743b"


def fixture(house="GH02", day=DAYS[0]):
    offset = (int(house[-1]) - 2) * .32 + DAYS.index(day) * .12
    start = datetime.fromisoformat(day)
    rows = []
    for i in range(288):
        h = i / 12
        values = {
            "temperature": 22 + offset + 1.4 * math.sin((h-6)*math.pi/12) + .12*math.sin(i*.7) + 3.4*math.exp(-((h-14.25)/.65)**2),
            "co2": 790 + offset*60 + 120*math.sin((h-7)*math.pi/12) + 180*math.exp(-((h-14.25)/.75)**2),
            "ec": 3.2 + offset*.3 + .25*math.sin(i/24) + 2.25*math.exp(-((h-16)/.65)**2),
            "ph": 5.9 + offset*.1 + .2*math.sin(i/23) + .8*math.exp(-((h-17)/.65)**2),
        }
        row = {"event_id": f"ui-v1-{house}-{day}-{i:03d}", "time": (start+timedelta(minutes=i*5)).isoformat()}
        for metric, value in values.items():
            quality = "missing" if 104 <= i < 112 else "suspect" if metric == "ph" and i in (150,151) else "accepted"
            row[metric] = round(value, 3) if quality == "accepted" else None
            row[f"{metric}_raw"] = 0 if quality == "suspect" else row[metric]
            row[f"{metric}_quality"] = quality
        rows.append(row)
    summaries = {key: summarize(rows, key) for key in METRICS}
    scale = Decimal(str(1 + (int(house[-1])-2)*.1 + DAYS.index(day)*.05))
    quantities = {"heat": Decimal("100")*scale, "peak": Decimal("10")*scale, "offpeak": Decimal("5")*scale}
    resources = {"irrigation": float(Decimal("4")*scale), "drain": float(Decimal("1.4")*scale), "a": float(Decimal("3")*scale), "b": float(Decimal("0.2")*scale)}
    return {"house": house, "house_label": HOUSES[house], "day": day, "rows": rows,
            "summaries": summaries, "resources": resources,
            "costs": {str(p): cost(quantities, p) for p in range(-30, 101, 5)},
            "quantities": {k: float(v) for k,v in quantities.items()},
            "publication": f"ui-demo-v1-{house}-{day}", "scope": "synthetic-ui-review"}


def summarize(rows, metric):
    spec = METRICS[metric]
    valid = [r for r in rows if r[metric] is not None]
    out = [r for r in valid if not spec["low"] <= r[metric] <= spec["high"]]
    episodes, current = [], []
    for row in rows + [None]:
        if row and row[metric] is not None and not spec["low"] <= row[metric] <= spec["high"]:
            current.append(row)
        elif current:
            end = datetime.fromisoformat(current[-1]["time"]) + timedelta(minutes=5)
            episodes.append({"start": current[0]["time"], "end": end.isoformat(), "minutes": len(current)*5,
                             "peak": max(r[metric] for r in current), "event_id": current[0]["event_id"]})
            current = []
    return {"coverage": len(valid)/len(rows)*100, "valid_minutes": len(valid)*5,
            "unknown_minutes": (len(rows)-len(valid))*5, "out_minutes": len(out)*5,
            "out_pct": len(out)/len(valid)*100 if valid else None,
            "latest": rows[-1][metric], "max": max((r[metric] for r in valid), default=None),
            "episodes": sorted(episodes, key=lambda e: e["minutes"], reverse=True),
            "suspect_count": sum(r[f"{metric}_quality"] == "suspect" for r in rows)}


def cost(quantities, percentage):
    multiplier = Decimal(1) + Decimal(percentage)/100
    base = [quantities["heat"]*Decimal("0.0083"), quantities["peak"]*Decimal("0.08"), quantities["offpeak"]*Decimal("0.04")]
    scenario = [base[0], base[1]*multiplier, base[2]*multiplier]
    return {"baseline": float(sum(base)), "scenario": float(sum(scenario)), "delta": float(sum(scenario)-sum(base)),
            "delta_pct": float((sum(scenario)-sum(base))/sum(base)*100),
            "base_components": list(map(float, base)), "scenario_components": list(map(float, scenario))}


def climate_figure(item, metric):
    spec = METRICS[metric]
    fig = go.Figure()
    x = [r["time"] for r in item["rows"]]
    y = [r[metric] for r in item["rows"]]
    fig.add_hrect(y0=spec["low"], y1=spec["high"], fillcolor="#eaf1e6", opacity=.85, line_width=0, layer="below")
    for bound in ["low", "high"]:
        fig.add_hline(y=spec[bound], line_color="#a9baa2", line_dash="dot", line_width=1)
    fig.add_vrect(x0=f'{item["day"]}T08:40:00', x1=f'{item["day"]}T09:20:00', fillcolor="#dde1df", opacity=.75, line_width=0, layer="below")
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line={"color": GREEN, "width": 2.6}, connectgaps=False,
                             name=spec["label"], hovertemplate=f'%{{x|%H:%M}} · %{{y:.2f}} {spec["unit"]}<extra></extra>'))
    fig.add_trace(go.Scatter(x=x, y=[v if v is not None and (v < spec["low"] or v > spec["high"]) else None for v in y],
                             mode="markers", marker={"size": 4, "color": AMBER}, name="Ngoài dải demo", hoverinfo="skip"))
    fig.update_layout(template="none", height=290, margin={"l": 38, "r": 18, "t": 18, "b": 32},
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
                      font={"family": "Arial, sans-serif", "color": "#7c867c", "size": 11}, hovermode="x unified",
                      xaxis={"type": "date", "range": [x[0], f'{item["day"]}T23:59:59'], "tickformat": "%H:%M", "dtick": 14400000, "showgrid": False, "zeroline": False},
                      yaxis={"range": spec["range"], "gridcolor": "#edf0e9", "zeroline": False, "title": {"text": spec["unit"], "font": {"size": 10}}})
    return fig


def cost_figure(item, percentage=20):
    values = item["costs"][str(percentage)]
    fig = go.Figure()
    for i,(label,color) in enumerate(zip(["Nhiệt", "Điện cao điểm", "Điện thấp điểm"], ["#a7b8a2", "#286d53", "#d5e5ab"])):
        fig.add_trace(go.Bar(name=label, x=["Baseline", "Kịch bản"], y=[values["base_components"][i],values["scenario_components"][i]], marker_color=color,
                             hovertemplate=f'{label}: %{{y:.3f}} EUR/m²<extra></extra>'))
    fig.update_layout(template="none", barmode="stack", height=210, margin={"l": 40, "r": 15, "t": 12, "b": 45},
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", bargap=.65,
                      font={"family": "Arial, sans-serif", "color": "#7c867c", "size": 11},
                      legend={"orientation": "h", "y": -.2, "x": 0}, yaxis={"gridcolor": "#edf0e9", "title": "EUR/m²"})
    return fig


def csv_text(item, metric):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["scope", "publication_id", "compartment", "source_time_naive", "event_id", "metric", "value", "raw_value", "unit", "quality", "rule", "lower", "upper"])
    spec = METRICS[metric]
    for r in item["rows"]:
        writer.writerow([item["scope"],item["publication"],item["house"],r["time"],r["event_id"],metric,r[metric],r[f"{metric}_raw"],spec["unit"],r[f"{metric}_quality"],"fixed-demo-band-v1",spec["low"],spec["high"]])
    return output.getvalue()
