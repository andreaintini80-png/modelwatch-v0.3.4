#!/usr/bin/env python3
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
import argparse
import datetime as dt
import difflib
import hashlib
import html
import json
import os
import re
import tempfile
import urllib.request
import urllib.error
import urllib.robotparser
import socket
import ipaddress
import sys

VERSION = "0.3.4"
from .storage import hosted, RedisREST, StorageError
from contextvars import ContextVar
_SELF_TEST_LOCAL = ContextVar("modelwatch_local_selftest", default=False)
MIN_PYTHON = (3, 10)

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "sources.json"

def _user_data_root():
    override = os.environ.get("MODELWATCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ModelWatch"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home()) / "ModelWatch"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "modelwatch"

ROOT = _user_data_root()
STATE = ROOT / "state"
SNAPS = STATE / "snapshots"
PENDING = STATE / "pending"
HISTORY = STATE / "history"
EVENTS = STATE / "events"
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_CONFIRMATIONS = 2


def get_config_path():
    override = os.environ.get("MODELWATCH_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return ROOT / "config" / "sources.json"


class Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            s = " ".join(data.split())
            if s:
                self.parts.append(s)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_text(path, text):
    if hasattr(path, "write_atomic"):
        path.write_atomic(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


LEGACY_XAI_DEFAULT = {
    "vendor": "xAI",
    "name": "API pricing",
    "url": "https://docs.x.ai/developers/pricing",
}
LEGACY_ANTHROPIC_NEWS_DEFAULT = {
    "vendor": "Anthropic",
    "name": "News and product updates",
    "url": "https://www.anthropic.com/news",
}
ANTHROPIC_GITHUB_DEFAULT = {
    "vendor": "Anthropic",
    "name": "SDK/API latest release",
    "url": "https://api.github.com/repos/anthropics/anthropic-sdk-python/releases/latest",
}
CURRENT_CONFIG_VERSION = 3


def _migrate_config(data, config_path):
    """Apply narrow, idempotent migrations to ModelWatch-managed config.

    Version 2 removes the exact legacy xAI default source that shipped in
    pre-0.3.1 builds. Other custom sources are preserved.
    """
    changed = False
    try:
        version = int(data.get("config_version", 1))
    except (TypeError, ValueError):
        version = 1

    if version < 2:
        sources = data.get("sources")
        if isinstance(sources, list):
            filtered = [src for src in sources if src != LEGACY_XAI_DEFAULT]
            if len(filtered) != len(sources):
                data["sources"] = filtered
                changed = True
        version = 2
        data["config_version"] = version
        changed = True

    if version < 3:
        sources = data.get("sources")
        if isinstance(sources, list):
            had_legacy = any(src == LEGACY_ANTHROPIC_NEWS_DEFAULT for src in sources)
            filtered = [src for src in sources if src != LEGACY_ANTHROPIC_NEWS_DEFAULT]
            if had_legacy and ANTHROPIC_GITHUB_DEFAULT not in filtered:
                filtered.append(dict(ANTHROPIC_GITHUB_DEFAULT))
            if filtered != sources:
                data["sources"] = filtered
                changed = True
        version = 3
        data["config_version"] = version
        changed = True

    if changed and not os.environ.get("MODELWATCH_CONFIG") and (not hosted() or _SELF_TEST_LOCAL.get()):
        atomic_write_json(config_path, data)
    return data


def load_config():
    config_path = get_config_path()
    if hosted() and not _SELF_TEST_LOCAL.get():
        config_path = DEFAULT_CONFIG
        if os.environ.get("MODELWATCH_CONFIG"):
            raise StorageError("Hosted mode requires bundled configuration or MODELWATCH_CONFIG_JSON")
    if (not hosted() or _SELF_TEST_LOCAL.get()) and not config_path.exists() and not os.environ.get("MODELWATCH_CONFIG"):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(config_path, DEFAULT_CONFIG.read_text(encoding="utf-8"))
    try:
        data = json.loads(os.environ.get("MODELWATCH_CONFIG_JSON", config_path.read_text(encoding="utf-8"))) if hosted() and not _SELF_TEST_LOCAL.get() else json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ValueError(f"Configuration file is missing: {config_path}") from e
    except PermissionError as e:
        raise ValueError(f"Configuration file is not readable: {config_path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Configuration JSON is invalid at line {e.lineno}, column {e.colno}"
        ) from e
    except UnicodeError as e:
        raise ValueError("Configuration must be valid UTF-8 text") from e
    except OSError as e:
        raise ValueError(f"Configuration could not be read: {type(e).__name__}") from e
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a JSON object")
    data = _migrate_config(data, config_path)
    settings = data.setdefault("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("settings must be a JSON object")
    settings.setdefault("max_bytes", DEFAULT_MAX_BYTES)
    settings.setdefault("confirmations_required", DEFAULT_CONFIRMATIONS)
    try:
        settings["max_bytes"] = int(settings["max_bytes"])
        settings["confirmations_required"] = int(settings["confirmations_required"])
    except (TypeError, ValueError) as e:
        raise ValueError("max_bytes and confirmations_required must be integers") from e
    if not 1_024 <= settings["max_bytes"] <= 10_000_000:
        raise ValueError("max_bytes must be between 1024 and 10000000")
    if not 1 <= settings["confirmations_required"] <= 5:
        raise ValueError("confirmations_required must be between 1 and 5")
    sources = data.setdefault("sources", [])
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty JSON array")
    seen = set()
    for i, src in enumerate(sources, 1):
        if not isinstance(src, dict):
            raise ValueError(f"source #{i} must be a JSON object")
        for field in ("vendor", "name", "url"):
            if not isinstance(src.get(field), str) or not src[field].strip():
                raise ValueError(f"source #{i} has invalid {field}")
            src[field] = src[field].strip()
        validate_public_https(src["url"], resolve_host=False)
        identity = (src["vendor"], src["name"], src["url"])
        if identity in seen:
            raise ValueError(f"duplicate source #{i}: {src['vendor']} — {src['name']}")
        seen.add(identity)
    return data


def _reject_non_public_ip(ip_text):
    ip = ipaddress.ip_address(ip_text)
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    ):
        raise ValueError(f"Non-public destination is not allowed: {ip}")


def validate_public_https(url, resolve_host=False):
    p = urlparse(url)
    if p.scheme.lower() != "https":
        raise ValueError("Only HTTPS sources are allowed in production configuration")
    if not p.hostname:
        raise ValueError("Source URL has no hostname")
    if p.username or p.password:
        raise ValueError("Credentials in source URLs are not allowed")
    host = p.hostname.lower()
    blocked = {"localhost", "localhost.localdomain"}
    if host in blocked or host.endswith(".local"):
        raise ValueError("Local/private hostnames are not allowed")
    try:
        _reject_non_public_ip(host)
        return
    except ValueError as e:
        # A literal IP was parsed and rejected. Hostnames fall through to DNS checks.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise e
    if resolve_host:
        try:
            infos = socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise ValueError(f"DNS resolution failed for {host}: {e}") from e
        if not infos:
            raise ValueError(f"DNS resolution returned no addresses for {host}")
        for info in infos:
            _reject_non_public_ip(info[4][0])



ROBOT_USER_AGENT = "ModelWatch"
ROBOTS_MAX_BYTES = 256_000


def _robots_url_for(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/robots.txt"


def robots_allows(url):
    """Return whether robots.txt allows ModelWatch to fetch *url*.

    Fails closed when robots.txt exists but cannot be verified. A 404/410 is
    treated as no robots policy. This is a technical safety gate, not legal
    advice or a substitute for reviewing a site's terms.
    """
    robots_url = _robots_url_for(url)
    validate_public_https(robots_url, resolve_host=True)
    req = urllib.request.Request(
        robots_url,
        headers={
            "User-Agent": f"ModelWatch/{VERSION} (+mailto:alystheexperiment@gmail.com)",
            "Accept": "text/plain,*/*;q=0.1",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler())
    try:
        with opener.open(req, timeout=15) as r:
            final_url = r.geturl()
            validate_public_https(final_url, resolve_host=True)
            raw = r.read(ROBOTS_MAX_BYTES + 1)
            if len(raw) > ROBOTS_MAX_BYTES:
                raise ValueError("robots.txt exceeds safety limit")
            text = raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True
        if e.code in (401, 403):
            return False
        raise ValueError(f"Could not verify robots.txt: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"Could not verify robots.txt: {type(e).__name__}: {e}") from e

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(final_url)
    rp.parse(text.splitlines())
    return rp.can_fetch(ROBOT_USER_AGENT, url)

class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_https(newurl, resolve_host=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, max_bytes=DEFAULT_MAX_BYTES):
    validate_public_https(url, resolve_host=True)
    if not robots_allows(url):
        raise ValueError("Automated access is disallowed by robots.txt for this URL")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"ModelWatch/{VERSION} (+mailto:alystheexperiment@gmail.com)",
            "Accept": "text/html,text/plain,application/json,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler())
    with opener.open(req, timeout=25) as r:
        final_url = r.geturl()
        validate_public_https(final_url, resolve_host=True)
        content_length = r.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError(f"Response exceeds safety limit of {max_bytes} bytes")
            except ValueError as e:
                if "exceeds safety limit" in str(e):
                    raise
        ctype = (r.headers.get_content_type() or "").lower()
        allowed = ctype.startswith("text/") or ctype in {
            "application/json", "application/xhtml+xml", "application/xml"
        }
        if ctype and not allowed:
            raise ValueError(f"Unsupported content type: {ctype}")
        raw = r.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"Response exceeds safety limit of {max_bytes} bytes")
        decoded = raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    p = Extractor()
    p.feed(decoded)
    text = html.unescape("\n".join(p.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("Fetched page produced no readable text")
    return text


def source_key(src):
    return hashlib.sha256(f'{src["vendor"]}|{src["name"]}|{src["url"]}'.encode()).hexdigest()[:16]


def path_for(src):
    return SNAPS / f"{source_key(src)}.json"


def pending_path_for(src):
    return PENDING / f"{source_key(src)}.json"


def history_dir_for(src):
    return HISTORY / source_key(src)


def added_lines(diff_lines):
    return [x[1:].strip() for x in diff_lines if x.startswith("+") and not x.startswith("+++") and x[1:].strip()]


def changed_lines(diff_lines):
    return [
        x[1:].strip()
        for x in diff_lines
        if (x.startswith("+") and not x.startswith("+++"))
        or (x.startswith("-") and not x.startswith("---"))
        if x[1:].strip()
    ]


def classify_change(lines):
    text = " ".join(lines).lower()
    groups = [
        ("BREAKING", ["breaking", "incompatible", "removed", "no longer supported", "migration required"]),
        ("DEPRECATION", ["deprecat", "sunset", "retire", "end of life", "eol"]),
        ("COST", ["pricing", "price", "cost", "per million", "per 1m", "$", "usd", "billing"]),
        ("POLICY", ["policy", "terms", "privacy", "compliance", "restriction", "prohibited"]),
        ("CAPABILITY", ["new model", "launch", "available", "supports", "context window", "capability", "preview"]),
    ]
    for label, words in groups:
        if any(w in text for w in words):
            return label
    return "INFO"


MONTHS = {m.lower(): i for i, m in enumerate([
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"
], 1)}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})


def parse_date_candidates(lines, today=None):
    text = "\n".join(lines)
    found = []
    for y, m, d in re.findall(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", text):
        try:
            found.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    month_rx = r"January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    for mon, d, y in re.findall(rf"\b({month_rx})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b", text, flags=re.I):
        key = mon.lower()
        key = "sep" if key == "sept" else key
        try:
            found.append(dt.date(int(y), MONTHS[key], int(d)))
        except (ValueError, KeyError):
            pass
    for d, mon, y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_rx})\.?[,]?\s+(20\d{{2}})\b", text, flags=re.I):
        key = mon.lower()
        key = "sep" if key == "sept" else key
        try:
            found.append(dt.date(int(y), MONTHS[key], int(d)))
        except (ValueError, KeyError):
            pass
    today = today or dt.datetime.now(dt.timezone.utc).date()
    future = sorted({x for x in found if x >= today})
    return future[0] if future else None


def ics_escape(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_ical_line(line, limit=75):
    # RFC 5545 uses octets. Continuation lines begin with one space.
    chunks = []
    current = ""
    current_bytes = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        allowed = limit if not chunks else limit - 1
        if current and current_bytes + b > allowed:
            chunks.append(current)
            current = ch
            current_bytes = b
        else:
            current += ch
            current_bytes += b
    if current or not chunks:
        chunks.append(current)
    return "\r\n ".join(chunks)


def create_ics(src, category, date_value, added, remote=False):
    if not remote:
        EVENTS.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f'{src["vendor"]}-{src["name"]}'.lower()).strip("-")[:70]
    p = EVENTS / f"{date_value.isoformat()}-{slug}.ics"
    uid = hashlib.sha256(f'{src["url"]}|{date_value.isoformat()}|{category}'.encode()).hexdigest()[:20] + "@modelwatch"
    summary = f'ModelWatch: {src["vendor"]} — {category}'
    excerpt = " ".join(added)[:900]
    desc = f'{src["name"]}. Review the official source before acting. Source: {src["url"]}. Detected change: {excerpt}'
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = date_value.strftime("%Y%m%d")
    next_day = (date_value + dt.timedelta(days=1)).strftime("%Y%m%d")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Alys//ModelWatch {VERSION}//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day}",
        f"DTEND;VALUE=DATE:{next_day}",
        f"SUMMARY:{ics_escape(summary)}",
        f"DESCRIPTION:{ics_escape(desc)}",
        f"URL:{ics_escape(src['url'])}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    body = "\r\n".join(fold_ical_line(x) for x in lines) + "\r\n"
    if remote:
        return body
    atomic_write_text(p, body)
    return str(p.relative_to(ROOT))


def safe_load_json(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"{label} is corrupt or unreadable: {type(e).__name__}: {e}")


def make_snapshot(text):
    return {"fetched_at": now(), "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}


def archive_baseline(src, old, session=None):
    d = session.path("history") if session else history_dir_for(src)
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    atomic_write_json(d / f"{stamp}-{old.get('sha256','unknown')[:12]}.json", old)


def _process_text(src, text, confirmations_required=DEFAULT_CONFIRMATIONS, session=None):
    out = {"vendor": src["vendor"], "name": src["name"], "url": src["url"], "checked_at": now()}
    snap = make_snapshot(text)
    p = session.path("baseline") if session else path_for(src)
    pp = session.path("pending") if session else pending_path_for(src)
    p.parent.mkdir(parents=True, exist_ok=True)
    pp.parent.mkdir(parents=True, exist_ok=True)

    if not p.exists():
        atomic_write_json(p, snap)
        if pp.exists():
            pp.unlink()
        out["status"] = "BASELINE_CREATED"
        return out

    try:
        old = safe_load_json(p, "Baseline")
    except ValueError as e:
        out.update(status="ERROR", error=str(e))
        return out

    if old.get("sha256") == snap["sha256"]:
        if pp.exists():
            pp.unlink()
        out["status"] = "UNCHANGED"
        return out

    diff = list(difflib.unified_diff(old.get("text", "").splitlines(), text.splitlines(), fromfile="previous", tofile="current", lineterm=""))
    added = added_lines(diff)
    impact = changed_lines(diff)
    category = classify_change(impact)
    date_value = parse_date_candidates(added)

    required = max(1, int(confirmations_required))
    if required > 1:
        pending = None
        if pp.exists():
            try:
                pending = safe_load_json(pp, "Pending snapshot")
            except ValueError:
                pp.unlink(missing_ok=True)
        if not pending or pending.get("sha256") != snap["sha256"]:
            pending_obj = dict(snap)
            pending_obj["confirmations"] = 1
            atomic_write_json(pp, pending_obj)
            out.update(
                status="CHANGE_PENDING",
                category=category,
                detected_date=date_value.isoformat() if date_value else None,
                confirmations=f"1/{required}",
                diff="\n".join(diff[:160]),
            )
            return out
        count = int(pending.get("confirmations", 1)) + 1
        if count < required:
            pending["confirmations"] = count
            atomic_write_json(pp, pending)
            out.update(
                status="CHANGE_PENDING",
                category=category,
                detected_date=date_value.isoformat() if date_value else None,
                confirmations=f"{count}/{required}",
                diff="\n".join(diff[:160]),
            )
            return out

    out.update(
        status="CHANGED",
        diff="\n".join(diff[:160]),
        category=category,
        added_excerpt="\n".join(added[:30]),
    )
    if date_value:
        out["detected_date"] = date_value.isoformat()
        if category in {"COST", "DEPRECATION", "BREAKING", "POLICY", "CAPABILITY"}:
            out["calendar_ics" if session else "calendar_file"] = create_ics(src, category, date_value, added, remote=bool(session))

    archive_baseline(src, old, session=session)
    atomic_write_json(p, snap)
    if pp.exists():
        pp.unlink()
    return out


def process_text(src, text, confirmations_required=DEFAULT_CONFIRMATIONS):
    if not hosted() or _SELF_TEST_LOCAL.get():
        return _process_text(src, text, confirmations_required)
    try:
        session = RedisREST().session(source_key(src))
        out = _process_text(src, text, confirmations_required, session=session)
        if out["status"] != "ERROR":
            session.commit()
        return out
    except StorageError as e:
        return {"vendor": src["vendor"], "name": src["name"], "url": src["url"],
                "checked_at": now(), "status": "ERROR", "error": str(e)}


def state_presence(src):
    if hosted() and not _SELF_TEST_LOCAL.get():
        session = RedisREST().session(source_key(src))
        return session.path("baseline").exists(), session.path("pending").exists()
    return path_for(src).exists(), pending_path_for(src).exists()


def check(src, max_bytes, confirmations_required):
    out = {"vendor": src["vendor"], "name": src["name"], "url": src["url"], "checked_at": now()}
    try:
        text = fetch(src["url"], max_bytes=max_bytes)
        return process_text(src, text, confirmations_required=confirmations_required)
    except Exception as e:
        out.update(status="ERROR", error=f"{type(e).__name__}: {e}")
        return out


def reset_state():
    if hosted() and not _SELF_TEST_LOCAL.get():
        raise StorageError("Hosted reset is disabled; use a fresh installation namespace")
    for d in (SNAPS, PENDING, EVENTS, HISTORY):
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
    print("ModelWatch local state reset.")


def self_test():
    global ROOT, STATE, SNAPS, PENDING, HISTORY, EVENTS, _SELF_TEST_LOCAL
    test_token = _SELF_TEST_LOCAL.set(True)
    original = (ROOT, STATE, SNAPS, PENDING, HISTORY, EVENTS)
    failures = []
    with tempfile.TemporaryDirectory(prefix="modelwatch-selftest-") as td:
        ROOT = Path(td)
        STATE = ROOT / ".modelwatch"
        SNAPS = STATE / "snapshots"
        PENDING = STATE / "pending"
        HISTORY = STATE / "history"
        EVENTS = STATE / "events"
        src = {"vendor": "SelfTest", "name": "Pricing", "url": "https://example.com/pricing"}
        try:
            r1 = process_text(src, "Pricing baseline", 2)
            assert r1["status"] == "BASELINE_CREATED"
            r2 = process_text(src, "Pricing baseline", 2)
            assert r2["status"] == "UNCHANGED"
            changed = "Pricing baseline\nPricing changes on October 1, 2099. API price will increase to $2 per 1M tokens."
            r3 = process_text(src, changed, 2)
            assert r3["status"] == "CHANGE_PENDING" and r3["category"] == "COST"
            r4 = process_text(src, changed, 2)
            assert r4["status"] == "CHANGED" and r4["category"] == "COST"
            assert r4.get("detected_date") == "2099-10-01"
            ics = ROOT / r4["calendar_file"]
            raw_ics = ics.read_bytes()
            txt = raw_ics.decode("utf-8")
            assert "DTSTART;VALUE=DATE:20991001" in txt
            assert "DTEND;VALUE=DATE:20991002" in txt
            assert all(len(line) <= 75 for line in raw_ics.split(b"\r\n") if line)

            src2 = {"vendor": "SelfTest", "name": "Transient", "url": "https://example.com/transient"}
            process_text(src2, "Stable content", 2)
            a = process_text(src2, "Transient changed content", 2)
            b = process_text(src2, "Stable content", 2)
            assert a["status"] == "CHANGE_PENDING" and b["status"] == "UNCHANGED"

            for unsafe in ("file:///etc/passwd", "http://example.com", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://[::1]/x"):
                try:
                    validate_public_https(unsafe)
                    failures.append(f"unsafe URL was accepted: {unsafe}")
                except ValueError:
                    pass

            # Past-only dates must not create actionable calendar dates.
            assert parse_date_candidates(["Price changed on January 1, 2020."], today=dt.date(2026, 1, 1)) is None

            # robots.txt wildcard rules must be honored by ModelWatch.
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(["User-agent: *", "Disallow: /private", "Allow: /"])
            assert not rp.can_fetch(ROBOT_USER_AGENT, "https://example.com/private/data")
            assert rp.can_fetch(ROBOT_USER_AGENT, "https://example.com/public")

            # Deletions can still carry material impact even with no added keyword.
            deletion_diff = ["--- previous", "+++ current", "-API pricing will increase to $9", "+Updated terms"]
            assert classify_change(changed_lines(deletion_diff)) == "COST"

            src3 = {"vendor": "SelfTest", "name": "Corrupt", "url": "https://example.com/corrupt"}
            process_text(src3, "Good baseline", 2)
            path_for(src3).write_text("{broken json", encoding="utf-8")
            c = process_text(src3, "Different content", 2)
            assert c["status"] == "ERROR" and "corrupt" in c["error"].lower()

            legacy_cfg = {
                "settings": {"confirmations_required": 2, "max_bytes": 2000000},
                "sources": [
                    {"vendor": "Google", "name": "Gemini API pricing", "url": "https://ai.google.dev/gemini-api/docs/pricing"},
                    dict(LEGACY_XAI_DEFAULT),
                    dict(LEGACY_ANTHROPIC_NEWS_DEFAULT),
                    {"vendor": "Custom", "name": "Keep me", "url": "https://example.com/custom"},
                ],
            }
            migrated = _migrate_config(legacy_cfg, Path(td) / "migration.json")
            assert migrated["config_version"] == CURRENT_CONFIG_VERSION
            assert LEGACY_XAI_DEFAULT not in migrated["sources"]
            assert LEGACY_ANTHROPIC_NEWS_DEFAULT not in migrated["sources"]
            assert ANTHROPIC_GITHUB_DEFAULT in migrated["sources"]
            assert any(src.get("vendor") == "Custom" for src in migrated["sources"])
        except AssertionError as e:
            failures.append(f"assertion failed: {e}")
        except Exception as e:
            failures.append(f"unexpected error: {type(e).__name__}: {e}")
    ROOT, STATE, SNAPS, PENDING, HISTORY, EVENTS = original
    _SELF_TEST_LOCAL.reset(test_token)
    if failures:
        print("SELF_TEST_FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("SELF_TEST_OK")
    print(" - baseline + unchanged")
    print(" - two-step change confirmation")
    print(" - COST classification + future date extraction")
    print(" - RFC-style .ics generation + line folding")
    print(" - transient-change rejection")
    print(" - unsafe scheme/private destination rejection")
    print(" - corrupt-baseline fail-safe")
    print(" - future-date-only calendar gating")
    print(" - robots.txt wildcard compliance")
    print(" - deletion-aware impact classification")
    print(" - legacy default-source migrations")
    return 0


def main():
    if sys.version_info < MIN_PYTHON:
        required = ".".join(map(str, MIN_PYTHON))
        current = ".".join(map(str, sys.version_info[:3]))
        print(f"RUNTIME_ERROR: ModelWatch requires Python {required} or newer; found {current}.")
        return 2

    ap = argparse.ArgumentParser(description="Local-first AI vendor change monitor")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("check-all")
    c.add_argument("--json", action="store_true")
    sp.add_parser("reset")
    sp.add_parser("self-test")
    sp.add_parser("status")
    a = ap.parse_args()

    if a.cmd == "status":
        cfg_path = get_config_path()
        print(json.dumps({
            "version": VERSION,
            "storage": "redis-rest" if hosted() else "local",
            "data_root": None if hosted() else str(ROOT),
            "config_path": "environment/bundled" if hosted() else str(cfg_path),
            "state_root": None if hosted() else str(STATE),
            "config_exists": True if hosted() else cfg_path.exists(),
        }, indent=2))
        return 0
    if a.cmd == "reset":
        reset_state()
        return 0
    if a.cmd == "self-test":
        return self_test()

    try:
        cfg = load_config()
    except ValueError as e:
        print(f"CONFIG_ERROR: {e}")
        return 2
    max_bytes = int(cfg["settings"].get("max_bytes", DEFAULT_MAX_BYTES))
    confirmations_required = int(cfg["settings"].get("confirmations_required", DEFAULT_CONFIRMATIONS))
    results = [check(s, max_bytes=max_bytes, confirmations_required=confirmations_required) for s in cfg["sources"]]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f'[{r["status"]}] {r["vendor"]} — {r["name"]}')
            if r["status"] == "CHANGE_PENDING":
                print(f'  confirmations: {r.get("confirmations", "")}; category: {r.get("category", "INFO")}')
                if r.get("detected_date"):
                    print(f'  detected_date: {r["detected_date"]}')
                print(r.get("diff", "")[:5000])
            if r["status"] == "CHANGED":
                print(f'  category: {r.get("category", "INFO")}')
                if r.get("detected_date"):
                    print(f'  detected_date: {r["detected_date"]}')
                if r.get("calendar_file"):
                    print(f'  calendar: {r["calendar_file"]}')
                print(r.get("diff", "")[:5000])
            if r["status"] == "ERROR":
                print(" ", r["error"])
    return 2 if any(r["status"] == "ERROR" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
