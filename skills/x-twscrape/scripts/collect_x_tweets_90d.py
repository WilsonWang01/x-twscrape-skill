#!/usr/bin/env python3
"""Collect raw X/Twitter posts for explicitly supplied handles.

This collector is designed for conservative, resumable collection:

- tweet ids are deduplicated across modes and reruns;
- completed handle/mode pairs are checkpointed;
- timeline, replies, and search can be combined for better coverage;
- each run emits a completeness report so partial data is explicit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import ssl
import sys
import time
from collections import Counter, defaultdict
from contextlib import aclosing
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.x.com/2"


USER_FIELDS = ",".join(
    [
        "created_at",
        "description",
        "entities",
        "id",
        "location",
        "name",
        "profile_banner_url",
        "profile_image_url",
        "protected",
        "public_metrics",
        "url",
        "username",
        "verified",
        "verified_type",
    ]
)

TWEET_FIELDS = ",".join(
    [
        "article",
        "attachments",
        "author_id",
        "card_uri",
        "conversation_id",
        "created_at",
        "display_text_range",
        "edit_controls",
        "entities",
        "geo",
        "id",
        "in_reply_to_user_id",
        "lang",
        "note_tweet",
        "possibly_sensitive",
        "public_metrics",
        "referenced_tweets",
        "reply_settings",
        "source",
        "text",
        "withheld",
    ]
)

MEDIA_FIELDS = ",".join(
    [
        "alt_text",
        "duration_ms",
        "height",
        "media_key",
        "preview_image_url",
        "public_metrics",
        "type",
        "url",
        "variants",
        "width",
    ]
)

EXPANSIONS = ",".join(
    [
        "attachments.media_keys",
        "author_id",
        "entities.mentions.username",
        "geo.place_id",
        "in_reply_to_user_id",
        "referenced_tweets.id",
        "referenced_tweets.id.author_id",
    ]
)

TWSCRAPE_MODES = ("timeline", "replies", "media", "search")


class CollectorError(RuntimeError):
    """Raised when a collector cannot continue."""


def selected_users(handles: list[str] | None) -> list[dict[str, str]]:
    if not handles:
        raise CollectorError("请使用 --handles 明确指定需要抓取的 X 账号。")
    users = []
    seen = set()
    for value in handles:
        handle = value.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise CollectorError("账号格式无效：请提供 X handle，而非网址。")
        if handle.lower() not in seen:
            seen.add(handle.lower())
            users.append({"name": handle, "handle": handle, "primary_source": "twscrape"})
    return users


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=json_default, separators=(",", ":"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )


def append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json_dumps(record) + "\n").encode("utf-8")
    with path.open("ab") as fh:
        fh.write(data)
        fh.flush()


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        append_jsonl_record(path, record)
        count += 1
    return count


def repair_jsonl(path: Path, quarantine_path: Path | None = None) -> dict[str, int]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return {"valid": 0, "invalid": 0}

    valid: list[str] = []
    invalid: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                invalid.append(line.rstrip("\n"))
                continue
            valid.append(line.rstrip("\n"))

    if invalid:
        path.write_text("\n".join(valid) + ("\n" if valid else ""), encoding="utf-8")
        if quarantine_path:
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            with quarantine_path.open("a", encoding="utf-8") as fh:
                for line in invalid:
                    fh.write(line)
                    fh.write("\n")

    return {"valid": len(valid), "invalid": len(invalid)}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def tweet_created_at(tweet: dict[str, Any]) -> datetime | None:
    for key in ("date", "created_at"):
        parsed = parse_datetime(tweet.get(key))
        if parsed:
            return parsed
    return None


def tweet_id(tweet: dict[str, Any]) -> str | None:
    raw_id = tweet.get("id_str") or tweet.get("id")
    if raw_id is None:
        return None
    return str(raw_id)


def raw_user_dict(raw_user: Any) -> dict[str, Any]:
    if isinstance(raw_user, dict):
        return raw_user
    if hasattr(raw_user, "dict"):
        return raw_user.dict()
    if hasattr(raw_user, "__dict__"):
        return dict(raw_user.__dict__)
    return {}


def raw_user_id(raw_user: Any) -> int | str | None:
    if isinstance(raw_user, dict):
        value = raw_user.get("id") or raw_user.get("id_str") or raw_user.get("rest_id")
    else:
        value = getattr(raw_user, "id", None) or getattr(raw_user, "id_str", None)
    if value is None:
        return None
    text = str(value)
    return int(text) if text.isdigit() else text


def cached_user_for_handle(store: "OutputStore", handle: str) -> Any | None:
    wanted = handle.lower()
    for cached_handle, cached_user in store.users_by_handle.items():
        if cached_handle.lower() != wanted:
            continue
        if isinstance(cached_user, dict) and "raw_user" in cached_user:
            cached_user = cached_user["raw_user"]
        if raw_user_id(cached_user) is not None:
            return cached_user
    return None


def patch_twscrape_xclid() -> None:
    """Use upstream signing and surface aborted requests instead of empty results."""
    from importlib.metadata import version

    try:
        from twscrape import queue_client
    except ImportError as exc:
        raise CollectorError("请先安装 requirements-scrape.txt（开源包使用 requirements.txt）。") from exc

    installed = re.match(r"(\d+)\.(\d+)\.(\d+)", version("twscrape"))
    if not installed or tuple(map(int, installed.groups())) < (0, 20, 1):
        raise CollectorError("X 网页签名格式已更新，请升级到 twscrape>=0.20.1；旧签名补丁已停用。")
    os.environ.setdefault("TWS_TELEMETRY", "0")

    original_get = queue_client.QueueClient.get
    if getattr(original_get, "_iw_checked", False):
        return

    async def checked_get(client: Any, *args: Any, **kwargs: Any) -> Any:
        response = await original_get(client, *args, **kwargs)
        if response is None:
            raise CollectorError(
                f"X 请求中止（端点 {client.queue}）；可能是签名、登录或网络错误，不能视为没有新帖。"
            )
        payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload:
            raise CollectorError(f"X 响应缺少 data（端点 {client.queue}），请检查接口或登录状态。")
        if payload.get("errors") and not any((payload.get("data") or {}).values()):
            codes = [item.get("code") for item in payload["errors"] if isinstance(item, dict)]
            raise CollectorError(f"X 返回错误（端点 {client.queue}，错误码 {codes}）。")
        return response

    checked_get._iw_checked = True
    queue_client.QueueClient.get = checked_get


def build_window(args: argparse.Namespace) -> tuple[date, date, datetime, datetime]:
    if args.lookback_minutes:
        until_dt = datetime.now(timezone.utc)
        since_dt = until_dt - timedelta(minutes=args.lookback_minutes)
        return since_dt.date(), until_dt.date() + timedelta(days=1), since_dt, until_dt

    today = datetime.now().date()
    since_day = args.since or today - timedelta(days=args.days)
    until_day = args.until or today + timedelta(days=1)

    # X operators and X API timestamps are UTC. The report records the exact
    # UTC boundaries so the 8-hour Asia/Shanghai offset is visible.
    since_dt = datetime.combine(since_day, datetime_time.min, tzinfo=timezone.utc)
    until_dt = datetime.combine(until_day, datetime_time.min, tzinfo=timezone.utc)
    return since_day, until_day, since_dt, until_dt


class OutputStore:
    def __init__(self, output_dir: Path, *, repair: bool) -> None:
        self.output_dir = output_dir
        self.tweets_path = output_dir / "tweets.jsonl"
        self.duplicates_path = output_dir / "duplicate_tweets.jsonl"
        self.raw_pages_path = output_dir / "raw_pages.jsonl"
        self.errors_path = output_dir / "errors.jsonl"
        self.users_path = output_dir / "users.json"
        self.checkpoints_path = output_dir / "checkpoints.json"
        self.completeness_path = output_dir / "completeness_report.json"
        self.corrupt_path = output_dir / "corrupt_jsonl_lines.log"

        output_dir.mkdir(parents=True, exist_ok=True)
        for path in [
            self.tweets_path,
            self.duplicates_path,
            self.raw_pages_path,
            self.errors_path,
        ]:
            if repair:
                repair_jsonl(path, self.corrupt_path)
            else:
                path.touch(exist_ok=True)

        self.seen_tweet_ids: set[str] = set()
        self.users_by_handle: dict[str, dict[str, Any]] = {}
        self.checkpoints: dict[str, Any] = read_json(self.checkpoints_path, {})
        self.new_tweets = 0
        self.duplicates = 0
        self.load_existing()

    def load_existing(self) -> None:
        with self.tweets_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                tid = tweet_id(record.get("tweet", {}))
                if tid:
                    self.seen_tweet_ids.add(tid)
                raw_user = record.get("tweet", {}).get("user")
                handle = record.get("handle")
                if handle and raw_user and handle not in self.users_by_handle:
                    self.users_by_handle[handle] = raw_user

        existing_users = read_json(self.users_path, [])
        if isinstance(existing_users, list):
            for item in existing_users:
                source = item.get("source") if isinstance(item, dict) else None
                raw_user = item.get("raw_user") if isinstance(item, dict) else None
                handle = source.get("handle") if isinstance(source, dict) else None
                if handle and raw_user:
                    self.users_by_handle.setdefault(handle, raw_user)
                elif isinstance(item, dict):
                    handle = item.get("username") or item.get("screen_name")
                    if handle:
                        self.users_by_handle.setdefault(str(handle), item)

    def checkpoint_key(self, handle: str, collector: str, mode: str) -> str:
        return f"{collector}:{handle.lower()}:{mode}"

    def checkpoint(self, handle: str, collector: str, mode: str) -> dict[str, Any]:
        return self.checkpoints.get(self.checkpoint_key(handle, collector, mode), {})

    def write_checkpoint(
        self,
        handle: str,
        collector: str,
        mode: str,
        payload: dict[str, Any],
    ) -> None:
        key = self.checkpoint_key(handle, collector, mode)
        existing = self.checkpoints.get(key, {})
        existing.update(payload)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.checkpoints[key] = existing
        write_json(self.checkpoints_path, self.checkpoints)

    def record_user(self, handle: str, source: dict[str, str], collector: str, raw_user: Any) -> None:
        if raw_user:
            self.users_by_handle[handle] = {
                "source": source,
                "collector": collector,
                "raw_user": raw_user,
            }
            self.flush_users()

    def flush_users(self) -> None:
        write_json(self.users_path, list(self.users_by_handle.values()))

    def record_error(self, error: dict[str, Any]) -> None:
        error.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        append_jsonl_record(self.errors_path, error)

    def record_page(self, page: dict[str, Any]) -> None:
        append_jsonl_record(self.raw_pages_path, page)

    def record_tweet(
        self,
        *,
        collector: str,
        source: dict[str, str],
        handle: str,
        mode: str,
        query: str | None,
        tweet: dict[str, Any],
        includes: dict[str, Any] | None = None,
    ) -> bool:
        tid = tweet_id(tweet)
        if not tid:
            self.record_error(
                {
                    "collector": collector,
                    "handle": handle,
                    "mode": mode,
                    "error": "Tweet without id was skipped.",
                    "tweet": tweet,
                }
            )
            return False

        record = {
            "collector": collector,
            "source": source,
            "handle": handle,
            "mode": mode,
            "query": query,
            "tweet": tweet,
        }
        if includes:
            record["includes"] = includes

        if tid in self.seen_tweet_ids:
            duplicate = {
                "duplicate_seen_at": datetime.now(timezone.utc).isoformat(),
                "tweet_id": tid,
                "handle": handle,
                "mode": mode,
                "collector": collector,
                "query": query,
            }
            append_jsonl_record(self.duplicates_path, duplicate)
            self.duplicates += 1
            return False

        self.seen_tweet_ids.add(tid)
        append_jsonl_record(self.tweets_path, record)
        self.new_tweets += 1
        return True

    def build_completeness_report(
        self,
        *,
        watched_users: list[dict[str, str]],
        modes: list[str],
        since_dt: datetime,
        until_dt: datetime,
        status: str,
        notes: list[str],
    ) -> dict[str, Any]:
        handle_counts: Counter[str] = Counter()
        handle_mode_counts: Counter[tuple[str, str]] = Counter()
        dates_by_handle: dict[str, list[str]] = defaultdict(list)
        media_count = 0
        link_count = 0
        quote_count = 0
        reply_count = 0

        with self.tweets_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handle = record.get("handle") or "unknown"
                mode = record.get("mode") or "unknown"
                tweet = record.get("tweet", {})
                created = tweet_created_at(tweet)
                if created:
                    dates_by_handle[handle].append(created.isoformat())
                handle_counts[handle] += 1
                handle_mode_counts[(handle, mode)] += 1
                media_count += len(tweet.get("media") or [])
                link_count += len(tweet.get("links") or [])
                if tweet.get("quotedTweet"):
                    quote_count += 1
                if tweet.get("inReplyToTweetId") or tweet.get("referenced_tweets"):
                    reply_count += 1

        per_handle: dict[str, Any] = {}
        for source in watched_users:
            handle = source["handle"]
            checkpoints = {
                mode: self.checkpoint(handle, "twscrape", mode) for mode in modes
            }
            dates = dates_by_handle.get(handle, [])
            per_handle[handle] = {
                "records": handle_counts.get(handle, 0),
                "date_min": min(dates) if dates else None,
                "date_max": max(dates) if dates else None,
                "mode_counts": {
                    mode: handle_mode_counts.get((handle, mode), 0) for mode in modes
                },
                "checkpoints": checkpoints,
                "has_any_data": handle_counts.get(handle, 0) > 0,
                "window_start_reached": bool(dates and min(dates) <= since_dt.isoformat()),
            }

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "window": {
                "start_time_utc": since_dt.isoformat(),
                "end_time_utc": until_dt.isoformat(),
            },
            "totals": {
                "unique_tweet_records": sum(handle_counts.values()),
                "duplicates_skipped_this_process": self.duplicates,
                "media_items": media_count,
                "links": link_count,
                "quoted_tweets": quote_count,
                "reply_like_records": reply_count,
            },
            "requested_modes": modes,
            "per_handle": per_handle,
            "notes": notes,
        }
        write_json(self.completeness_path, report)
        return report


def request_json(
    path: str,
    params: dict[str, Any],
    bearer_token: str,
    *,
    retries: int = 2,
) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    query = urlencode(params, doseq=False)
    if query:
        url = f"{url}?{query}"

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": "investor-watcher-x-collector/0.2",
    }

    context = ssl_context()
    for attempt in range(retries + 1):
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=60, context=context) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = exc.headers.get("retry-after")
            if exc.code == 429 and attempt < retries:
                wait_seconds = int(retry_after or "60")
                print(f"Rate limited on {path}; sleeping {wait_seconds}s", file=sys.stderr)
                time.sleep(wait_seconds)
                continue
            raise CollectorError(f"X API {exc.code} for {path}: {detail}") from exc
        except URLError as exc:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise CollectorError(f"Network error for {path}: {exc}") from exc

    raise CollectorError(f"X API request failed for {path}")


def in_window(created_at: datetime | None, since_dt: datetime, until_dt: datetime) -> bool:
    return bool(created_at and since_dt <= created_at < until_dt)


async def maybe_sleep(min_seconds: float, max_seconds: float, label: str) -> None:
    if max_seconds <= 0:
        return
    pause = random.uniform(min_seconds, max_seconds)
    print(f"{label}; sleeping {pause:.1f}s")
    await asyncio.sleep(pause)


async def collect_twscrape_mode(
    *,
    api: Any,
    store: OutputStore,
    source: dict[str, str],
    raw_user: Any,
    mode: str,
    since_day: date,
    until_day: date,
    since_dt: datetime,
    until_dt: datetime,
    args: argparse.Namespace,
) -> dict[str, Any]:
    handle = source["handle"]
    if args.lookback_minutes:
        query = f"from:{handle} since_time:{int(since_dt.timestamp())} until_time:{int(until_dt.timestamp())}"
    else:
        query = f"from:{handle} since:{since_day.isoformat()} until:{until_day.isoformat()}"
    if args.query_suffix:
        query = f"{query} {args.query_suffix}"

    checkpoint = store.checkpoint(handle, "twscrape", mode)
    if checkpoint.get("completed") and not args.force:
        print(f"@{handle}/{mode}: already checkpointed, skipping")
        return {"mode": mode, "status": "skipped_checkpoint", "new": 0, "scanned": 0}

    user_id = raw_user_id(raw_user)
    if user_id is None:
        raise CollectorError(f"Cannot collect @{handle}/{mode}: missing cached user id")

    if mode == "timeline":
        tweet_stream = api.user_tweets(user_id, limit=-1)
    elif mode == "replies":
        tweet_stream = api.user_tweets_and_replies(user_id, limit=-1)
    elif mode == "media":
        tweet_stream = api.user_media(user_id, limit=-1)
    elif mode == "search":
        tweet_stream = api.search(query, limit=-1)
    else:
        raise CollectorError(f"Unsupported twscrape mode: {mode}")

    started_at = datetime.now(timezone.utc).isoformat()
    scanned = 0
    new_count = 0
    duplicate_count = 0
    out_of_window_old = 0
    out_of_window_new = 0
    oldest_seen: str | None = None
    newest_seen: str | None = None
    stop_reason = "stream_end"

    async with aclosing(tweet_stream):
        async for tweet in tweet_stream:
            scanned += 1
            raw = tweet.dict()
            created = tweet_created_at(raw)
            if created:
                created_iso = created.isoformat()
                oldest_seen = min(oldest_seen, created_iso) if oldest_seen else created_iso
                newest_seen = max(newest_seen, created_iso) if newest_seen else created_iso

            if args.max_scan_per_mode > 0 and scanned > args.max_scan_per_mode:
                stop_reason = "max_scan_per_mode"
                break

            if created and created >= until_dt:
                out_of_window_new += 1
                continue

            if created and created < since_dt:
                out_of_window_old += 1
                if (
                    not args.disable_old_stop
                    and scanned >= args.min_scanned_before_old_stop
                    and out_of_window_old >= args.old_tweet_stop_count
                ):
                    stop_reason = "old_window_stop"
                    break
                continue

            if not in_window(created, since_dt, until_dt):
                continue

            added = store.record_tweet(
                collector="twscrape",
                source=source,
                handle=handle,
                mode=mode,
                query=query if mode == "search" else None,
                tweet=raw,
            )
            if added:
                new_count += 1
            else:
                duplicate_count += 1

            if args.limit_per_user_mode > 0 and new_count >= args.limit_per_user_mode:
                stop_reason = "limit_per_user_mode"
                break

            if (
                args.sleep_every_tweets > 0
                and scanned % args.sleep_every_tweets == 0
                and args.sleep_between_batches_max > 0
            ):
                await maybe_sleep(
                    args.sleep_between_batches_min,
                    args.sleep_between_batches_max,
                    f"@{handle}/{mode}: scanned {scanned}, new {new_count}",
                )

    result = {
        "collector": "twscrape",
        "handle": handle,
        "mode": mode,
        "completed": stop_reason in {"stream_end", "old_window_stop"},
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scanned": scanned,
        "new": new_count,
        "duplicates": duplicate_count,
        "out_of_window_old": out_of_window_old,
        "out_of_window_new": out_of_window_new,
        "oldest_seen": oldest_seen,
        "newest_seen": newest_seen,
        "stop_reason": stop_reason,
    }
    store.write_checkpoint(handle, "twscrape", mode, result)
    store.record_page(
        {
            "collector": "twscrape",
            "handle": handle,
            "mode": mode,
            "query": query if mode == "search" else None,
            "summary": result,
        }
    )
    print(
        f"@{handle}/{mode}: scanned={scanned} new={new_count} "
        f"dupes={duplicate_count} stop={stop_reason}"
    )
    return result


async def collect_with_twscrape(
    args: argparse.Namespace,
    store: OutputStore,
    watched_users: list[dict[str, str]],
    since_day: date,
    until_day: date,
    since_dt: datetime,
    until_dt: datetime,
) -> dict[str, Any]:
    try:
        patch_twscrape_xclid()
        from twscrape import API
    except (ImportError, CollectorError) as exc:
        error = {"collector": "twscrape", "phase": "runtime", "error_type": type(exc).__name__, "error": str(exc)}
        store.record_error(error)
        return {"collector": "twscrape", "status": "blocked", "tweets": 0, "errors": [error]}

    api = API(str(args.twscrape_db), wait_timeout=60)
    stats = await api.pool.stats()
    if stats.get("active", 0) < 1:
        error = {
            "collector": "twscrape",
            "error": "No active twscrape accounts. Add and login accounts before rerunning.",
            "account_stats": stats,
        }
        store.record_error(error)
        return {
            "collector": "twscrape",
            "status": "blocked",
            "account_stats": stats,
            "tweets": 0,
            "errors": [error],
        }

    modes = args.twscrape_modes
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for user_index, source in enumerate(watched_users):
        handle = source["handle"]
        cached_user = cached_user_for_handle(store, handle)
        try:
            raw_user = await api.user_by_login(handle)
            if raw_user is None:
                raise CollectorError(f"Could not resolve @{handle}")
            store.record_user(handle, source, "twscrape", raw_user_dict(raw_user))
        except Exception as exc:  # noqa: BLE001 - continue to other users.
            if cached_user is None:
                error = {
                    "collector": "twscrape",
                    "handle": handle,
                    "phase": "resolve_user",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                store.record_error(error)
                errors.append(error)
                continue
            raw_user = cached_user
            print(
                f"@{handle}: user_by_login failed ({type(exc).__name__}); "
                f"using cached user id {raw_user_id(raw_user)}",
                file=sys.stderr,
            )

        for mode_index, mode in enumerate(modes):
            try:
                result = await collect_twscrape_mode(
                    api=api,
                    store=store,
                    source=source,
                    raw_user=raw_user,
                    mode=mode,
                    since_day=since_day,
                    until_day=until_day,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    args=args,
                )
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - keep collecting.
                store.write_checkpoint(handle, "twscrape", mode, {"completed": False, "status": "failed"})
                error = {
                    "collector": "twscrape",
                    "handle": handle,
                    "mode": mode,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                store.record_error(error)
                errors.append(error)
                print(f"@{handle}/{mode}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

            if mode_index < len(modes) - 1:
                await maybe_sleep(
                    args.sleep_between_modes_min,
                    args.sleep_between_modes_max,
                    f"@{handle}: finished {mode}",
                )

        if user_index < len(watched_users) - 1:
            await maybe_sleep(
                args.sleep_between_users_min,
                args.sleep_between_users_max,
                f"@{handle}: finished all requested modes",
            )

    status = "ok" if not errors else "partial"
    return {
        "collector": "twscrape",
        "status": status,
        "account_stats": stats,
        "tweets": store.new_tweets,
        "duplicates": store.duplicates,
        "mode_results": results,
        "errors": errors,
    }


def collect_with_x_api(
    args: argparse.Namespace,
    store: OutputStore,
    watched_users: list[dict[str, str]],
    _since_day: date,
    _until_day: date,
    since_dt: datetime,
    until_dt: datetime,
) -> dict[str, Any]:
    bearer_token = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer_token:
        error = {
            "collector": "xapi",
            "error": "Missing X_BEARER_TOKEN or TWITTER_BEARER_TOKEN.",
        }
        store.record_error(error)
        return {
            "collector": "xapi",
            "status": "blocked",
            "tweets": 0,
            "errors": [error],
        }

    handles = ",".join(user["handle"] for user in watched_users)
    profile_response = request_json(
        "/users/by",
        {"usernames": handles, "user.fields": USER_FIELDS},
        bearer_token,
    )
    profiles = {
        user["username"].lower(): user for user in profile_response.get("data", [])
    }

    per_user: dict[str, int] = {}
    errors: list[dict[str, Any]] = []
    for source in watched_users:
        handle = source["handle"]
        checkpoint = store.checkpoint(handle, "xapi", "timeline")
        if checkpoint.get("completed") and not args.force:
            print(f"@{handle}/xapi: already checkpointed, skipping")
            continue

        profile = profiles.get(handle.lower())
        if not profile:
            error = {
                "collector": "xapi",
                "handle": handle,
                "error": "Profile was not returned by X API.",
            }
            store.record_error(error)
            errors.append(error)
            per_user[handle] = 0
            continue

        store.record_user(handle, source, "xapi", profile)
        fetched = 0
        duplicate_count = 0
        pages = 0
        pagination_token = None
        while True:
            params: dict[str, Any] = {
                "max_results": "100",
                "start_time": since_dt.isoformat().replace("+00:00", "Z"),
                "end_time": until_dt.isoformat().replace("+00:00", "Z"),
                "tweet.fields": TWEET_FIELDS,
                "user.fields": USER_FIELDS,
                "media.fields": MEDIA_FIELDS,
                "expansions": EXPANSIONS,
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            try:
                page = request_json(f"/users/{profile['id']}/tweets", params, bearer_token)
            except CollectorError as exc:
                error = {
                    "collector": "xapi",
                    "handle": handle,
                    "mode": "timeline",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                store.record_error(error)
                errors.append(error)
                break

            pages += 1
            store.record_page(
                {
                    "collector": "xapi",
                    "source": source,
                    "handle": handle,
                    "profile": profile,
                    "response": page,
                }
            )

            for tweet in page.get("data", []):
                if store.record_tweet(
                    collector="xapi",
                    source=source,
                    handle=handle,
                    mode="timeline",
                    query=None,
                    tweet=tweet,
                    includes=page.get("includes", {}),
                ):
                    fetched += 1
                else:
                    duplicate_count += 1

            if args.limit_per_user_mode > 0 and fetched >= args.limit_per_user_mode:
                break

            pagination_token = page.get("meta", {}).get("next_token")
            if not pagination_token:
                break

        per_user[handle] = fetched
        store.write_checkpoint(
            handle,
            "xapi",
            "timeline",
            {
                "collector": "xapi",
                "handle": handle,
                "mode": "timeline",
                "completed": not pagination_token,
                "pages": pages,
                "new": fetched,
                "duplicates": duplicate_count,
                "next_token": pagination_token,
            },
        )
        print(f"@{handle}/xapi: new={fetched} dupes={duplicate_count} pages={pages}")

    return {
        "collector": "xapi",
        "status": "ok" if not errors else "partial",
        "tweets": sum(per_user.values()),
        "duplicates": store.duplicates,
        "per_user": per_user,
        "errors": errors,
    }


def parse_modes(values: list[str]) -> list[str]:
    modes: list[str] = []
    for raw in values:
        for item in raw.split(","):
            mode = item.strip()
            if not mode:
                continue
            if mode not in TWSCRAPE_MODES:
                raise argparse.ArgumentTypeError(
                    f"Unsupported mode {mode!r}. Use one of: {', '.join(TWSCRAPE_MODES)}"
                )
            if mode not in modes:
                modes.append(mode)
    return modes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取明确指定的 X 账号，保存正文、媒体元数据与进度。"
    )
    parser.add_argument(
        "--source",
        choices=["twscrape"],
        default="twscrape",
        help="开源版仅启用 twscrape，不自动调用官方 X API。",
    )
    parser.add_argument("--days", type=int, default=90, help="Lookback window in days.")
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=None,
        help="Rolling lookback window in minutes. Overrides --days/--since/--until.",
    )
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        help="Exclusive YYYY-MM-DD end date. Defaults to tomorrow.",
    )
    parser.add_argument(
        "--limit-per-user-mode",
        type=int,
        default=-1,
        help="Maximum new tweets per user per mode. -1 means no explicit new-tweet limit.",
    )
    parser.add_argument(
        "--limit-per-user",
        type=int,
        default=None,
        help="Deprecated alias for --limit-per-user-mode.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to data/x_raw_tweets_90d_<timestamp>.",
    )
    parser.add_argument(
        "--twscrape-db",
        type=Path,
        default=Path("data/twscrape/accounts.db"),
        help="twscrape account database path.",
    )
    parser.add_argument(
        "--twscrape-modes",
        nargs="+",
        default=["timeline"],
        help="Modes to combine: timeline, replies, media, search. Comma or space separated.",
    )
    parser.add_argument(
        "--twscrape-mode",
        choices=list(TWSCRAPE_MODES),
        default=None,
        help="Deprecated single-mode alias for --twscrape-modes.",
    )
    parser.add_argument(
        "--query-suffix",
        default="",
        help="Optional extra X search operators for twscrape search mode.",
    )
    parser.add_argument(
        "--handles",
        nargs="+",
        default=None,
        help="明确指定账号列表，支持带或不带 @。",
    )
    parser.add_argument("--force", action="store_true", help="Ignore completed checkpoints.")
    parser.add_argument(
        "--no-repair-jsonl",
        action="store_true",
        default=True,
        help="默认保留损坏行并报错，不自动修复历史数据。",
    )
    parser.add_argument(
        "--max-scan-per-mode",
        type=int,
        default=-1,
        help="Maximum tweets to scan per handle/mode. -1 means no explicit scan cap.",
    )
    parser.add_argument(
        "--disable-old-stop",
        action="store_true",
        help="Do not stop timeline scans after many older-than-window tweets.",
    )
    parser.add_argument(
        "--old-tweet-stop-count",
        type=int,
        default=500,
        help="Stop after this many older-than-window tweets once min scan threshold is met.",
    )
    parser.add_argument(
        "--min-scanned-before-old-stop",
        type=int,
        default=500,
        help="Minimum scanned tweets before old-window stop can trigger.",
    )
    parser.add_argument(
        "--sleep-every-tweets",
        type=int,
        default=100,
        help="For twscrape, sleep after every N scanned tweets. 0 disables.",
    )
    parser.add_argument(
        "--sleep-between-batches-min",
        type=float,
        default=20.0,
        help="Minimum seconds to sleep between tweet batches for twscrape.",
    )
    parser.add_argument(
        "--sleep-between-batches-max",
        type=float,
        default=45.0,
        help="Maximum seconds to sleep between tweet batches for twscrape.",
    )
    parser.add_argument(
        "--sleep-between-modes-min",
        type=float,
        default=60.0,
        help="Minimum seconds to sleep between modes for one user.",
    )
    parser.add_argument(
        "--sleep-between-modes-max",
        type=float,
        default=150.0,
        help="Maximum seconds to sleep between modes for one user.",
    )
    parser.add_argument(
        "--sleep-between-users-min",
        type=float,
        default=120.0,
        help="Minimum seconds to sleep between users for twscrape.",
    )
    parser.add_argument(
        "--sleep-between-users-max",
        type=float,
        default=300.0,
        help="Maximum seconds to sleep between users for twscrape.",
    )
    args = parser.parse_args()

    if args.source != "twscrape":
        parser.error("开源包仅启用 twscrape；官方 X API 可能产生费用，需另行明确授权接入。")
    if args.days <= 0 or (args.lookback_minutes is not None and args.lookback_minutes <= 0):
        parser.error("时间窗口必须大于 0。")

    if args.limit_per_user is not None:
        args.limit_per_user_mode = args.limit_per_user
    if args.twscrape_mode:
        args.twscrape_modes = [args.twscrape_mode]
    else:
        args.twscrape_modes = parse_modes(args.twscrape_modes)
    return args


async def main_async() -> int:
    args = parse_args()
    load_dotenv(Path(".env"))

    since_day, until_day, since_dt, until_dt = build_window(args)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("data") / f"x_raw_tweets_90d_{timestamp}"
    watched_users = selected_users(args.handles)
    store = OutputStore(output_dir, repair=not args.no_repair_jsonl)

    metadata: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "requested_source": args.source,
        "window": {
            "days": args.days,
            "since_date": since_day.isoformat(),
            "until_date_exclusive": until_day.isoformat(),
            "start_time_utc": since_dt.isoformat(),
            "end_time_utc": until_dt.isoformat(),
        },
        "watched_users": watched_users,
        "selected_handles": args.handles,
        "twscrape_modes": args.twscrape_modes,
        "rate_limit_controls": {
            "sleep_between_users_min": args.sleep_between_users_min,
            "sleep_between_users_max": args.sleep_between_users_max,
            "sleep_between_modes_min": args.sleep_between_modes_min,
            "sleep_between_modes_max": args.sleep_between_modes_max,
            "sleep_every_tweets": args.sleep_every_tweets,
            "sleep_between_batches_min": args.sleep_between_batches_min,
            "sleep_between_batches_max": args.sleep_between_batches_max,
        },
        "output_files": {
            "tweets_jsonl": str(store.tweets_path),
            "duplicate_tweets_jsonl": str(store.duplicates_path),
            "raw_pages_jsonl": str(store.raw_pages_path),
            "users_json": str(store.users_path),
            "errors_jsonl": str(store.errors_path),
            "checkpoints_json": str(store.checkpoints_path),
            "completeness_report_json": str(store.completeness_path),
            "metadata_json": str(output_dir / "run_metadata.json"),
        },
        "attempts": [],
    }

    collectors = ["twscrape", "xapi"] if args.source == "auto" else [args.source]
    final_result: dict[str, Any] | None = None
    for collector in collectors:
        print(f"Running collector: {collector}")
        if collector == "twscrape":
            result = await collect_with_twscrape(
                args, store, watched_users, since_day, until_day, since_dt, until_dt
            )
        elif collector == "xapi":
            result = collect_with_x_api(
                args, store, watched_users, since_day, until_day, since_dt, until_dt
            )
        else:
            raise AssertionError(f"Unknown collector: {collector}")

        metadata["attempts"].append(result)
        final_result = result
        if result.get("tweets", 0) > 0 or args.source != "auto":
            break

    metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
    metadata["final_result"] = final_result
    write_json(output_dir / "run_metadata.json", metadata)

    notes = [
        "Completeness requires all requested handles and modes to have completed checkpoints.",
        "Attachment files are not downloaded by this script; run download_x_attachments.py.",
        "External article pages and indirect PDF links are not crawled by this package.",
    ]
    status = final_result.get("status", "unknown") if final_result else "unknown"
    report = store.build_completeness_report(
        watched_users=watched_users,
        modes=args.twscrape_modes if args.source != "xapi" else ["timeline"],
        since_dt=since_dt,
        until_dt=until_dt,
        status=status,
        notes=notes,
    )

    print(f"Wrote output under {output_dir}")
    print(
        "Completeness: "
        f"{report['totals']['unique_tweet_records']} unique tweets, "
        f"{report['totals']['duplicates_skipped_this_process']} duplicates skipped"
    )
    mode_results = final_result.get("mode_results", []) if final_result else []
    if (
        final_result
        and final_result.get("status") == "ok"
        and len(mode_results) == len(watched_users) * len(args.twscrape_modes)
        and all(item.get("completed") or item.get("status") == "skipped_checkpoint" for item in mode_results)
    ):
        return 0

    print("抓取被阻断或仅部分完成，请检查 errors.jsonl 和 run_metadata.json。", file=sys.stderr)
    return 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
