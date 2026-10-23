#!/usr/bin/env python3
"""Download media and PDF/link attachments referenced by collected X records."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


IMAGE_TYPES = {"photo", "animated_gif"}
VIDEO_TYPES = {"video"}
PDF_RE = re.compile(r"\.pdf(?:$|[?#])", re.IGNORECASE)


@dataclass(frozen=True)
class Attachment:
    tweet_id: str
    handle: str
    kind: str
    url: str
    source_field: str
    media_key: str | None = None
    content_type_hint: str | None = None


def ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


def safe_ext(url: str, content_type: str | None, fallback: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return fallback


def best_video_variant(media: dict[str, Any]) -> str | None:
    variants = media.get("variants") or []
    candidates = []
    for variant in variants:
        url = variant.get("url")
        if not url:
            continue
        bitrate = variant.get("bit_rate") or variant.get("bitrate") or 0
        candidates.append((int(bitrate or 0), url))
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def original_twitter_image_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc not in {"pbs.twimg.com", "pbs.twimg.com"}:
        return url
    if not parsed.path.startswith("/media/"):
        return url

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "orig"
    return urlunparse(parsed._replace(query=urlencode(query)))


def alternate_twitter_image_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.netloc != "pbs.twimg.com" or not parsed.path.startswith("/media/"):
        return []

    alternates: list[str] = []
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for size in ("large", "4096x4096", ""):
        next_query = dict(query)
        if size:
            next_query["name"] = size
            alternate = urlunparse(parsed._replace(query=urlencode(next_query)))
        else:
            alternate = urlunparse(parsed._replace(query=""))
        if alternate != url and alternate not in alternates:
            alternates.append(alternate)
    return alternates


def media_items(media: Any) -> list[dict[str, Any]]:
    if isinstance(media, list):
        return [item for item in media if isinstance(item, dict)]
    if not isinstance(media, dict):
        return []

    items: list[dict[str, Any]] = []
    for photo in media.get("photos") or []:
        if isinstance(photo, dict):
            items.append({"type": "photo", **photo})
    for video in media.get("videos") or []:
        if isinstance(video, dict):
            items.append({"type": "video", **video})
    for animated in media.get("animated") or []:
        if isinstance(animated, dict):
            items.append({"type": "animated_gif", **animated})
    return items


def iter_attachments(tweets_path: Path, include_videos: bool) -> list[Attachment]:
    attachments: list[Attachment] = []
    skipped_bad_lines = 0
    with tweets_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped_bad_lines += 1
                continue
            tweet = record.get("tweet", {})
            tweet_id = str(tweet.get("id_str") or tweet.get("id") or "")
            handle = str(record.get("handle") or tweet.get("user", {}).get("username") or "unknown")
            if not tweet_id:
                continue

            for media in media_items(tweet.get("media")):
                media_type = str(media.get("type") or "").lower()
                url = media.get("url") or media.get("preview_image_url")
                kind = "image"
                if media_type in VIDEO_TYPES:
                    if not include_videos:
                        continue
                    url = best_video_variant(media) or url
                    kind = "video"
                elif media_type not in IMAGE_TYPES and not url:
                    continue
                if url:
                    if kind == "image":
                        url = original_twitter_image_url(str(url))
                    attachments.append(
                        Attachment(
                            tweet_id=tweet_id,
                            handle=handle,
                            kind=kind,
                            url=str(url),
                            source_field="tweet.media",
                            media_key=media.get("media_key"),
                        )
                    )

            for link in tweet.get("links") or []:
                url = link.get("expanded_url") or link.get("url")
                if not url:
                    continue
                url = str(url)
                if PDF_RE.search(url):
                    attachments.append(
                        Attachment(
                            tweet_id=tweet_id,
                            handle=handle,
                            kind="pdf",
                            url=url,
                            source_field="tweet.links",
                            content_type_hint="application/pdf",
                        )
                    )

    deduped: dict[tuple[str, str, str], Attachment] = {}
    for attachment in attachments:
        deduped[(attachment.tweet_id, attachment.kind, attachment.url)] = attachment
    if skipped_bad_lines:
        print(f"Skipped {skipped_bad_lines} invalid JSONL lines in {tweets_path}")
    return list(deduped.values())


def target_path(output_dir: Path, attachment: Attachment, content_type: str | None) -> Path:
    digest = hashlib.sha256(attachment.url.encode("utf-8")).hexdigest()[:16]
    if attachment.kind == "pdf":
        subdir = "pdfs"
        fallback = ".pdf"
    elif attachment.kind == "video":
        subdir = "videos"
        fallback = ".mp4"
    else:
        subdir = "images"
        fallback = ".jpg"
    ext = safe_ext(attachment.url, content_type or attachment.content_type_hint, fallback)
    name = f"{attachment.handle}_{attachment.tweet_id}_{digest}{ext}"
    return output_dir / subdir / name


def fetch_once(url: str, *, max_bytes: int, timeout: int) -> tuple[bytes, str | None]:
    headers = {
        "User-Agent": "investor-watcher-attachment-downloader/0.1",
        "Accept": "*/*",
    }
    request = Request(url, headers=headers, method="GET")
    context = ssl_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        content_type = response.headers.get("content-type")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 64)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError(f"download exceeded max bytes: {max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks), content_type


def fetch(url: str, *, max_bytes: int, timeout: int, retries: int, retry_sleep: float) -> tuple[bytes, str | None]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fetch_once(url, max_bytes=max_bytes, timeout=timeout)
        except (HTTPError, URLError, RuntimeError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(retry_sleep * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def fetch_with_alternates(url: str, *, max_bytes: int, timeout: int, retries: int, retry_sleep: float) -> tuple[bytes, str | None, str]:
    candidates = [url, *alternate_twitter_image_urls(url)]
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            body, content_type = fetch(
                candidate,
                max_bytes=max_bytes,
                timeout=timeout,
                retries=retries,
                retry_sleep=retry_sleep,
            )
            return body, content_type, candidate
        except (HTTPError, URLError, RuntimeError, TimeoutError, OSError) as exc:
            last_exc = exc
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500:
                break
    assert last_exc is not None
    raise last_exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download X media and PDF attachments.")
    parser.add_argument("--tweets", type=Path, required=True, help="Path to tweets.jsonl")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/attachments"),
        help="Directory for downloaded attachments.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Manifest JSONL path. Defaults to <output-dir>/attachment_manifest.jsonl.",
    )
    parser.add_argument("--include-videos", action="store_true", help="Download videos too.")
    parser.add_argument("--dry-run", action="store_true", help="Write manifest candidates only.")
    parser.add_argument("--max-mb", type=int, default=50, help="Max bytes per attachment in MB.")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=3.0)
    parser.add_argument("--sleep-min", type=float, default=2.0)
    parser.add_argument("--sleep-max", type=float, default=8.0)
    parser.add_argument(
        "--skip-existing-manifest",
        action="store_true",
        help="Do not append manifest rows for files that already exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest or args.output_dir / "attachment_manifest.jsonl"
    errors_path = args.output_dir / "attachment_errors.jsonl"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    attachments = iter_attachments(args.tweets, args.include_videos)
    max_bytes = args.max_mb * 1024 * 1024
    downloaded = 0
    skipped_existing = 0
    errors = 0

    for index, attachment in enumerate(attachments, start=1):
        content_type = attachment.content_type_hint
        path = target_path(args.output_dir, attachment, content_type)
        record = {
            "tweet_id": attachment.tweet_id,
            "handle": attachment.handle,
            "kind": attachment.kind,
            "source_field": attachment.source_field,
            "media_key": attachment.media_key,
            "url": attachment.url,
            "local_path": str(path),
            "dry_run": args.dry_run,
        }

        if args.dry_run:
            append_jsonl(manifest_path, record)
            continue

        if path.exists() and path.stat().st_size > 0:
            record["status"] = "exists"
            record["bytes"] = path.stat().st_size
            if not args.skip_existing_manifest:
                append_jsonl(manifest_path, record)
            skipped_existing += 1
            continue

        network_attempted = False
        try:
            network_attempted = True
            body, content_type, final_url = fetch_with_alternates(
                attachment.url,
                max_bytes=max_bytes,
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
            path = target_path(args.output_dir, attachment, content_type)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_bytes(body)
            os.replace(tmp_path, path)
            record["status"] = "downloaded"
            record["content_type"] = content_type
            record["bytes"] = len(body)
            record["local_path"] = str(path)
            if final_url != attachment.url:
                record["download_url"] = final_url
            append_jsonl(manifest_path, record)
            downloaded += 1
        except (HTTPError, URLError, RuntimeError, TimeoutError, OSError) as exc:
            errors += 1
            append_jsonl(
                errors_path,
                {
                    **record,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )

        if network_attempted and index < len(attachments) and args.sleep_max > 0:
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    print(
        f"attachments={len(attachments)} downloaded={downloaded} "
        f"exists={skipped_existing} errors={errors} manifest={manifest_path}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
