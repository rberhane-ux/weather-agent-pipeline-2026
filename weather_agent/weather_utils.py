"""Weather data helpers for the Bloomington weather pipeline.

Validation, unit conversion, weather-condition lookup, daily aggregation and
plain-English insights. Everything here is a pure function with no file or
network I/O, so it can be unit-tested without the ADK runtime.
"""

from __future__ import annotations

import math
from datetime import date
from statistics import mean
from typing import Any

import pandas as pd

WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)
MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

REQUIRED_HOURLY_FIELDS = (
    "time",
    "temperature_2m",
    "precipitation_probability",
    "relative_humidity_2m",
)

# Physically plausible near-surface air temperatures (°C). Readings outside
# this range are treated as corrupt and excluded from the daily summaries.
MIN_VALID_TEMP_C = -90.0
MAX_VALID_TEMP_C = 60.0

# Thresholds used by the plain-English weekly summary.
NOTABLE_TEMP_CHANGE_F = 5     # change in highs worth calling a "trend"
NOTABLE_WEEKEND_DIFF_F = 4    # weekend vs. weekday difference worth noting
LIKELY_RAIN_PCT = 50
POSSIBLE_RAIN_PCT = 25
FREEZING_F = 32
WET_ICONS = {"drizzle", "rain", "sleet", "snow", "thunder"}


class WeatherDataError(ValueError):
    """Raised when weather data is missing, malformed or unusable."""


# WMO weather interpretation codes, as documented by Open-Meteo
# (https://open-meteo.com/en/docs). Each maps to (label, icon key).
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Sunny", "clear"),
    1: ("Mostly sunny", "partly"),
    2: ("Partly cloudy", "partly"),
    3: ("Cloudy", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "sleet"),
    57: ("Freezing drizzle", "sleet"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "sleet"),
    67: ("Freezing rain", "sleet"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Light snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorms", "thunder"),
    96: ("Thunderstorms with hail", "thunder"),
    99: ("Thunderstorms with hail", "thunder"),
}


# --------------------------------------------------
# Small helpers
# --------------------------------------------------

def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert °C to °F using F = (C × 9/5) + 32."""
    return celsius * 9 / 5 + 32


def whole(value: float) -> int:
    """Round half up to a whole number (avoids Python's banker's rounding)."""
    return int(math.floor(value + 0.5))


def weekday_name(day: date) -> str:
    return WEEKDAYS[day.weekday()]


def short_date(day: date) -> str:
    """Format a date like 'Nov 17'."""
    return f"{MONTHS[day.month - 1]} {day.day}"


def describe_weather_code(code: Any) -> tuple[str | None, str | None]:
    """Return (label, icon key) for a WMO code, or (None, None) if unknown."""
    if code is None:
        return None, None
    try:
        if isinstance(code, float) and math.isnan(code):
            return None, None
        return WMO_CODES.get(int(code), (None, None))
    except (TypeError, ValueError):
        return None, None


def _round_or_none(value: Any, digits: int = 1) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# --------------------------------------------------
# Validation and aggregation
# --------------------------------------------------

def validate_raw_weather(weather: Any) -> None:
    """Check that an Open-Meteo response has the hourly fields we need."""
    if not isinstance(weather, dict):
        raise WeatherDataError("Weather response is not a JSON object.")

    if "error" in weather and weather.get("reason"):
        raise WeatherDataError(f"Open-Meteo returned an error: {weather['reason']}")

    hourly = weather.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherDataError("Weather response has no 'hourly' section.")

    missing = [f for f in REQUIRED_HOURLY_FIELDS if not isinstance(hourly.get(f), list)]
    if missing:
        raise WeatherDataError(f"Hourly data is missing fields: {', '.join(missing)}")

    lengths = {len(hourly[f]) for f in REQUIRED_HOURLY_FIELDS}
    if lengths == {0}:
        raise WeatherDataError("Hourly data is empty.")
    if len(lengths) != 1:
        raise WeatherDataError("Hourly data fields have mismatched lengths.")


def _daily_weather_codes(weather: dict) -> dict[str, Any]:
    """Map 'YYYY-MM-DD' -> Open-Meteo's daily WMO weather code, if present."""
    daily = weather.get("daily") or {}
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    if len(times) != len(codes):
        return {}
    return dict(zip(times, codes))


