#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SOURCE_URL = (
    "https://raw.githubusercontent.com/arinhadebackend/"
    "arinhadebackend.github.io/2026-preview/results-preview.json"
)

ROOT = Path(__file__).resolve().parents[1]
RAW_RESULTS_PATH = ROOT / "data" / "raw" / "results-preview.json"
LANGUAGE_CACHE_PATH = ROOT / "data" / "raw" / "repo-languages-cache.json"
PROCESSED_PATH = ROOT / "data" / "processed" / "dashboard-data.json"
DASHBOARD_DATA_PATH = ROOT / "dashboard" / "data" / "dashboard-data.json"

CACHE_TTL_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 30
MAX_API_RETRIES = 3
USER_AGENT = "rinha-dashboard-updater/1.0"


@dataclass
class LanguageResolution:
    language: str
    status: str
    languages: dict[str, int]
    bytes_total: int
    from_cache: bool


def ensure_dirs() -> None:
    RAW_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LANGUAGE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower().replace(",", ".")
        if text.endswith("%"):
            maybe = parse_float(text[:-1])
            if maybe is not None:
                return maybe / 100.0
            return None
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def quantile(values: list[float], q: float) -> float | None:
    clean = sorted(v for v in values if v is not None and not math.isnan(v))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    if q <= 0:
        return clean[0]
    if q >= 1:
        return clean[-1]
    idx = (len(clean) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return clean[lo]
    frac = idx - lo
    return clean[lo] + (clean[hi] - clean[lo]) * frac


def mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return None
    return sum(clean) / len(clean)


def weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    clean = [(value, weight) for value, weight in pairs if value is not None and weight > 0]
    if not clean:
        return None
    weighted_sum = sum(value * weight for value, weight in clean)
    total_weight = sum(weight for _, weight in clean)
    if total_weight <= 0:
        return None
    return weighted_sum / total_weight


def parse_github_repo(repo_url: str | None) -> tuple[str, str] | None:
    if not repo_url:
        return None
    try:
        parsed = urlparse(repo_url)
    except ValueError:
        return None
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def load_cache() -> dict[str, Any]:
    if not LANGUAGE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(LANGUAGE_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    payload = {"updated_at": utc_now().isoformat(), "repos": cache}
    LANGUAGE_CACHE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_cache(payload: dict[str, Any]) -> dict[str, Any]:
    if "repos" in payload and isinstance(payload["repos"], dict):
        return payload["repos"]
    return payload if isinstance(payload, dict) else {}


def cache_fresh(entry: dict[str, Any], now: datetime) -> bool:
    fetched_at_text = entry.get("fetched_at")
    if not isinstance(fetched_at_text, str):
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_text)
    except ValueError:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    ttl_days = 1 if entry.get("language") == "Unknown" else CACHE_TTL_DAYS
    return fetched_at >= now - timedelta(days=ttl_days)


def resolve_primary_language(owner: str, repo: str) -> str | None:
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".language"],
        capture_output=True,
        text=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or value.lower() == "null":
        return None
    return value



def resolve_language(
    repo_slug: str,
    cache: dict[str, Any],
    stats: dict[str, int],
    now: datetime,
) -> LanguageResolution:
    cached = cache.get(repo_slug)
    if isinstance(cached, dict) and cache_fresh(cached, now):
        stats["cache_hits"] += 1
        return LanguageResolution(
            language=cached.get("language", "Unknown"),
            status=cached.get("status", "ok"),
            languages=cached.get("languages", {}),
            bytes_total=int(cached.get("bytes_total", 0) or 0),
            from_cache=True,
        )

    owner, repo = repo_slug.split("/", maxsplit=1)

    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/languages", "--jq", "."],
                capture_output=True,
                text=True,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if result.returncode == 0:
                payload = json.loads(result.stdout)
                if not isinstance(payload, dict):
                    payload = {}
                languages = {str(k): int(v) for k, v in payload.items() if isinstance(v, int)}
                bytes_total = sum(languages.values())
                language = max(languages, key=languages.get) if languages else "Unknown"
                status_text = "ok"
                if not languages:
                    primary_language = resolve_primary_language(owner, repo)
                    if primary_language:
                        language = primary_language
                        status_text = "fallback_primary"
                    else:
                        status_text = "empty"

                cache[repo_slug] = {
                    "language": language,
                    "status": status_text,
                    "languages": languages,
                    "bytes_total": bytes_total,
                    "fetched_at": now.isoformat(),
                }
                stats["api_calls"] += 1
                if status_text != "ok":
                    stats[f"api_{status_text}"] += 1
                return LanguageResolution(
                    language=language,
                    status=status_text,
                    languages=languages,
                    bytes_total=bytes_total,
                    from_cache=False,
                )
            else:
                error_msg = result.stderr.lower()
                if "not found" in error_msg or "404" in error_msg:
                    status_text = "not_found"
                elif "unauthorized" in error_msg or "403" in error_msg or "401" in error_msg:
                    status_text = "rate_limited"
                elif "private" in error_msg:
                    status_text = "private"
                else:
                    status_text = "api_error"

                if status_text == "rate_limited" and attempt < MAX_API_RETRIES:
                    time.sleep(2**attempt)
                    continue

                cache[repo_slug] = {
                    "language": "Unknown",
                    "status": status_text,
                    "languages": {},
                    "bytes_total": 0,
                    "fetched_at": now.isoformat(),
                }
                stats[f"api_{status_text}"] += 1
                return LanguageResolution(
                    language="Unknown",
                    status=status_text,
                    languages={},
                    bytes_total=0,
                    from_cache=False,
                )

        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            if attempt < MAX_API_RETRIES:
                time.sleep(2**attempt)
                continue
            cache[repo_slug] = {
                "language": "Unknown",
                "status": "api_error",
                "languages": {},
                "bytes_total": 0,
                "fetched_at": now.isoformat(),
            }
            stats["api_error"] += 1
            return LanguageResolution(
                language="Unknown", status="api_error", languages={}, bytes_total=0, from_cache=False
            )

    return LanguageResolution(language="Unknown", status="unknown", languages={}, bytes_total=0, from_cache=False)


