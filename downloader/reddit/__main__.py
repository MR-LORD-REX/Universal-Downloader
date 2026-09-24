"""Command line interface: ``python -m downloader.reddit <url>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .client import RedditClient
from .config import RedditConfig
from .exceptions import RedditError
from .models import ProgressEvent


def _progress(event: ProgressEvent) -> None:
    sys.stderr.write(f"\r{event}")
    sys.stderr.flush()
    if event.phase.value in ("done",):
        sys.stderr.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m downloader.reddit",
        description="Inspect and download reddit posts.",
    )
    parser.add_argument("urls", nargs="+", help="post urls, share links, ids or media urls")
    parser.add_argument("-d", "--dest", default=None, help="download directory")
    parser.add_argument("-q", "--quality", default="best", help="best|worst|720|1080p")
    parser.add_argument("-p", "--pattern", default=None, help="filename pattern, e.g. {author}_{id}_{index}.{ext}")
    parser.add_argument("--info", action="store_true", help="metadata only, never download")
    parser.add_argument("--json", action="store_true", help="print metadata as json")
    parser.add_argument("--describe", action="store_true", help="print every format")
    parser.add_argument("--album-dir", action="store_true", help="put multi item posts in their own folder")
    parser.add_argument("--no-sizes", action="store_true", help="skip content-length probes")
    parser.add_argument("--no-expand", action="store_true", help="skip DASH/HLS manifest parsing")
    parser.add_argument("--previews", action="store_true", help="include reddit preview renditions")
    parser.add_argument("--providers", default=None, help="comma separated provider order")
    return parser


async def run(args: argparse.Namespace) -> int:
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None
    config = RedditConfig.from_env()
    overrides = {}
    if providers:
        overrides["providers"] = providers
    config = config.with_overrides(**overrides) if overrides else config
    exit_code = 0
    async with RedditClient(config) as client:
        for url in args.urls:
            try:
                metadata = await client.get_metadata(
                    url,
                    include_sizes=not args.no_sizes,
                    expand_formats=not args.no_expand,
                    include_previews=args.previews,
                    progress=_progress,
                )
            except RedditError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = 1
                continue

            if args.json:
                print(json.dumps(metadata.to_dict(), indent=2, ensure_ascii=False))
            elif args.describe:
                print(metadata.describe())
            else:
                print(metadata.summary())

            if args.info or args.dest is None:
                continue
            try:
                result = await client.download(
                    metadata,
                    quality=args.quality,
                    target="disk",
                    dest=args.dest,
                    pattern=args.pattern,
                    album_dir=args.album_dir,
                    progress=_progress,
                )
            except RedditError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            print(result.summary())
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())