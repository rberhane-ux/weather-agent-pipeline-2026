"""HTML dashboard rendering for the Bloomington weather pipeline.

Takes the processed summary (temperatures already in °F) and returns a
self-contained HTML page. No unit conversion happens here: every number shown
is read from processed_weather.json and rounded once for display, so the
forecast cards, data table and chart always show identical values.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from html import escape

from .weather_utils import (
    WEEKDAYS,
    describe_weather_code,
    generate_weekly_summary,
    short_date,
    weekday_name,
    weekly_highlights,
    whole,
)

CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
LIKELY_RAIN_PCT = 50


# --------------------------------------------------
# Icons (inline SVG, no external assets)
# --------------------------------------------------

def _sun(cx: float, cy: float, r: float, ray_in: float, ray_out: float) -> str:
    rays = []
    for i in range(8):
        a = math.radians(i * 45)
        rays.append(
            f'<line x1="{cx + ray_in * math.cos(a):.1f}" y1="{cy + ray_in * math.sin(a):.1f}" '
            f'x2="{cx + ray_out * math.cos(a):.1f}" y2="{cy + ray_out * math.sin(a):.1f}"/>'
        )
    return (
        f'<g stroke="#f59e0b" stroke-width="3" stroke-linecap="round">{"".join(rays)}</g>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#fbbf24"/>'
    )


def _cloud(dx: float = 0, dy: float = 0, fill: str = "#cbd5e1") -> str:
    return (
        f'<g fill="{fill}" transform="translate({dx} {dy})">'
        '<circle cx="24" cy="36" r="10"/><circle cx="36" cy="30" r="13"/>'
        '<circle cx="46" cy="38" r="8"/><rect x="24" y="34" width="22" height="12"/></g>'
    )


def _flake(cx: float, cy: float, r: float = 4) -> str:
    lines = []
    for deg in (90, 30, 150):
        a = math.radians(deg)
        lines.append(
            f'<line x1="{cx - r * math.cos(a):.1f}" y1="{cy - r * math.sin(a):.1f}" '
            f'x2="{cx + r * math.cos(a):.1f}" y2="{cy + r * math.sin(a):.1f}"/>'
        )
    return f'<g stroke="#60a5fa" stroke-width="2.2" stroke-linecap="round">{"".join(lines)}</g>'


def _drops(*segments: tuple[float, float]) -> str:
    lines = "".join(
        f'<line x1="{x}" y1="{y}" x2="{x - 3}" y2="{y + 9}"/>' for x, y in segments
    )
    return f'<g stroke="#2563eb" stroke-width="3" stroke-linecap="round">{lines}</g>'


_WEATHER_ICONS = {
    "clear": _sun(32, 32, 10, 15, 21),
    "partly": _sun(23, 23, 8, 12, 17) + _cloud(4, 6, "#d5dde8"),
    "cloudy": _cloud(-6, -8, "#b6c2d1") + _cloud(4, 2, "#d5dde8"),
    "fog": _cloud(0, -8, "#cbd5e1")
    + '<g stroke="#94a3b8" stroke-width="3" stroke-linecap="round">'
      '<line x1="16" y1="46" x2="48" y2="46"/><line x1="22" y1="53" x2="54" y2="53"/></g>',
    "drizzle": _cloud(0, -8, "#b6c2d1")
    + '<g fill="#3b82f6"><circle cx="24" cy="47" r="2"/><circle cx="34" cy="50" r="2"/>'
      '<circle cx="44" cy="47" r="2"/><circle cx="29" cy="56" r="2"/><circle cx="39" cy="56" r="2"/></g>',
    "rain": _cloud(0, -8, "#b6c2d1") + _drops((26, 44), (36, 44), (46, 44)),
    "sleet": _cloud(0, -8, "#b6c2d1") + _drops((26, 44), (44, 44)) + _flake(34, 52),
    "snow": _cloud(0, -8, "#b6c2d1") + _flake(24, 49) + _flake(42, 49) + _flake(33, 57),
    "thunder": _cloud(0, -8, "#94a3b8")
    + '<polygon points="35,40 26,53 32,53 28,62 41,47 35,47 39,40" fill="#f59e0b"/>',
}


def weather_icon(icon_key: str | None) -> str:
    """Return an inline SVG for a weather icon key, or '' if there is none."""
    body = _WEATHER_ICONS.get(icon_key or "")
    if not body:
        return ""
    return f'<svg class="wx" viewBox="0 0 64 64" aria-hidden="true" focusable="false">{body}</svg>'


ICON_PIN = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" '
    'd="M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 '
    '2.5 2.5 0 0 1 0 5z"/></svg>'
)
ICON_UMBRELLA = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" '
    'd="M12 3a9 9 0 0 0-9 9h18a9 9 0 0 0-9-9z"/><path d="M12 12v6.5a2 2 0 0 0 4 0" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
)
ICON_DROPLET = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linejoin="round" '
    'd="M12 3.5c-3 4-6 7.3-6 10.5a6 6 0 0 0 12 0c0-3.2-3-6.5-6-10.5z"/></svg>'
)


# --------------------------------------------------
# View model
# --------------------------------------------------

def _format_updated(processed_at: str | None, tz_label: str | None) -> tuple[str, str]:
    """Return (human-readable label, machine datetime) for the update time."""
    if not processed_at:
        return "Unknown", ""
    try:
        dt = datetime.fromisoformat(processed_at)
    except ValueError:
        return escape(processed_at), ""
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    label = (
        f"{WEEKDAYS[dt.weekday()]}, {short_date(dt.date())}, {dt.year} "
        f"at {hour}:{dt.minute:02d} {meridiem}"
    )
    if tz_label:
        label += f" {tz_label}"
    return label, dt.isoformat()


def _build_days(records: list[dict]) -> list[dict]:
    days = []
    for rec in records:
        day = date.fromisoformat(rec["date"])
        condition, icon_key = describe_weather_code(rec.get("weather_code"))
        rain = rec.get("maximum_precipitation_probability")
        humidity = rec.get("average_humidity")
        days.append({
            "iso": rec["date"],
            # Derived from the actual date, never from list position.
            "weekday": weekday_name(day),
            "short_date": short_date(day),
            "high": whole(rec["maximum_temperature_f"]),
            "low": whole(rec["minimum_temperature_f"]),
            "average": whole(rec["average_temperature_f"]),
            "rain": int(rain) if rain is not None else None,
            "humidity": whole(humidity) if humidity is not None else None,
            "condition": condition,
            "icon": weather_icon(icon_key),
        })
    return days


# --------------------------------------------------
# HTML fragments
# --------------------------------------------------

def _day_card(d: dict, week_low: int, week_high: int) -> str:
    span = max(week_high - week_low, 1)
    left = (d["low"] - week_low) / span * 100
    width = max((d["high"] - d["low"]) / span * 100, 4)

    rain_html = ""
    if d["rain"] is not None:
        likely = " meta--likely" if d["rain"] >= LIKELY_RAIN_PCT else ""
        rain_html = (
            f'<span class="meta meta--rain{likely}" title="Chance of rain">{ICON_UMBRELLA}'
            f'{d["rain"]}%<span class="sr-only"> chance of rain</span></span>'
        )
    humidity_html = ""
    if d["humidity"] is not None:
        humidity_html = (
            f'<span class="meta meta--humidity" title="Humidity">{ICON_DROPLET}'
            f'{d["humidity"]}%<span class="sr-only"> humidity</span></span>'
        )

    icon = d["icon"] or '<span class="wx wx--none" aria-hidden="true"></span>'
    condition = escape(d["condition"]) if d["condition"] else "&nbsp;"

    return f"""
        <li class="day">
          <div class="day__when">
            <span class="day__name">{escape(d["weekday"])}</span>
            <time class="day__date" datetime="{d["iso"]}">{escape(d["short_date"])}</time>
          </div>
          <div class="day__icon">{icon}</div>
          <div class="day__cond">{condition}</div>
          <div class="day__temps">
            <div class="temps">
              <span class="temp temp--hi"><span class="temp__label">High</span>{d["high"]}°<span class="sr-only">F</span></span>
              <span class="temp temp--lo"><span class="temp__label">Low</span>{d["low"]}°<span class="sr-only">F</span></span>
            </div>
            <div class="range" aria-hidden="true"><span style="left:{left:.1f}%;width:{width:.1f}%"></span></div>
          </div>
          <div class="day__meta">{rain_html}{humidity_html}</div>
        </li>"""


def _table_rows(days: list[dict]) -> str:
    def pct(v: int | None) -> str:
        return f"{v}%" if v is not None else "—"

    return "".join(
        f"""
              <tr>
                <th scope="row">{escape(d["weekday"])}</th>
                <td>{escape(d["short_date"])}</td>
                <td>{escape(d["condition"] or "—")}</td>
                <td>{d["high"]}°F</td>
                <td>{d["low"]}°F</td>
                <td>{d["average"]}°F</td>
                <td>{pct(d["rain"])}</td>
                <td>{pct(d["humidity"])}</td>
              </tr>"""
        for d in days
    )


STYLES = """
    :root {
      --bg: #f3f6fb;
      --surface: #ffffff;
      --surface-alt: #f7f9fc;
      --text: #14213a;
      --muted: #536179;
      --border: #e2e8f0;
      --accent: #1d4ed8;
      --rain: #1667c4;
      --radius: 18px;
      --shadow: 0 1px 2px rgba(15, 23, 42, .04), 0 8px 24px rgba(15, 23, 42, .06);
      --font: "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; }
    html { -webkit-text-size-adjust: 100%; }
    body {
      margin: 0;
      font-family: var(--font);
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      font-variant-numeric: tabular-nums;
    }
    .sr-only {
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
    }
    .page { max-width: 1160px; margin: 0 auto; padding: 32px 24px 48px; }
    main { display: grid; gap: 24px; margin-top: 24px; }

    /* Header */
    .hero {
      position: relative;
      overflow: hidden;
      display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
      gap: 16px 32px;
      padding: 32px 36px;
      border-radius: var(--radius);
      color: #fff;
      background:
        radial-gradient(circle at 92% -20%, rgba(255, 255, 255, .18), transparent 45%),
        linear-gradient(135deg, #1e3a8a 0%, #1d4ed8 100%);
      box-shadow: var(--shadow);
    }
    .location {
      display: inline-flex; align-items: center; gap: 6px;
      margin: 0 0 10px; font-size: .9rem; font-weight: 600; letter-spacing: .02em;
      color: #dbe7ff;
    }
    .location svg { width: 16px; height: 16px; }
    .hero h1 { margin: 0; font-size: clamp(2rem, 4.5vw, 2.75rem); line-height: 1.1; letter-spacing: -.02em; }
    .subtitle { margin: 8px 0 0; font-size: 1.1rem; color: #dbe7ff; }
    .updated {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 14px; border-radius: 999px;
      background: rgba(255, 255, 255, .12); border: 1px solid rgba(255, 255, 255, .2);
      font-size: .875rem; color: #fff;
    }
    .updated::before {
      content: ""; width: 8px; height: 8px; border-radius: 50%; background: #4ade80;
      box-shadow: 0 0 0 3px rgba(74, 222, 128, .25);
    }

    /* Cards */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 28px;
    }
    .section-head {
      display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
      gap: 8px 24px; margin-bottom: 20px;
    }
    .section-head h2 { margin: 0; font-size: 1.3rem; letter-spacing: -.01em; }
    .section-note { margin: 4px 0 0; color: var(--muted); font-size: .9rem; }
    .legend { display: flex; gap: 16px; margin: 0; padding: 0; list-style: none; color: var(--muted); font-size: .85rem; }
    .legend li { display: inline-flex; align-items: center; gap: 6px; }
    .legend svg { width: 16px; height: 16px; }
    .legend .rain-key { color: var(--rain); }

    /* Seven-day forecast */
    .days {
      list-style: none; margin: 0; padding: 0;
      display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 12px;
    }
    .day {
      display: flex; flex-direction: column; align-items: center; gap: 10px;
      padding: 18px 10px 16px;
      text-align: center;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 14px;
    }
    .day__when { display: flex; flex-direction: column; }
    .day__name { font-weight: 700; font-size: 1.02rem; }
    .day__date { color: var(--muted); font-size: .85rem; }
    .wx { display: block; width: 56px; height: 56px; }
    .day__cond { color: var(--muted); font-size: .85rem; min-height: 2.5em; line-height: 1.25; }
    .day__temps { width: 100%; }
    .temps { display: flex; justify-content: center; gap: 14px; }
    .temp { display: flex; flex-direction: column; font-size: 1.45rem; font-weight: 700; line-height: 1.15; }
    .temp--lo { color: var(--muted); font-weight: 600; }
    .temp__label { font-size: .68rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
    .range { position: relative; height: 6px; margin: 10px 6px 0; border-radius: 999px; background: #e3e9f2; }
    .range span {
      position: absolute; top: 0; bottom: 0; border-radius: 999px;
      background: linear-gradient(90deg, #60a5fa, #f59e0b);
    }
    .day__meta {
      display: flex; flex-wrap: wrap; justify-content: center; gap: 4px 12px;
      font-size: .85rem; color: var(--muted);
    }
    .meta { display: inline-flex; align-items: center; gap: 4px; }
    .meta svg { width: 15px; height: 15px; }
    .meta--rain { color: var(--rain); }
    .meta--likely { font-weight: 700; }

    /* Week at a glance */
    .glance { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 28px; align-items: start; }
    .insights { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
    .insights li { position: relative; padding-left: 22px; font-size: 1.02rem; }
    .insights li::before {
      content: ""; position: absolute; left: 2px; top: .55em;
      width: 9px; height: 9px; border-radius: 50%; background: var(--accent); opacity: .85;
    }
    .stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 0; }
    .stat { padding: 16px; border-radius: 14px; background: var(--surface-alt); border: 1px solid var(--border); }
    .stat dt { font-size: .8rem; font-weight: 600; color: var(--muted); }
    .stat dd { margin: 0; }
    .stat__value { display: block; font-size: 1.6rem; font-weight: 700; line-height: 1.25; }
    .stat__detail { display: block; font-size: .85rem; color: var(--muted); }

    /* Chart */
    .chart-box { position: relative; height: 300px; }
    .chart-fallback { color: var(--muted); }
    details { margin-top: 20px; border-top: 1px solid var(--border); padding-top: 16px; }
    summary { cursor: pointer; font-weight: 600; color: var(--accent); width: fit-content; }
    summary:focus-visible { outline: 3px solid #93c5fd; outline-offset: 3px; border-radius: 4px; }
    .table-wrap { overflow-x: auto; margin-top: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; min-width: 620px; }
    th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
    th:first-child, td:nth-child(2), td:nth-child(3) { text-align: left; }
    thead th { font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }

    footer { margin-top: 28px; text-align: center; color: var(--muted); font-size: .85rem; }
    footer p { margin: 4px 0; }
    footer a { color: var(--accent); }

    /* Tablet & mobile: forecast becomes an easy-to-scan list */
    @media (max-width: 960px) {
      .days { grid-template-columns: 1fr; gap: 0; }
      .day {
        display: grid;
        grid-template-columns: 48px minmax(0, 1fr) auto;
        grid-template-areas: "icon when temps" "icon cond meta";
        align-items: center; gap: 2px 16px;
        padding: 14px 4px; text-align: left;
        background: none; border: 0; border-bottom: 1px solid var(--border); border-radius: 0;
      }
      .day:last-child { border-bottom: 0; }
      .day__icon { grid-area: icon; }
      .day__when { grid-area: when; flex-direction: row; align-items: baseline; gap: 8px; }
      .day__cond { grid-area: cond; min-height: 0; }
      .day__temps { grid-area: temps; width: auto; justify-self: end; }
      .day__meta { grid-area: meta; justify-self: end; }
      .wx { width: 48px; height: 48px; }
      .temps { justify-content: flex-end; gap: 12px; }
      .temp { flex-direction: row; font-size: 1.25rem; }
      .temp__label { display: none; }
      .range { width: 120px; margin: 6px 0 0 auto; }
      .glance { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .page { padding: 16px 14px 36px; }
      main { gap: 16px; margin-top: 16px; }
      .hero { padding: 24px 22px; }
      .card { padding: 20px 18px; }
      .day { grid-template-columns: 40px minmax(0, 1fr) auto; gap: 2px 12px; }
      .wx { width: 40px; height: 40px; }
      .day__when { flex-direction: column; gap: 0; }
      .range { width: 88px; }
      .stats { gap: 10px; }
      .stat { padding: 14px; }
      .stat__value { font-size: 1.35rem; }
      .chart-box { height: 240px; }
    }
"""


CHART_SCRIPT = """
    (function () {
      var data = JSON.parse(document.getElementById('chart-data').textContent);
      var canvas = document.getElementById('temperatureChart');
      if (typeof Chart === 'undefined') {
        canvas.parentNode.innerHTML = '<p class="chart-fallback">The chart could not be loaded. ' +
          'The same values are available in the daily data table below.</p>';
        return;
      }
      var narrow = function () { return window.innerWidth < 560; };
      var ctx = canvas.getContext('2d');
      var fill = ctx.createLinearGradient(0, 0, 0, 300);
      fill.addColorStop(0, 'rgba(29, 78, 216, 0.18)');
      fill.addColorStop(1, 'rgba(29, 78, 216, 0)');
      var min = Math.min.apply(null, data.average), max = Math.max.apply(null, data.average);

      Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
      Chart.defaults.color = '#536179';

      new Chart(canvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [{
            label: 'Average temperature',
            data: data.average,
            borderColor: '#1d4ed8',
            backgroundColor: fill,
            fill: true,
            borderWidth: 2.5,
            cubicInterpolationMode: 'monotone',
            pointRadius: 4,
            pointHoverRadius: 6,
            pointBackgroundColor: '#ffffff',
            pointBorderColor: '#1d4ed8',
            pointBorderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#14213a',
              padding: 12,
              displayColors: false,
              callbacks: {
                title: function (items) { var i = items[0].dataIndex; return data.labels[i] + ', ' + data.dates[i]; },
                label: function (item) { return 'Average: ' + item.parsed.y + '°F'; },
                afterLabel: function (item) {
                  var i = item.dataIndex; return 'High ' + data.high[i] + '°F · Low ' + data.low[i] + '°F';
                }
              }
            }
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: {
                font: { size: 13, weight: '600' },
                callback: function (value) {
                  var label = this.getLabelForValue(value);
                  return narrow() ? label.slice(0, 3) : label;
                }
              }
            },
            y: {
              suggestedMin: Math.floor(min - 5),
              suggestedMax: Math.ceil(max + 5),
              grid: { color: '#edf1f7' },
              border: { display: false },
              ticks: { callback: function (v) { return v + '°'; }, maxTicksLimit: 6 },
              title: { display: true, text: 'Temperature (°F)', font: { size: 13, weight: '600' } }
            }
          }
        }
      });
    })();
"""


def render_dashboard(summary: dict) -> str:
    """Render the full dashboard HTML from a processed weather summary."""
    records = summary.get("daily_forecast")
    if not records:
        raise ValueError("Processed summary has no daily forecast records.")

    required = ("date", "maximum_temperature_f", "minimum_temperature_f", "average_temperature_f")
    for rec in records:
        missing = [k for k in required if rec.get(k) is None]
        if missing:
            raise ValueError(f"Daily record is missing fields: {', '.join(missing)}")

    days = _build_days(records)
    week_low = min(d["low"] for d in days)
    week_high = max(d["high"] for d in days)

    location = escape(summary.get("location", "Bloomington, Indiana"))
    updated_label, updated_iso = _format_updated(
        summary.get("processed_at"), summary.get("timezone_abbreviation")
    )

    sentences = summary.get("weekly_summary") or generate_weekly_summary(records)
    insights_html = "".join(f"\n            <li>{escape(s)}</li>" for s in sentences)
    stats_html = "".join(
        f"""
            <div class="stat">
              <dt>{escape(h["label"])}</dt>
              <dd><span class="stat__value">{escape(h["value"])}</span>
                  <span class="stat__detail">{escape(h["detail"])}</span></dd>
            </div>"""
        for h in weekly_highlights(records)
    )

    cards_html = "".join(_day_card(d, week_low, week_high) for d in days)

    # Chart uses the exact same rounded numbers as the cards and table.
    chart_data = json.dumps({
        "labels": [d["weekday"] for d in days],
        "dates": [d["short_date"] for d in days],
        "average": [d["average"] for d in days],
        "high": [d["high"] for d in days],
        "low": [d["low"] for d in days],
    }).replace("</", "<\\/")

    avg_text = ", ".join(f'{d["weekday"]} {d["average"]}°F' for d in days)
    first, last = days[0], days[-1]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="7-day forecast and weather insights for {location}.">
  <meta name="theme-color" content="#1e3a8a">
  <title>Bloomington Weather · 7-Day Forecast</title>
  <style>{STYLES}  </style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <div>
        <p class="location">{ICON_PIN}{location}</p>
        <h1>Bloomington Weather</h1>
        <p class="subtitle">7-day forecast and weather insights</p>
      </div>
      <p class="updated"><span>Updated <time datetime="{escape(updated_iso)}">{escape(updated_label)}</time></span></p>
    </header>

    <main>
      <section class="card" aria-labelledby="forecast-title">
        <div class="section-head">
          <div>
            <h2 id="forecast-title">7-Day Forecast</h2>
            <p class="section-note">{escape(first["weekday"])}, {escape(first["short_date"])} – {escape(last["weekday"])}, {escape(last["short_date"])} · Daily high and low in °F</p>
          </div>
          <ul class="legend" aria-label="Legend">
            <li class="rain-key">{ICON_UMBRELLA}Chance of rain</li>
            <li>{ICON_DROPLET}Humidity</li>
          </ul>
        </div>
        <ol class="days">{cards_html}
        </ol>
      </section>

      <section class="card" aria-labelledby="glance-title">
        <div class="section-head">
          <h2 id="glance-title">This Week at a Glance</h2>
        </div>
        <div class="glance">
          <ul class="insights">{insights_html}
          </ul>
          <dl class="stats">{stats_html}
          </dl>
        </div>
      </section>

      <section class="card" aria-labelledby="chart-title">
        <div class="section-head">
          <div>
            <h2 id="chart-title">Average Daily Temperature</h2>
            <p class="section-note">Average of all hourly readings for each day, in °F</p>
          </div>
        </div>
        <div class="chart-box">
          <canvas id="temperatureChart" role="img"
                  aria-label="Line chart of average daily temperature: {escape(avg_text)}"></canvas>
        </div>
        <details>
          <summary>Show daily data table</summary>
          <div class="table-wrap">
            <table>
              <caption class="sr-only">Daily forecast details for {location}</caption>
              <thead>
                <tr>
                  <th scope="col">Day</th><th scope="col">Date</th><th scope="col">Conditions</th>
                  <th scope="col">High</th><th scope="col">Low</th><th scope="col">Average</th>
                  <th scope="col">Chance of rain</th><th scope="col">Humidity</th>
                </tr>
              </thead>
              <tbody>{_table_rows(days)}
              </tbody>
            </table>
          </div>
        </details>
      </section>
    </main>

    <footer>
      <p>Forecast data from <a href="https://open-meteo.com/">Open-Meteo</a>. Chance of rain is the highest hourly probability for each day; humidity is the daily average.</p>
      <p>Times shown in local Bloomington time.</p>
    </footer>
  </div>

  <script id="chart-data" type="application/json">{chart_data}</script>
  <script src="{CHART_JS_URL}"></script>
  <script>{CHART_SCRIPT}  </script>
</body>
</html>
"""