def download_source(url: str) -> dict[str, Any]:
    request = Request(url=url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = response.read().decode("utf-8")
        parsed = json.loads(data)
    RAW_RESULTS_PATH.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
    return parsed


def normalize_records(
    source: dict[str, Any],
    cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    now = utc_now()
    stats = defaultdict(int)
    records: list[dict[str, Any]] = []
    unique_repo_slugs: set[str] = set()

    for participant, projects in source.items():
        if not isinstance(projects, dict):
            continue

        for project_name, entry in projects.items():
            if not isinstance(entry, dict):
                continue
            stats["projects_seen"] += 1
            scoring = entry.get("scoring") if isinstance(entry.get("scoring"), dict) else {}
            breakdown = scoring.get("breakdown") if isinstance(scoring.get("breakdown"), dict) else {}
            raw = scoring.get("raw") if isinstance(scoring.get("raw"), dict) else {}
            expected = entry.get("expected") if isinstance(entry.get("expected"), dict) else {}

            p99_ms = parse_float(raw.get("p99_ms"))
            if p99_ms is None:
                p99_ms = parse_float(entry.get("p99"))

            final_score = parse_float(raw.get("final_score"))
            if final_score is None:
                final_score = parse_float(scoring.get("final_score"))

            failure_rate = parse_float(raw.get("failure_rate"))
            if failure_rate is None:
                failure_rate = parse_float(scoring.get("failure_rate"))

            error_rate = parse_float(raw.get("error_rate_epsilon"))
            if error_rate is None:
                error_rate = parse_float(scoring.get("error_rate_epsilon"))

            expected_total = parse_float(expected.get("total"))
            http_errors = int(parse_float(breakdown.get("http_errors")) or 0)
            false_pos = int(parse_float(breakdown.get("false_positive_detections")) or 0)
            false_neg = int(parse_float(breakdown.get("false_negative_detections")) or 0)
            true_pos = int(parse_float(breakdown.get("true_positive_detections")) or 0)
            true_neg = int(parse_float(breakdown.get("true_negative_detections")) or 0)

            repo_url = entry.get("repo_url")
            repo_info = parse_github_repo(repo_url)
            repo_slug = f"{repo_info[0]}/{repo_info[1]}" if repo_info else None
            language_result: LanguageResolution | None = None

            if repo_slug:
                unique_repo_slugs.add(repo_slug)
                language_result = resolve_language(repo_slug, cache, stats, now)
            else:
                stats["invalid_repo_url"] += 1

            if final_score is not None:
                stats["scored_projects"] += 1

            record = {
                "participant": participant,
                "project_name": project_name,
                "repo_url": repo_url,
                "repo_slug": repo_slug,
                "issue_url": entry.get("issue_url"),
                "timestamp": entry.get("timestamp"),
                "language": (language_result.language if language_result else "Unknown"),
                "language_status": (language_result.status if language_result else "invalid_url"),
                "p99_ms": p99_ms,
                "final_score": final_score,
                "failure_rate": failure_rate,
                "error_rate": error_rate,
                "expected_total": expected_total,
                "telemetry": {
                    "http_errors": http_errors,
                    "false_positive_detections": false_pos,
                    "false_negative_detections": false_neg,
                    "true_positive_detections": true_pos,
                    "true_negative_detections": true_neg,
                },
            }
            records.append(record)

    stats["participants"] = len(source)
    stats["unique_repositories"] = len(unique_repo_slugs)
    return records, dict(stats), {"generated_at": now.isoformat()}


def aggregate_language(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record.get("language") or "Unknown"].append(record)

    rankings: list[dict[str, Any]] = []
    for language, items in groups.items():
        scores = [r["final_score"] for r in items if r.get("final_score") is not None]
        p99s = [r["p99_ms"] for r in items if r.get("p99_ms") is not None]
        error_rates = [r["error_rate"] for r in items if r.get("error_rate") is not None]
        failure_rates = [r["failure_rate"] for r in items if r.get("failure_rate") is not None]

        error_weight_pairs = []
        failure_weight_pairs = []
        participants = {r["participant"] for r in items if r.get("participant")}
        repos = {r["repo_slug"] for r in items if r.get("repo_slug")}

        telemetry_totals = defaultdict(int)
        for item in items:
            weight = item.get("expected_total") or 0
            if item.get("error_rate") is not None and weight:
                error_weight_pairs.append((item["error_rate"], float(weight)))
            if item.get("failure_rate") is not None and weight:
                failure_weight_pairs.append((item["failure_rate"], float(weight)))
            telemetry = item.get("telemetry") or {}
            for key in (
                "http_errors",
                "false_positive_detections",
                "false_negative_detections",
                "true_positive_detections",
                "true_negative_detections",
            ):
                telemetry_totals[key] += int(telemetry.get(key, 0) or 0)

        average_score = mean(scores)
        rankings.append(
            {
                "language": language,
                "sample_count": len(items),
                "repository_count": len(repos),
                "participant_count": len(participants),
                "average_score": average_score,
                "mean_score": average_score,
                "median_score": quantile(scores, 0.5),
                "p99_score": quantile(scores, 0.99),
                "average_p99_ms": mean(p99s),
                "median_p99_ms": quantile(p99s, 0.5),
                "p99_p99_ms": quantile(p99s, 0.99),
                "average_error_rate": mean(error_rates),
                "average_failure_rate": mean(failure_rates),
                "weighted_error_rate": weighted_mean(error_weight_pairs),
                "weighted_failure_rate": weighted_mean(failure_weight_pairs),
                "telemetry_totals": dict(telemetry_totals),
            }
        )

    rankings.sort(
        key=lambda row: (
            row["average_score"] is not None,
            row["average_score"] if row["average_score"] is not None else float("-inf"),
            row["sample_count"],
        ),
        reverse=True,
    )
    return rankings


def compute_global_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r["final_score"] for r in records if r.get("final_score") is not None]
    p99s = [r["p99_ms"] for r in records if r.get("p99_ms") is not None]
    error_rates = [r["error_rate"] for r in records if r.get("error_rate") is not None]
    failure_rates = [r["failure_rate"] for r in records if r.get("failure_rate") is not None]
    weight_pairs_error = []
    weight_pairs_failure = []
    for record in records:
        weight = record.get("expected_total")
        if weight and record.get("error_rate") is not None:
            weight_pairs_error.append((record["error_rate"], float(weight)))
        if weight and record.get("failure_rate") is not None:
            weight_pairs_failure.append((record["failure_rate"], float(weight)))

    return {
        "record_count": len(records),
        "participants": len({r["participant"] for r in records if r.get("participant")}),
        "languages": len({r["language"] for r in records if r.get("language")}),
        "average_score": mean(scores),
        "mean_score": mean(scores),
        "median_score": quantile(scores, 0.5),
        "p99_score": quantile(scores, 0.99),
        "average_p99_ms": mean(p99s),
        "median_p99_ms": quantile(p99s, 0.5),
        "p99_p99_ms": quantile(p99s, 0.99),
        "average_error_rate": mean(error_rates),
        "average_failure_rate": mean(failure_rates),
        "weighted_error_rate": weighted_mean(weight_pairs_error),
        "weighted_failure_rate": weighted_mean(weight_pairs_failure),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def main() -> int:
    ensure_dirs()

    source = download_source(SOURCE_URL)
    cache_payload = load_cache()
    cache = parse_cache(cache_payload)

    records, processing_stats, context = normalize_records(source, cache)
    language_rankings = aggregate_language(records)
    global_stats = compute_global_stats(records)
    save_cache(cache)

    payload = {
        "metadata": {
            "generated_at": context["generated_at"],
            "source_url": SOURCE_URL,
            "schema_version": "1.0",
            "metrics_note": (
                "average_* are arithmetic means over repository entries; "
                "weighted_* rates are weighted by expected_total when available; "
                "p99_p99_ms is percentile across per-repository p99 values."
            ),
            "processing_stats": processing_stats,
        },
        "global_stats": global_stats,
        "filters": {
            "languages": sorted({r["language"] for r in records if r.get("language")}),
            "participants": sorted({r["participant"] for r in records if r.get("participant")}),
            "score_min": min((r["final_score"] for r in records if r.get("final_score") is not None), default=None),
            "score_max": max((r["final_score"] for r in records if r.get("final_score") is not None), default=None),
        },
        "language_rankings": language_rankings,
        "repositories": records,
    }

    write_json(PROCESSED_PATH, payload)
    write_json(DASHBOARD_DATA_PATH, payload)
    print(f"Wrote {PROCESSED_PATH}")
    print(f"Wrote {DASHBOARD_DATA_PATH}")
    print(f"Records: {len(records)} | Languages: {len(payload['filters']['languages'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
