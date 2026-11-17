"""Bloomington weather data pipeline built on Google ADK.

Four specialised agents run in sequence:
    ingestion -> processing -> cloud storage -> visualization

Data is handed between agents through invocation-scoped `temp:` state.
Each tool returns {"status": "success", ...} or {"status": "error",
"message": ...} so a failure is reported clearly instead of crashing the run,
and downstream steps refuse to work with data from an earlier run.
"""

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from google.api_core import exceptions as gcloud_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import storage
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools import ToolContext
from google.genai import types

from .dashboard import render_dashboard
from .weather_utils import (
    WeatherDataError,
    build_daily_summaries,
    generate_weekly_summary,
    validate_raw_weather,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

# Load project configuration.
# `adk run` only loads the first .env found walking up from this folder
# (weather_agent/.env), and a bare load_dotenv() resolves to that same file,
# so the root .env (GCS bucket, project, credentials) must be loaded
# explicitly. override=False keeps any values that are already set.
load_dotenv(BASE_DIR / "weather_agent" / ".env")
load_dotenv(BASE_DIR / ".env")

# Resolve a relative service-account path against the project root so the
# pipeline works no matter which directory `adk run` is launched from.
_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if _credentials and not Path(_credentials).is_absolute():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
        (BASE_DIR / _credentials).resolve()
    )

# Gemini model shared by all four agents (override with GEMINI_MODEL in .env).
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Retry transient Gemini errors (429 rate limits, 5xx such as 503 "high demand")
# with exponential backoff instead of failing the pipeline immediately.
GENERATE_CONTENT_CONFIG = types.GenerateContentConfig(
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=2.0,
            max_delay=30.0,
            http_status_codes=[429, 500, 502, 503, 504],
        )
    )
)

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Bloomington, Indiana coordinates
LOCATION_NAME = "Bloomington, Indiana"
LATITUDE = 39.1653
LONGITUDE = -86.5264
FORECAST_DAYS = 7
TIMEZONE = "America/Indiana/Indianapolis"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
RAW_FILE = "raw_weather.json"
PROCESSED_FILE = "processed_weather.json"
DASHBOARD_FILE = "index.html"
GCS_PREFIX = "weather-data"


# --------------------------------------------------
# SHARED HELPERS
# --------------------------------------------------

