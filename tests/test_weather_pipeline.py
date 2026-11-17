"""Offline tests for the weather pipeline (no network, no cloud, no LLM).

Run from the project root:
    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import copy
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_agent import agent  # noqa: E402
from weather_agent.dashboard import render_dashboard  # noqa: E402
from weather_agent.weather_utils import (  # noqa: E402
    WEEKDAYS,
    build_daily_summaries,
    celsius_to_fahrenheit,
    generate_weekly_summary,
    whole,
)

START = date(2026, 11, 17)  # a Tuesday


def make_raw_weather(days=7, temp_fn=None, precip=0, codes=None):
    """Build an Open-Meteo-shaped payload with hourly data in Celsius."""
    times, temps, rain, humidity = [], [], [], []
    for d in range(days):
        for h in range(24):
            t = datetime(START.year, START.month, START.day) + timedelta(days=d, hours=h)
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            temps.append(temp_fn(d, h) if temp_fn else 10.0 + d + (h % 12) * 0.5)
            rain.append(precip)
            humidity.append(60 + h % 10)
    daily_times = [(START + timedelta(days=d)).isoformat() for d in range(days)]
    return {
        "timezone": "America/Indiana/Indianapolis",
        "utc_offset_seconds": -18000,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "precipitation_probability": rain,
            "relative_humidity_2m": humidity,
        },
        "daily": {"time": daily_times, "weather_code": codes or [0] * days},
    }


class FakeToolContext:
    def __init__(self, state=None):
        self.state = state if state is not None else {}


class ConversionTests(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(0), 32)
        self.assertAlmostEqual(celsius_to_fahrenheit(100), 212)
        self.assertAlmostEqual(celsius_to_fahrenheit(-40), -40)
        self.assertAlmostEqual(celsius_to_fahrenheit(37), 98.6)

    def test_whole_rounds_half_up(self):
        self.assertEqual(whole(40.5), 41)
        self.assertEqual(whole(41.5), 42)
        self.assertEqual(whole(-0.4), 0)


class ProcessingTests(unittest.TestCase):
    def test_seven_days_with_correct_weekdays(self):
        records, dropped = build_daily_summaries(make_raw_weather())
        self.assertEqual(len(records), 7)
        self.assertEqual(dropped, 0)
        for rec in records:
            self.assertEqual(rec["weekday"], WEEKDAYS[date.fromisoformat(rec["date"]).weekday()])
        self.assertEqual(records[0]["weekday"], "Tuesday")

    def test_fahrenheit_matches_formula_and_is_converted_once(self):
        raw = make_raw_weather()
        records, _ = build_daily_summaries(raw)
        hourly = raw["hourly"]
        for rec in records:
            day_temps = [
                t for ts, t in zip(hourly["time"], hourly["temperature_2m"])
                if ts.startswith(rec["date"])
            ]
            self.assertEqual(rec["maximum_temperature_f"], round(max(day_temps) * 9 / 5 + 32, 1))
            self.assertEqual(rec["minimum_temperature_f"], round(min(day_temps) * 9 / 5 + 32, 1))
            avg_c = sum(day_temps) / len(day_temps)
            self.assertAlmostEqual(rec["average_temperature_f"], avg_c * 9 / 5 + 32, delta=0.0501)

    def test_raw_data_is_not_modified(self):
        raw = make_raw_weather()
        snapshot = copy.deepcopy(raw)
        build_daily_summaries(raw)
        self.assertEqual(raw, snapshot)

    def test_invalid_and_missing_readings_are_dropped(self):
        raw = make_raw_weather()
        raw["hourly"]["temperature_2m"][0] = None
        raw["hourly"]["temperature_2m"][1] = 999.0
        raw["hourly"]["precipitation_probability"][2] = None
        records, dropped = build_daily_summaries(raw)
        self.assertEqual(dropped, 2)
        self.assertEqual(records[0]["hourly_readings"], 22)
        self.assertEqual(len(records), 7)

    def test_weather_codes_mapped(self):
        records, _ = build_daily_summaries(make_raw_weather(codes=[0, 2, 3, 61, 73, 95, 999]))
        self.assertEqual(
            [r["condition"] for r in records],
            ["Sunny", "Partly cloudy", "Cloudy", "Light rain", "Snow", "Thunderstorms", None],
        )
        self.assertIsNone(records[-1]["weather_code"])

    def test_malformed_payload_raises(self):
        from weather_agent.weather_utils import WeatherDataError
        with self.assertRaises(WeatherDataError):
            build_daily_summaries({"hourly": {"time": []}})
        raw = make_raw_weather()
        raw["hourly"]["temperature_2m"].pop()
        with self.assertRaises(WeatherDataError):
            build_daily_summaries(raw)


class SummaryTests(unittest.TestCase):
    def test_no_invented_rain(self):
        records, _ = build_daily_summaries(make_raw_weather(precip=0))
        text = " ".join(generate_weekly_summary(records))
        self.assertIn("Rain chances remain low all week.", text)

    def test_rain_named_when_likely(self):
        records, _ = build_daily_summaries(make_raw_weather(precip=80))
        text = " ".join(generate_weekly_summary(records))
        self.assertIn("Rain is most likely", text)
        self.assertIn("80%", text)

    def test_warming_trend(self):
        records, _ = build_daily_summaries(make_raw_weather(temp_fn=lambda d, h: 5 + d * 3))
        text = generate_weekly_summary(records)[0]
        self.assertIn("trend warmer", text)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        records, _ = build_daily_summaries(make_raw_weather(codes=[0, 1, 2, 3, 61, 71, 95]))
        self.records = records
        self.summary = {
            "location": "Bloomington, Indiana",
            "processed_at": "2026-11-17T10:30:00-05:00",
            "timezone_abbreviation": "EST",
            "total_days": 7,
            "daily_forecast": records,
            "weekly_summary": generate_weekly_summary(records),
        }
        self.html = render_dashboard(self.summary)

    def test_uses_weekday_names_not_relative_labels(self):
        for rec in self.records:
            self.assertIn(f'<span class="day__name">{rec["weekday"]}</span>', self.html)
        for banned in ("Day 1", "Today", "Tomorrow"):
            self.assertNotIn(banned, self.html)

    def test_fahrenheit_only(self):
        self.assertIn("°F", self.html)
        self.assertNotIn("°C", self.html)

    def test_chart_matches_cards(self):
        chart = json.loads(re.search(
            r'<script id="chart-data" type="application/json">(.*?)</script>', self.html
        ).group(1))
        self.assertEqual(chart["labels"], [r["weekday"] for r in self.records])
        self.assertEqual(chart["average"], [whole(r["average_temperature_f"]) for r in self.records])
        self.assertEqual(chart["high"], [whole(r["maximum_temperature_f"]) for r in self.records])
        for rec in self.records:
            self.assertIn(f'High</span>{whole(rec["maximum_temperature_f"])}°', self.html)
            self.assertIn(f'<td>{whole(rec["average_temperature_f"])}°F</td>', self.html)

    def test_header_content(self):
        for text in ("Bloomington Weather", "7-day forecast and weather insights",
                     "Bloomington, Indiana", "Tuesday, Nov 17, 2026 at 10:30 AM EST",
                     "This Week at a Glance", "Average Daily Temperature", "Temperature (°F)"):
            self.assertIn(text, self.html)

    def test_missing_data_rejected(self):
        with self.assertRaises(ValueError):
            render_dashboard({"daily_forecast": []})


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(agent, "DATA_DIR", tmp),
            mock.patch.object(agent, "OUTPUT_DIR", tmp),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_process_then_dashboard(self):
        ctx = FakeToolContext({"temp:raw_weather": make_raw_weather()})
        result = agent.process_weather(ctx)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["days_processed"], 7)
        saved = json.loads((agent.DATA_DIR / "processed_weather.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["units"]["temperature"], "°F")
        self.assertEqual(saved, ctx.state["temp:processed_weather"])

        result = agent.create_dashboard(ctx)
        self.assertEqual(result["status"], "success")
        self.assertTrue((agent.OUTPUT_DIR / "index.html").is_file())

    def test_steps_refuse_to_run_without_upstream_data(self):
        ctx = FakeToolContext()
        self.assertEqual(agent.process_weather(ctx)["status"], "error")
        self.assertEqual(agent.upload_to_cloud(ctx)["status"], "error")
        self.assertEqual(agent.create_dashboard(ctx)["status"], "error")

    def test_fetch_reports_network_failure(self):
        import requests
        with mock.patch.object(agent.requests.Session, "get", side_effect=requests.ConnectionError("offline")):
            result = agent.fetch_weather(FakeToolContext())
        self.assertEqual(result["status"], "error")
        self.assertIn("Could not reach Open-Meteo", result["message"])

    def test_missing_bucket_reported(self):
        ctx = FakeToolContext({"temp:processed_weather": {"total_days": 7}})
        with mock.patch.dict(agent.os.environ, {"GCS_BUCKET_NAME": ""}):
            result = agent.upload_to_cloud(ctx)
        self.assertEqual(result["status"], "error")
        self.assertIn("GCS_BUCKET_NAME", result["message"])


if __name__ == "__main__":
    unittest.main()