def build_daily_summaries(weather: dict) -> tuple[list[dict], int]:
    """Aggregate hourly Celsius readings into daily summaries in Fahrenheit.

    Temperatures are aggregated in °C (the raw API unit) and converted to °F
    exactly once, here. Returns (daily records, number of hourly readings
    discarded as missing or invalid).
    """
    validate_raw_weather(weather)
    hourly = weather["hourly"]

    df = pd.DataFrame({
        "time": pd.to_datetime(pd.Series(hourly["time"]), errors="coerce"),
        "temperature_c": pd.to_numeric(pd.Series(hourly["temperature_2m"]), errors="coerce"),
        "precipitation_probability": pd.to_numeric(
            pd.Series(hourly["precipitation_probability"]), errors="coerce"
        ),
        "humidity": pd.to_numeric(pd.Series(hourly["relative_humidity_2m"]), errors="coerce"),
    })

    total = len(df)
    # A reading is only usable with a timestamp and a plausible temperature.
    # Missing rain/humidity values are skipped per-column by the aggregations.
    df = df.dropna(subset=["time", "temperature_c"])
    df = df[df["temperature_c"].between(MIN_VALID_TEMP_C, MAX_VALID_TEMP_C)]
    dropped = total - len(df)

    if df.empty:
        raise WeatherDataError("No valid hourly temperature readings were found.")

    daily = (
        df.groupby(df["time"].dt.date)
        .agg(
            hourly_readings=("temperature_c", "size"),
            average_c=("temperature_c", "mean"),
            maximum_c=("temperature_c", "max"),
            minimum_c=("temperature_c", "min"),
            average_precipitation=("precipitation_probability", "mean"),
            maximum_precipitation=("precipitation_probability", "max"),
            average_humidity=("humidity", "mean"),
        )
        .sort_index()
    )

    codes = _daily_weather_codes(weather)
    records = []

    for day, row in daily.iterrows():
        code = codes.get(day.isoformat())
        condition, _ = describe_weather_code(code)
        max_precip = _round_or_none(row["maximum_precipitation"], 0)

        records.append({
            "date": day.isoformat(),
            "weekday": weekday_name(day),
            "average_temperature_f": round(float(celsius_to_fahrenheit(row["average_c"])), 1),
            "maximum_temperature_f": round(float(celsius_to_fahrenheit(row["maximum_c"])), 1),
            "minimum_temperature_f": round(float(celsius_to_fahrenheit(row["minimum_c"])), 1),
            "average_precipitation_probability": _round_or_none(row["average_precipitation"]),
            "maximum_precipitation_probability": (
                int(max_precip) if max_precip is not None else None
            ),
            "average_humidity": _round_or_none(row["average_humidity"]),
            "weather_code": int(code) if condition else None,
            "condition": condition,
            "hourly_readings": int(row["hourly_readings"]),
        })

    return records, dropped


# --------------------------------------------------
# Plain-English insights
# --------------------------------------------------