def _error(message: str) -> dict:
    logger.error(message)
    return {"status": "error", "message": message}


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temp file then swap it in, so readers never see half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _http_session() -> requests.Session:
    """HTTP session that retries transient Open-Meteo failures."""
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _local_now(weather: dict) -> datetime:
    """Current time in the forecast's own time zone (e.g. EST/EDT)."""
    try:
        return datetime.now(ZoneInfo(weather.get("timezone") or TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        offset = weather.get("utc_offset_seconds") or 0
        return datetime.now(timezone(timedelta(seconds=offset)))


def _md5_base64(path: Path) -> str:
    return base64.b64encode(hashlib.md5(path.read_bytes()).digest()).decode()


# --------------------------------------------------
# AGENT TOOL 1: DATA INGESTION
# --------------------------------------------------

def fetch_weather(tool_context: ToolContext) -> dict:
    """Fetch seven days of hourly weather data from Open-Meteo."""

    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": (
            "temperature_2m,"
            "precipitation_probability,"
            "relative_humidity_2m"
        ),
        # Daily WMO condition code (drives the forecast icons). Requested in
        # the same call, so the pipeline still makes a single API request.
        "daily": "weather_code",
        "forecast_days": FORECAST_DAYS,
        "timezone": TIMEZONE,
    }

    try:
        with _http_session() as session:
            response = session.get(OPEN_METEO_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        return _error(f"Could not reach Open-Meteo: {exc}")

    try:
        weather_data = response.json()
    except ValueError:
        return _error(f"Open-Meteo returned a non-JSON response (HTTP {response.status_code}).")

    if not response.ok:
        reason = weather_data.get("reason") if isinstance(weather_data, dict) else None
        return _error(f"Open-Meteo request failed (HTTP {response.status_code}): {reason or response.reason}")

    try:
        validate_raw_weather(weather_data)
    except WeatherDataError as exc:
        return _error(f"Open-Meteo response is incomplete: {exc}")

    # Saved exactly as received: Celsius, original API structure.
    raw_path = DATA_DIR / RAW_FILE
    _write_atomic(raw_path, json.dumps(weather_data, indent=4))

    tool_context.state["temp:raw_weather"] = weather_data

    return {
        "status": "success",
        "records": len(weather_data["hourly"]["time"]),
        "file": str(raw_path)
    }


# --------------------------------------------------
# AGENT TOOL 2: DATA PROCESSING
# --------------------------------------------------

def process_weather(tool_context: ToolContext) -> dict:
    """Clean hourly data and calculate daily weather summaries in Fahrenheit."""

    weather = tool_context.state.get("temp:raw_weather")

    if not weather:
        return _error("Raw weather data is missing; the ingestion step did not complete in this run.")

    try:
        records, dropped = build_daily_summaries(weather)
    except WeatherDataError as exc:
        return _error(f"Weather data could not be processed: {exc}")

    processed_at = _local_now(weather)

    summary = {
        "location": LOCATION_NAME,
        "latitude": weather.get("latitude"),
        "longitude": weather.get("longitude"),
        "timezone": weather.get("timezone"),
        "timezone_abbreviation": processed_at.tzname(),
        "processed_at": processed_at.isoformat(timespec="seconds"),
        "source": "Open-Meteo",
        "units": {
            "temperature": "°F",
            "precipitation_probability": "%",
            "humidity": "%",
        },
        "total_days": len(records),
        "hourly_readings_discarded": dropped,
        "daily_forecast": records,
        "weekly_summary": generate_weekly_summary(records),
    }

    processed_path = DATA_DIR / PROCESSED_FILE
    _write_atomic(processed_path, json.dumps(summary, indent=4, ensure_ascii=False))

    tool_context.state["temp:processed_weather"] = summary

    result = {
        "status": "success",
        "days_processed": len(records),
        "first_day": f"{records[0]['weekday']} {records[0]['date']}",
        "last_day": f"{records[-1]['weekday']} {records[-1]['date']}",
        "temperature_unit": "°F",
        "file": str(processed_path)
    }
    if len(records) != FORECAST_DAYS:
        result["warning"] = f"Expected {FORECAST_DAYS} days but produced {len(records)}."
    if dropped:
        result["discarded_hourly_readings"] = dropped
    return result


# --------------------------------------------------
# AGENT TOOL 3: CLOUD STORAGE
# --------------------------------------------------

def upload_to_cloud(tool_context: ToolContext) -> dict:
    """Upload raw and processed weather data to Google Cloud Storage."""

    # Only upload files produced in this run, never stale ones from disk.
    if not tool_context.state.get("temp:processed_weather"):
        return _error("Upload skipped: no data was processed in this run.")

    bucket_name = os.getenv("GCS_BUCKET_NAME")
    if not bucket_name:
        return _error("GCS_BUCKET_NAME is missing from .env")

    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials_path and not Path(credentials_path).is_file():
        return _error("The service account file named in GOOGLE_APPLICATION_CREDENTIALS was not found.")

    files = [DATA_DIR / RAW_FILE, DATA_DIR / PROCESSED_FILE]
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        return _error(f"Local data files are missing: {', '.join(missing)}")

    uploaded = []
    try:
        client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
        bucket = client.bucket(bucket_name)

        for file_path in files:
            blob = bucket.blob(f"{GCS_PREFIX}/{file_path.name}")
            blob.upload_from_filename(str(file_path), content_type="application/json")

            # Confirm the stored object is byte-for-byte the local file.
            blob.reload()
            if blob.md5_hash != _md5_base64(file_path):
                return _error(f"Checksum mismatch after uploading {file_path.name}.")
            uploaded.append(blob.name)

    except auth_exceptions.DefaultCredentialsError:
        return _error("Google Cloud credentials are missing or invalid.")
    except gcloud_exceptions.GoogleAPIError as exc:
        return _error(f"Cloud Storage upload failed: {exc}")
    except OSError as exc:
        return _error(f"Could not read a local file for upload: {exc}")

    tool_context.state["temp:storage_status"] = "success"

    return {
        "status": "success",
        "bucket": bucket_name,
        "uploaded_files": uploaded,
        "checksums_verified": True
    }


# --------------------------------------------------
# AGENT TOOL 4: VISUALIZATION
# --------------------------------------------------

def create_dashboard(tool_context: ToolContext) -> dict:
    """Generate the HTML weather dashboard from this run's processed data."""

    summary = tool_context.state.get("temp:processed_weather")

    if not summary:
        return _error("Processed weather data is missing; the processing step did not complete in this run.")

    try:
        html = render_dashboard(summary)
    except (KeyError, ValueError, TypeError) as exc:
        return _error(f"Dashboard could not be generated: {exc}")

    dashboard_path = OUTPUT_DIR / DASHBOARD_FILE
    try:
        _write_atomic(dashboard_path, html)
    except OSError as exc:
        return _error(f"Dashboard could not be saved: {exc}")

    return {
        "status": "success",
        "dashboard": str(dashboard_path),
        "days_shown": summary["total_days"],
        "data_processed_at": summary["processed_at"]
    }


# --------------------------------------------------
# SPECIALIZED ADK AGENTS
# --------------------------------------------------

ERROR_POLICY = (
    "If the tool returns status 'error', clearly report the error message "
    "and do not claim the step succeeded. Report only on your own step."
)

ingestion_agent = Agent(
    name="ingestion_agent",
    model=MODEL,
    generate_content_config=GENERATE_CONTENT_CONFIG,
    description="Retrieves raw weather data from Open-Meteo.",
    instruction=(
        "You are the data ingestion specialist. "
        "You MUST call fetch_weather exactly once. "
        "Report the number of records retrieved. " + ERROR_POLICY
    ),
    tools=[fetch_weather]
)

processing_agent = Agent(
    name="processing_agent",
    model=MODEL,
    generate_content_config=GENERATE_CONTENT_CONFIG,
    description="Cleans and aggregates weather data.",
    instruction=(
        "You are the data processing specialist. "
        "You MUST call process_weather exactly once. "
        "Report the daily aggregation results, including the date range. "
        + ERROR_POLICY
    ),
    tools=[process_weather]
)

storage_agent = Agent(
    name="storage_agent",
    model=MODEL,
    generate_content_config=GENERATE_CONTENT_CONFIG,
    description="Stores pipeline data in Google Cloud Storage.",
    instruction=(
        "You are the cloud storage specialist. "
        "You MUST call upload_to_cloud exactly once. "
        "Report the upload result accurately. " + ERROR_POLICY
    ),
    tools=[upload_to_cloud]
)

visualization_agent = Agent(
    name="visualization_agent",
    model=MODEL,
    generate_content_config=GENERATE_CONTENT_CONFIG,
    description="Creates an HTML weather dashboard.",
    instruction=(
        "You are the visualization specialist. "
        "You MUST call create_dashboard exactly once. "
        "Report where the dashboard was created. " + ERROR_POLICY
    ),
    tools=[create_dashboard]
)


# --------------------------------------------------
# ROOT ORCHESTRATOR
# --------------------------------------------------

root_agent = SequentialAgent(
    name="weather_pipeline",
    description="Orchestrates the complete weather data engineering pipeline.",
    sub_agents=[
        ingestion_agent,
        processing_agent,
        storage_agent,
        visualization_agent
    ]
)
