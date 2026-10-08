# ModelWatch — AI Provider Change Monitor

Version 0.3.4. Proprietary software. All Rights Reserved.
This repository provides source for the authorized MCPRush import/build workflow.
It contains no deployment credentials or persistent monitoring data.
Support: alystheexperiment@gmail.com. Never send passwords, tokens or keys.

## Runtime

Python 3.10 or newer. MCPRush Python build: Python 3.12, stdio transport.
Install command: `pip install .`
Start command: `modelwatch-mcp` (no arguments).
The hosting platform provides the stdio to HTTP adapter.

Tools: `check_all`, `status`, `self_test`. Checks are on demand only.
Default sources are Gemini pricing/release notes and Anthropic Python SDK latest
release metadata through the GitHub REST API. Review the official linked source
before acting on a detected change.

## Storage

Local storage is the default and preserves v0.3.3 behavior.
Hosted deployments must set these environment variables through runtime secrets:

- `MODELWATCH_STORAGE`: `redis-rest`
- `UPSTASH_REDIS_REST_URL`: the private database REST endpoint
- `UPSTASH_REDIS_REST_TOKEN`: the private database token
- `MODELWATCH_REDIS_PREFIX`: a unique namespace for each installation

Optional `MODELWATCH_CONFIG_JSON` contains the full source configuration.
Otherwise the bundled configuration is read without copying it to a local file.
Hosted mode rejects file-based `MODELWATCH_CONFIG` and disables reset.
No database endpoint, token or environment file belongs in this repository.

Redis REST uses the Python standard library. Baseline, pending change and history
are committed atomically per source; stale concurrent updates are rejected.
Storage errors never fall back to local persistence. Calendar events are returned
as `calendar_ics` content, without persistent local calendar files in hosted mode.

Isolate credentials between installations. A namespace is not an access-control
boundary for an unrestricted database token. Retained history increases transfer
size; storage, bandwidth and request limits apply. Quota errors fail closed.

## Offline self-test

Run `python -m modelwatch.cli self-test`.
The deterministic self-test uses disposable temporary files and does not contact
external services. Normal hosted operation requires no persistent local files.