def _temperature_trend(days: list[dict]) -> str:
    highs = [d["maximum_temperature_f"] for d in days]
    names = [d["weekday"] for d in days]
    n = len(highs)
    lo_high, hi_high = whole(min(highs)), whole(max(highs))

    if n >= 4:
        peak = highs.index(max(highs))
        trough = highs.index(min(highs))
        ends_high = max(highs[0], highs[-1])
        ends_low = min(highs[0], highs[-1])

        if 0 < peak < n - 1 and highs[peak] - ends_high >= NOTABLE_TEMP_CHANGE_F:
            return (
                f"Temperatures climb to a peak high of {whole(highs[peak])}°F "
                f"on {names[peak]}, then cool off to {whole(highs[-1])}°F by {names[-1]}."
            )
        if 0 < trough < n - 1 and ends_low - highs[trough] >= NOTABLE_TEMP_CHANGE_F:
            return (
                f"Temperatures dip to a high of just {whole(highs[trough])}°F "
                f"on {names[trough]} before warming back up."
            )

        k = max(1, n // 3)
        change = mean(highs[-k:]) - mean(highs[:k])
        if change >= NOTABLE_TEMP_CHANGE_F:
            return (
                f"Temperatures trend warmer as the week goes on, with highs "
                f"rising from {whole(highs[0])}°F on {names[0]} to "
                f"{whole(highs[-1])}°F by {names[-1]}."
            )
        if change <= -NOTABLE_TEMP_CHANGE_F:
            return (
                f"Temperatures trend cooler as the week goes on, with highs "
                f"falling from {whole(highs[0])}°F on {names[0]} to "
                f"{whole(highs[-1])}°F by {names[-1]}."
            )

    return f"Temperatures stay fairly steady, with highs between {lo_high}°F and {hi_high}°F."


def _weekend_outlook(days: list[dict]) -> str | None:
    weekend = [d["maximum_temperature_f"] for d in days if d["weekday"] in ("Saturday", "Sunday")]
    weekdays = [d["maximum_temperature_f"] for d in days if d["weekday"] not in ("Saturday", "Sunday")]
    if not weekend or not weekdays:
        return None

    weekend_high = whole(mean(weekend))
    diff = mean(weekend) - mean(weekdays)
    if diff >= NOTABLE_WEEKEND_DIFF_F:
        return f"The weekend looks warmer than the weekdays, with highs near {weekend_high}°F."
    if diff <= -NOTABLE_WEEKEND_DIFF_F:
        return f"The weekend is expected to be cooler, with highs near {weekend_high}°F."
    return f"The weekend looks similar to the rest of the week, with highs near {weekend_high}°F."


def _precipitation_outlook(days: list[dict]) -> str | None:
    with_rain = [d for d in days if d.get("maximum_precipitation_probability") is not None]
    if not with_rain:
        return None

    likely = [d for d in with_rain if d["maximum_precipitation_probability"] >= LIKELY_RAIN_PCT]
    possible = [
        d for d in with_rain
        if POSSIBLE_RAIN_PCT <= d["maximum_precipitation_probability"] < LIKELY_RAIN_PCT
    ]

    def kind(group: list[dict]) -> str:
        # Name snow only when Open-Meteo's own condition code says snow.
        snowy = [d for d in group if describe_weather_code(d.get("weather_code"))[1] == "snow"]
        if snowy and len(snowy) == len(group):
            return "Snow"
        return "Rain or snow" if snowy else "Rain"

    if likely:
        peak = max(d["maximum_precipitation_probability"] for d in likely)
        return (
            f"{kind(likely)} is most likely on {_join_names([d['weekday'] for d in likely])}, "
            f"with chances up to {peak}%."
        )
    if possible:
        peak = max(d["maximum_precipitation_probability"] for d in possible)
        return (
            f"Rain chances stay fairly low, with the best chance ({peak}%) on "
            f"{_join_names([d['weekday'] for d in possible])}."
        )
    # Chances are low, but Open-Meteo's condition code may still flag some
    # precipitation on a day; mention it so the summary matches the cards.
    wet = [
        d for d in days
        if describe_weather_code(d.get("weather_code"))[1] in WET_ICONS
    ]
    if wet:
        condition = describe_weather_code(wet[0]["weather_code"])[0].lower()
        return (
            f"Rain chances remain low for most of the week, though {condition} "
            f"is possible on {_join_names([d['weekday'] for d in wet])}."
        )
    return "Rain chances remain low all week."


def _overnight_outlook(days: list[dict]) -> str:
    coldest = min(days, key=lambda d: d["minimum_temperature_f"])
    coldest_low = whole(coldest["minimum_temperature_f"])
    freezing = [d["weekday"] for d in days if whole(d["minimum_temperature_f"]) <= FREEZING_F]

    if len(freezing) == len(days):
        return f"Expect freezing temperatures every night, bottoming out near {coldest_low}°F on {coldest['weekday']}."
    if freezing == [coldest["weekday"]]:
        return (
            f"{coldest['weekday']} is the only day with a freezing low, near {coldest_low}°F; "
            f"other lows stay above {FREEZING_F}°F."
        )
    if freezing:
        return (
            f"Expect freezing temperatures on {_join_names(freezing)}, "
            f"with the coldest low near {coldest_low}°F on {coldest['weekday']}."
        )
    return f"Lows stay above freezing, with the coldest reading near {coldest_low}°F on {coldest['weekday']}."


def generate_weekly_summary(days: list[dict]) -> list[str]:
    """Build short plain-English sentences describing the forecast.

    Every statement is derived directly from the daily records; nothing is
    added that the data does not support.
    """
    if not days:
        return []

    sentences = [
        _temperature_trend(days),
        _weekend_outlook(days),
        _precipitation_outlook(days),
        _overnight_outlook(days),
    ]
    return [s for s in sentences if s]


def weekly_highlights(days: list[dict]) -> list[dict]:
    """Key stats for the week: warmest day, coldest night, rain, humidity."""
    if not days:
        return []

    warmest = max(days, key=lambda d: d["maximum_temperature_f"])
    coldest = min(days, key=lambda d: d["minimum_temperature_f"])
    highlights = [
        {"label": "Warmest day", "value": f"{whole(warmest['maximum_temperature_f'])}°F",
         "detail": warmest["weekday"]},
        {"label": "Coldest night", "value": f"{whole(coldest['minimum_temperature_f'])}°F",
         "detail": coldest["weekday"]},
    ]

    rainy = [d for d in days if d.get("maximum_precipitation_probability") is not None]
    if rainy:
        wettest = max(rainy, key=lambda d: d["maximum_precipitation_probability"])
        chance = wettest["maximum_precipitation_probability"]
        highlights.append({
            "label": "Best chance of rain",
            "value": f"{chance}%",
            "detail": wettest["weekday"] if chance > 0 else "None expected",
        })

    humid = [d["average_humidity"] for d in days if d.get("average_humidity") is not None]
    if humid:
        highlights.append({
            "label": "Average humidity",
            "value": f"{whole(mean(humid))}%",
            "detail": "Across the week",
        })

    return highlights
