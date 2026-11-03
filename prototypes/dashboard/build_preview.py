"""Build one offline HTML file from the same fixtures/Plotly figures as Streamlit."""
from pathlib import Path
import json
from plotly.offline import get_plotlyjs
from data import DAYS, HOUSES, METRICS, fixture, climate_figure, cost_figure, csv_text

HERE = Path(__file__).parent


def build():
    records = {}
    for house in HOUSES:
        for day in DAYS:
            item = fixture(house, day)
            item["figures"] = {m: json.loads(climate_figure(item,m).to_json()) for m in METRICS}
            item["cost_figure"] = json.loads(cost_figure(item).to_json())
            item["csv"] = {m: csv_text(item,m) for m in METRICS}
            records[f"{house}|{day}"] = item
    payload = json.dumps({"records": records, "metrics": METRICS}, ensure_ascii=False).replace("</", "<\\/")
    html = (HERE / "template.html").read_text()
    html = html.replace("/*__PLOTLY__*/",get_plotlyjs()).replace("/*__DATA__*/", payload)
    target = HERE / "preview.html"
    target.write_text(html)
    print(f"Built {target} ({target.stat().st_size / 1024 / 1024:.1f} MiB), fully offline")


if __name__ == "__main__":
    build()
