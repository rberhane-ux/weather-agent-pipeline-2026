# Future Improvements

This roadmap lists how the Bloomington Weather Analytics Pipeline could grow from a single-location portfolio project into a production-grade service. Items are grouped by theme and roughly ordered by value within each section.

---

## 1. Scalability

- **Multiple locations.** Move the hard-coded Bloomington coordinates into a configuration file or table (name, latitude, longitude, time zone) and run the pipeline once per location. Open-Meteo accepts comma-separated coordinate lists, so many locations could be fetched in a single request.
- **Partitioned storage layout.** Write files to date- and location-partitioned paths (e.g. `weather-data/location=bloomington/date=2026-11-17/raw.json`) instead of overwriting one object. This builds a history that can be queried later.
- **Analytical warehouse.** Load processed records into BigQuery (or use external tables over GCS) to support historical trend analysis and SQL access.
- **Deterministic orchestration for scale.** The four stages are deterministic, so at higher volumes they could run as plain functions (Cloud Run Jobs, Cloud Functions or Airflow tasks). Gemini would then be used only where it adds value, such as narrative summaries.
- **Parallel fan-out.** Use ADK's `ParallelAgent` (or async tasks) to process multiple locations concurrently, then aggregate the results.

## 2. Error handling and reliability

- **Stop on failure.** The pipeline currently reports errors and later stages refuse to run without upstream data. A custom orchestrator or ADK callback could halt the `SequentialAgent` as soon as a stage fails, saving LLM calls.
- **Dead-letter storage.** Save invalid or unexpected API responses to a `quarantine/` prefix for later inspection.
- **Upload retries.** Add explicit retry policies (`google.api_core.retry.Retry`) for transient GCS errors, and use generation preconditions (`if_generation_match`) to avoid accidental concurrent overwrites.
- **Model fallback.** Fall back automatically to a secondary Gemini model if the primary one returns sustained 503 "high demand" errors.
- **Last-known-good dashboard.** If a run fails, keep serving the previous dashboard with a visible "data may be outdated" banner instead of failing silently.
- **Idempotent runs.** Give each run an ID and store it with its outputs, so a rerun of the same run replaces its own files instead of producing duplicates.

## 3. Monitoring and logging

- **Structured logging.** Replace plain log messages with JSON logs (run ID, stage, duration, record counts, status) that Cloud Logging can index.
- **Run metrics.** Track records fetched, days produced, readings discarded, upload bytes, stage latency and Gemini token usage. Publish them as Cloud Monitoring custom metrics.
- **Alerting.** Send email/Slack alerts when a run fails, produces fewer than 7 days, discards an unusual share of readings, or hasn't succeeded in 24 hours.
- **Tracing.** Enable ADK's OpenTelemetry tracing to see agent and tool spans end to end in Cloud Trace.
- **Run history.** Keep a small `runs` table or JSON log recording each execution's status and outputs for auditing.

## 4. Scheduling and automation

- **Scheduled runs.** Containerize the pipeline and trigger it with Cloud Scheduler → Cloud Run Jobs (e.g. every 3 hours), or with a cron job or GitHub Actions schedule.
- **Non-interactive entry point.** Add a small CLI (`python -m weather_agent.run`) that runs the pipeline with ADK's `Runner` and exits with a non-zero code on failure, for use in automation.
- **Automatic dashboard publishing.** Upload `output/index.html` to a static-website bucket, Firebase Hosting or GitHub Pages after each run, so the dashboard is always online.
- **CI/CD.** Run tests and linting on every pull request with GitHub Actions, and deploy automatically from `main`.

## 5. Testing

- **pytest migration.** Move to `pytest` with fixtures and parametrized cases, and add coverage reporting (`pytest-cov`) with a minimum threshold in CI.
- **Recorded API fixtures.** Store a few real, anonymized Open-Meteo responses (sunny week, rainy week, snow, DST transition) as regression fixtures.
- **Integration tests.** Test GCS against a dedicated test bucket or an emulator, and run the full agent pipeline with a mocked model to check orchestration.
- **Property-based tests.** Use Hypothesis to check invariants (e.g. low ≤ average ≤ high, exactly one conversion, valid percentages) across random inputs.
- **Visual regression tests.** Use Playwright screenshots at mobile, tablet and desktop widths to catch layout regressions in the dashboard.
- **Accessibility checks.** Run automated audits (axe-core / Lighthouse) for contrast, labels and keyboard navigation.

## 6. Security

- **Keyless authentication.** Replace the downloaded service account key with Workload Identity Federation (GitHub Actions) or an attached service account (Cloud Run). This removes long-lived keys entirely.
- **Secret management.** Store the Gemini API key in Google Secret Manager instead of a local `.env` file.
- **Least privilege.** Grant the service account access only to the specific bucket and prefix it needs. Consider separate identities for read and write.
- **Bucket hardening.** Enable Uniform Bucket-Level Access and Public Access Prevention, and turn on object versioning for recovery.
- **Secret scanning.** Add pre-commit hooks (e.g. `gitleaks` or `detect-secrets`) and enable GitHub secret scanning to block accidental credential commits.
- **Dependency hygiene.** Pin exact versions with a lock file (`pip-tools` / `uv`) and enable Dependabot or `pip-audit` for vulnerability alerts.
- **Content Security Policy.** Self-host Chart.js with a Subresource Integrity hash, or use a CSP header when the dashboard is hosted.

## 7. Cost optimization

- **Reduce LLM calls.** Each run makes at least four Gemini calls just to trigger deterministic tools. Running the stages as plain code, or using a single agent, would cut cost and latency. Gemini could be kept only for optional natural-language reporting.
- **Use the smallest capable model.** Keep lightweight "flash-lite" models for tool routing and benchmark them against larger models before upgrading.
- **Skip unchanged data.** Compare the new forecast's hash with the last stored one and skip the upload and dashboard rebuild when nothing changed.
- **Storage lifecycle rules.** Once history is kept, move old raw files to Nearline/Coldline storage and delete them after a retention period.
- **Right-size scheduling.** Open-Meteo models update every few hours, so running more often than that adds cost without new information.

## 8. Data quality

- **Schema validation.** Define explicit schemas (Pydantic or JSON Schema) for the raw and processed files, and reject or quarantine data that doesn't conform.
- **Completeness checks.** Flag days with fewer than 24 hourly readings and show partial days clearly on the dashboard.
- **Range and consistency checks.** Check that humidity and precipitation probability stay within 0–100%, that low ≤ average ≤ high, and that timestamps are continuous and hourly.
- **Anomaly detection.** Compare each forecast with recent runs and flag implausible jumps (e.g. a 30 °F change in the same day's forecast between runs).
- **Forecast accuracy tracking.** Store forecasts and later compare them with observed data (e.g. Open-Meteo historical or NOAA) to measure forecast error over time.
- **Data lineage.** Record the source URL, request parameters, API model and generation time with every processed file, so any dashboard value can be traced back to its raw input.
- **Richer variables.** Add precipitation amount, wind speed, UV index, and sunrise/sunset times to the single existing request, which keeps the one-call design.
