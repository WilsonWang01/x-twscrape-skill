#!/usr/bin/env python3
"""凭据导入、单进程抓取、轮询及不暴露凭据的本地状态查询。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import fcntl
from getpass import getpass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


SCRIPTS = Path(__file__).resolve().parent


def read_accounts(db: Path) -> list[dict]:
    if not db.is_file():
        return []
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        # 只读取允许公开的列，不读 cookies、password、headers、proxy 或自由文本错误。
        return [dict(row) for row in conn.execute("SELECT username, active, locks, stats FROM accounts")]


@contextmanager
def pipeline_lock(db: Path, output: Path):
    paths = {db.resolve().with_suffix(".collector.lock"), output.resolve() / ".pipeline.lock"}
    with ExitStack() as stack:
        for path in sorted(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open("a+", encoding="utf-8"))
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("已有任务使用相同账号库或输出目录，请等待当前任务完成。") from None
        yield


async def init_account(args):
    with pipeline_lock(args.db, args.output):
        if any(row["username"] == args.username for row in read_accounts(args.db)):
            raise RuntimeError("该账号标识已存在。导入已停止，没有修改其 Cookie、限流锁或统计。")
        if args.cookies_file:
            if args.cookies_file.stat().st_mode & 0o077:
                raise RuntimeError("Cookie 文件应仅允许本人读写，请先 chmod 600 该文件。")
            cookies = args.cookies_file.read_text(encoding="utf-8").strip()
        else:
            cookies = getpass("粘贴 X Cookie（隐藏输入，仅在本机保存）：").strip()
        from twscrape.utils import parse_cookies
        from twscrape import API
        parsed = parse_cookies(cookies)
        if not parsed.get("auth_token") or not parsed.get("ct0"):
            raise RuntimeError("缺少 auth_token 或 ct0；请在本机检查 Cookie 文件。")
        previous_umask = os.umask(0o077)
        try:
            args.db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await API(str(args.db)).pool.add_account(
                args.username, "_", "_", "_", cookies=cookies
            )
            args.db.chmod(0o600)
        finally:
            os.umask(previous_umask)
    print("登录态已导入本地账号库；未执行联网抓取。")


def run_once(args, run_number=1):
    with pipeline_lock(args.db, args.output):
        if not args.db.is_file():
            raise RuntimeError("账号库不存在，请先执行 init-account。")
        modes = list(args.modes)
        if args.replies_every and run_number % args.replies_every == 0 and "replies" not in modes:
            modes.append("replies")
        cmd = [sys.executable, str(SCRIPTS / "collect_x_tweets_90d.py"),
               "--source", "twscrape", "--twscrape-db", str(args.db),
               "--output-dir", str(args.output), "--force", "--no-repair-jsonl",
               "--handles", *args.handles, "--twscrape-modes", *modes]
        if args.days:
            cmd += ["--days", str(args.days)]
        else:
            cmd += ["--lookback-minutes", str(args.lookback_minutes or 20),
                    "--sleep-every-tweets", "50", "--sleep-between-batches-min", "5",
                    "--sleep-between-batches-max", "15", "--sleep-between-modes-min", "15",
                    "--sleep-between-modes-max", "45", "--sleep-between-users-min", "5",
                    "--sleep-between-users-max", "20", "--old-tweet-stop-count", "20",
                    "--min-scanned-before-old-stop", "20"]
        if args.query_suffix:
            cmd += ["--query-suffix", args.query_suffix]
        scrape_code = subprocess.run(cmd, check=False).returncode
        attachment_code = None
        if args.attachments and (args.output / "tweets.jsonl").is_file():
            attachment_code = subprocess.run([
                sys.executable, str(SCRIPTS / "download_x_attachments.py"),
                "--tweets", str(args.output / "tweets.jsonl"),
                "--output-dir", str(args.output / "attachments"),
                "--skip-existing-manifest", "--retries", "4", "--timeout", "90",
                "--max-mb", "80", "--sleep-min", "0.5", "--sleep-max", "2",
            ], check=False).returncode
        state = {"finished_at_utc": datetime.now(timezone.utc).isoformat(),
                 "scrape_exit_code": scrape_code, "attachment_exit_code": attachment_code,
                 "ok": scrape_code == 0 and attachment_code in (0, None),
                 "modes": modes, "run_number": run_number}
        temporary = args.output / "last_run.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output / "last_run.json")
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0 if state["ok"] else 1


def status(args):
    counts = {}
    for relative in ("tweets.jsonl", "duplicate_tweets.jsonl", "errors.jsonl",
                     "attachments/attachment_manifest.jsonl", "attachments/attachment_errors.jsonl"):
        path = args.output / relative
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                counts[relative] = sum(1 for line in stream if line.strip())
        else:
            counts[relative] = 0
    state = args.output / "last_run.json"
    print(json.dumps({"本地记录行数": counts, "账号状态": read_accounts(args.db),
                      "上轮结果": json.loads(state.read_text(encoding="utf-8")) if state.is_file() else None},
                     ensure_ascii=False, indent=2))


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init-account", "status", "run", "watch"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--db", type=Path, default=Path("data/twscrape/accounts.db"))
        sub.add_argument("--output", type=Path, default=Path("data/x_archive"))
        if name == "init-account":
            sub.add_argument("--username", required=True, help="本地账号标识")
            sub.add_argument("--cookies-file", type=Path)
        if name in ("run", "watch"):
            sub.add_argument("--handles", nargs="+", required=True)
            sub.add_argument("--modes", nargs="+", choices=["timeline", "replies", "media", "search"], default=["timeline"])
            window = sub.add_mutually_exclusive_group()
            window.add_argument("--lookback-minutes", type=positive)
            if name == "run":
                window.add_argument("--days", type=positive)
            sub.add_argument("--attachments", action="store_true")
            sub.add_argument("--query-suffix", default="")
            sub.add_argument("--replies-every", type=positive, default=None)
        if name == "watch":
            sub.add_argument("--interval-seconds", type=positive, default=600)
    args = parser.parse_args()
    try:
        if args.command == "init-account":
            asyncio.run(init_account(args))
        elif args.command == "status":
            status(args)
        elif args.command == "run":
            return run_once(args)
        else:
            args.days = None
            number = 0
            while True:
                number += 1
                run_once(args, number)
                print(f"本轮结束，等待 {args.interval_seconds} 秒后继续。", flush=True)
                time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print("任务已停止。")
        return 130
    except Exception as exc:
        if args.command == "init-account":
            # SDK 异常可能携带输入内容，凭据导入时只输出错误类型。
            print(f"凭据导入未完成（{type(exc).__name__}），请本机检查格式、权限或重复账号。", file=sys.stderr)
        else:
            print(f"任务未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
