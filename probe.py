#!/usr/bin/env python3
"""Free LLM Health Probe Bot.

Probes free LLM providers every hour, stores results in SQLite.
Generates daily markdown reports + current.json for model selection.

Keyless probes to avoid Cloudflare false-negatives (error 1010).
Spaced requests to avoid rate limiting.
"""

import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "probe_data.db"
REPORTS_DIR = Path(__file__).parent / "reports"

TIMEOUT = 10  # seconds per request
INTERVAL = 1.5  # seconds between requests

UA = "FreeLLM-Probe/1.0 (research; +https://github.com/slamdunktiger/free-llm-health)"

# Providers to probe: (provider_id, endpoint_type, url, extractor)
# extractor: lambda from response dict -> list of model strings or None
PROVIDERS = [
    {
        "id": "openrouter_free",
        "name": "OpenRouter (Free)",
        "base_url": "https://openrouter.ai/api/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", []) if m.get("id", "").endswith(":free")],
    },
    {
        "id": "groq_free",
        "name": "Groq (Free)",
        "base_url": "https://api.groq.com/openai/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "cerebras_free",
        "name": "Cerebras (Free)",
        "base_url": "https://api.cerebras.ai/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "nvidia_nim",
        "name": "NVIDIA NIM (Free)",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "github_models",
        "name": "GitHub Models",
        "base_url": "https://models.github.ai/inference/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "huggingface",
        "name": "HuggingFace (Router)",
        "base_url": "https://router.huggingface.co/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "mistral",
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "type": "models_list",
        "extract": lambda d: [m["id"] for m in d.get("data", [])],
    },
    {
        "id": "openrouter_chat",
        "name": "OpenRouter Chat Test",
        "base_url": "https://openrouter.ai/api/v1",
        "type": "chat_test",
        "test_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "extract": None,  # Just check if the endpoint responds
    },
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS probes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            status_code INTEGER,
            latency_ms REAL,
            models_found INTEGER DEFAULT 0,
            error TEXT,
            raw_response TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            probe_id INTEGER,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (probe_id) REFERENCES probes(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_probes_time ON probes(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_probes_provider ON probes(provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_models_time ON models(timestamp)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------


def fetch_json(url: str, timeout: int = TIMEOUT):
    """Fetch JSON from URL, return (status_code, parsed_dict_or_None, error_string)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return resp.status, data, None
    except urllib.error.HTTPError as e:
        return e.code, None, str(e)
    except urllib.error.URLError as e:
        return 0, None, str(e.reason)
    except TimeoutError:
        return 0, None, "TIMEOUT"
    except Exception as e:
        return 0, None, str(e)


# ---------------------------------------------------------------------------
# Probe Execution
# ---------------------------------------------------------------------------


def probe_provider(provider: dict) -> dict:
    """Probe a single provider, return result dict."""
    ts = datetime.now(timezone.utc).isoformat()
    result = {
        "timestamp": ts,
        "provider_id": provider["id"],
        "status_code": 0,
        "latency_ms": 0,
        "models_found": 0,
        "error": None,
        "models": [],
    }

    start = time.monotonic()

    if provider["type"] == "models_list":
        url = f"{provider['base_url']}/models"
        status, data, err = fetch_json(url)
        result["status_code"] = status
        result["error"] = err

        if data and provider.get("extract"):
            try:
                models = provider["extract"](data)
                result["models"] = models
                result["models_found"] = len(models)
            except Exception as e:
                result["error"] = f"extract error: {e}"

    elif provider["type"] == "chat_test":
        # Lightweight chat test - just check if the endpoint responds
        # We expect 401 (no auth) or 200 (if somehow allowed)
        url = f"{provider['base_url']}/models"
        status, data, err = fetch_json(url)
        result["status_code"] = status
        result["error"] = err

        if data and status == 200:
            result["models"] = [provider.get("test_model", "test")]
            result["models_found"] = 1 if result["models"] else 0

    result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
    return result


def run_all_probes():
    """Run probes for all providers."""
    conn = init_db()
    results = []

    for provider in PROVIDERS:
        print(f"  Probing {provider['name']}...", end=" ", flush=True)
        result = probe_provider(provider)
        results.append(result)

        # Store in DB
        cur = conn.execute(
            """INSERT INTO probes (timestamp, provider_id, status_code, latency_ms, models_found, error, raw_response)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                result["timestamp"],
                result["provider_id"],
                result["status_code"],
                result["latency_ms"],
                result["models_found"],
                result["error"],
                json.dumps(result["models"]) if result["models"] else None,
            ),
        )
        probe_id = cur.lastrowid

        for model_id in result["models"]:
            conn.execute(
                "INSERT INTO models (probe_id, provider_id, model_id, timestamp) VALUES (?, ?, ?, ?)",
                (probe_id, result["provider_id"], model_id, result["timestamp"]),
            )

        conn.commit()
        status = f"✓ {result['status_code']} ({result['latency_ms']}ms, {result['models_found']} models)"
        if result["error"]:
            status += f" [err: {result['error'][:60]}]"
        print(status)

        time.sleep(INTERVAL)

    conn.close()
    return results


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------


def generate_current_json():
    """Generate current.json — the authoritative live model list."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get the most recent probe for each provider
    rows = conn.execute("""
        SELECT p.*, p.raw_response
        FROM probes p
        INNER JOIN (
            SELECT provider_id, MAX(timestamp) as max_ts
            FROM probes
            GROUP BY provider_id
        ) latest ON p.provider_id = latest.provider_id AND p.timestamp = latest.max_ts
        ORDER BY p.provider_id
    """).fetchall()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": [],
        "summary": {"total_providers": 0, "live_providers": 0, "total_models": 0},
    }

    for row in rows:
        provider_data = {
            "provider_id": row["provider_id"],
            "status_code": row["status_code"],
            "latency_ms": row["latency_ms"],
            "models_found": row["models_found"],
            "last_probe": row["timestamp"],
            "models": json.loads(row["raw_response"]) if row["raw_response"] else [],
            "is_live": row["status_code"] in (200, 429),
            "error": row["error"],
        }
        report["providers"].append(provider_data)
        report["summary"]["total_providers"] += 1
        if provider_data["is_live"]:
            report["summary"]["live_providers"] += 1
        report["summary"]["total_models"] += row["models_found"]

    conn.close()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_DIR / "current.json", "w") as f:
        json.dump(report, f, indent=2)

    return report


def generate_daily_report():
    """Generate daily markdown report."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get last 24h of data
    now = datetime.now(timezone.utc)
    day_ago = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Summary stats
    stats = conn.execute("""
        SELECT provider_id,
               COUNT(*) as probe_count,
               SUM(CASE WHEN status_code IN (200, 429) THEN 1 ELSE 0 END) as success_count,
               AVG(latency_ms) as avg_latency,
               MAX(models_found) as peak_models
        FROM probes
        WHERE timestamp >= ?
        GROUP BY provider_id
    """, (day_ago.isoformat(),)).fetchall()

    # All models discovered today
    models = conn.execute("""
        SELECT DISTINCT provider_id, model_id
        FROM models
        WHERE timestamp >= ?
        ORDER BY provider_id, model_id
    """, (day_ago.isoformat(),)).fetchall()

    conn.close()

    # Build markdown
    date_str = now.strftime("%Y-%m-%d")
    lines = [
        f"# Free LLM Health Report — {date_str}",
        "",
        f"*Generated: {now.isoformat()}*",
        "",
        "## Summary",
        "",
        f"| Providers Live | Total Models |",
        f"|---|---|",
        f"| {sum(1 for s in stats if s['success_count'] > 0)} | {len(models)} |",
        "",
        "## Provider Status",
        "",
        "| Provider | Status | Probes | Success % | Avg Latency | Peak Models |",
        "|---|---|---|---|---|---|",
    ]

    for s in stats:
        status = "🟢" if s["success_count"] > 0 else "🔴"
        success_pct = (s["success_count"] / s["probe_count"] * 100) if s["probe_count"] > 0 else 0
        lines.append(
            f"| {status} {s['provider_id']} | {'live' if s['success_count'] > 0 else 'down'} | "
            f"{s['probe_count']} | {success_pct:.0f}% | {s['avg_latency']:.0f}ms | {s['peak_models']} |"
        )

    lines.extend(["", "## Models Discovered", ""])
    current_provider = None
    for m in models:
        if m["provider_id"] != current_provider:
            current_provider = m["provider_id"]
            lines.append(f"### {current_provider}")
            lines.append("")
        lines.append(f"- `{m['model_id']}`")
    lines.append("")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{date_str}.md"
    with open(report_path, "w") as f:
        f.write("\n".join(lines))

    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        print("Generating daily report...")
        report_path = generate_daily_report()
        print(f"Report written to: {report_path}")
        current = generate_current_json()
        print(f"Current JSON: {current['summary']}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--current":
        print("Regenerating current.json...")
        current = generate_current_json()
        print(json.dumps(current["summary"], indent=2))
        return

    print(f"=== Free LLM Health Probe ===")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"Providers: {len(PROVIDERS)}")
    print()

    results = run_all_probes()

    print()
    print("=== Summary ===")
    for r in results:
        status = "✓" if r["status_code"] in (200, 429) else "✗"
        print(f"  {status} {r['provider_id']}: {r['status_code']} ({r['latency_ms']}ms, {r['models_found']} models)")

    print()
    print("Generating current.json...")
    current = generate_current_json()
    print(f"  Live providers: {current['summary']['live_providers']}/{current['summary']['total_providers']}")
    print(f"  Total models: {current['summary']['total_models']}")


if __name__ == "__main__":
    main()
