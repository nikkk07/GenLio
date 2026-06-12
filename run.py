"""gelio CLI.

Usage:
    python run.py generate [--slides 9] [--dry-run]

``generate`` runs one daily pipeline: select an unused psychology concept,
build a Brief, write the carousel Content, validate everything, and (unless
``--dry-run``) persist artifacts to ``output/<id>/`` and a DRAFTED row in SQLite.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from gelio import configure_logging
from gelio.approval import ApprovalError, build_approval
from gelio.assets import AssetError, setup_assets
from gelio.compositor import CompositorError
from gelio.content_writer import ContentWriterError
from gelio.llm import LLMError
from gelio.pipeline import RenderResult, RunResult, build_pipeline, build_renderer
from gelio.publisher import PublishError
from gelio.schemas import PostState
from gelio.store import Store
from gelio.sync import build_sync
from gelio.topic_engine import TopicEngineError
from config.settings import load_settings

logger = logging.getLogger("gelio.cli")


def _print_result(result: RunResult) -> None:
    if result.dry_run:
        print("=== DRY RUN (nothing written) ===")
        print("\n--- Brief ---")
        print(json.dumps(result.brief.model_dump(mode="json"), indent=2, ensure_ascii=False))
        print("\n--- Content ---")
        print(json.dumps(result.content.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return

    if result.already_existed:
        print(f"Already drafted: {result.brief.id} (idempotent, nothing rewritten)")
        if result.brief_path:
            print(f"  brief:   {result.brief_path}")
        if result.content_path:
            print(f"  content: {result.content_path}")
        return

    print(f"Drafted: {result.brief.id}  ({result.brief.concept})")
    print(f"  brief:   {result.brief_path}")
    print(f"  content: {result.content_path}")
    print("  state:   DRAFTED")
    if result.render is not None:
        _print_render(result.render)


def _print_render(render: RenderResult) -> None:
    if render.skipped:
        print(f"  render:  skipped (slides already complete) -> {render.pdf_path}")
        return
    by_source = {s: render.sources.count(s) for s in sorted(set(render.sources))}
    print(
        f"  render:  {len(render.slide_paths)} slides {by_source} -> {render.pdf_path}"
    )


def cmd_generate(args: argparse.Namespace) -> int:
    pipeline, store = build_pipeline()
    try:
        # --approve implies render: we need composited slides to preview.
        render = args.render or args.approve
        result = pipeline.generate(
            slides=args.slides,
            dry_run=args.dry_run,
            render=render,
            force=args.force,
        )
        _print_result(result)

        if args.approve and not args.dry_run:
            settings = load_settings()
            approval = build_approval(settings, store, build_sync(settings))
            approval.send_preview(result.brief.id)
            print(f"  approval: preview sent to Telegram admin (AWAITING_APPROVAL)")
        return 0
    finally:
        store.close()


def cmd_send_approval(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        approval = build_approval(settings, store, build_sync(settings))
        approval.send_preview(args.id)
        print(f"Preview sent for {args.id} (AWAITING_APPROVAL)")
        return 0
    finally:
        store.close()


def cmd_schedule(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        from datetime import datetime, timezone

        from gelio.timeutil import ScheduleParseError, parse_schedule_input, utc_to_ist

        # Naive input is interpreted as IST; explicit Z/offset is respected.
        try:
            scheduled_time = parse_schedule_input(args.time)
        except (ScheduleParseError, ValueError):
            print('ERROR: Invalid time. Use IST "YYYY-MM-DD HH:MM" or ISO UTC like 2026-06-12T10:30:00Z')
            return 1

        record = store.get_post(args.id)
        if record is None:
            print(f"ERROR: Post {args.id} not found")
            return 1

        if record.state != PostState.APPROVED:
            print(f"ERROR: Post {args.id} is {record.state.value}, must be APPROVED to schedule")
            return 1

        updated = store.set_scheduled_time(args.id, scheduled_time)
        ist = utc_to_ist(scheduled_time).strftime("%Y-%m-%d %I:%M %p IST")
        print(f"Scheduled {args.id} for {scheduled_time} ({ist})")
        print(f"  Concept: {updated.concept}")
        print(f"  Current time: {datetime.now(timezone.utc).isoformat()}")
        return 0
    finally:
        store.close()


def _build_publish_service(settings, store):
    """Publish service with Telegram wired in when configured (LinkedIn +
    result broadcasts); without a token, LinkedIn reports as disabled."""
    from gelio.approval import TelegramClient
    from gelio.publisher import build_publish_service

    telegram = (
        TelegramClient(settings.telegram_bot_token) if settings.telegram_bot_token else None
    )
    return build_publish_service(settings, store, build_sync(settings), telegram=telegram)


def cmd_publish(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        service = _build_publish_service(settings, store)
        platforms = (
            [p.strip() for p in args.platforms.split(",") if p.strip()]
            if args.platforms
            else None
        )
        report = service.publish(args.id, platforms=platforms)
        print(f"Publish {args.id}:")
        print(f"  {report.summary()}")
        print(f"  state: {report.final_state.value if report.final_state else '?'}")
        failed = [r for r in report.results if not r.ok and not r.skipped]
        return 1 if failed else 0
    finally:
        store.close()


def cmd_publish_due(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        service = _build_publish_service(settings, store)
        reports = service.publish_due()
        if not reports:
            print("No approved posts due for publishing.")
            return 0
        for report in reports:
            print(f"Published {report.post_id}: {report.summary()}")
        return 0
    finally:
        store.close()


def cmd_list_scheduled(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        posts = store.get_posts_due_for_posting(args.before if hasattr(args, 'before') and args.before else None)
        if not posts:
            print("No posts scheduled for posting")
            return 0
        
        print(f"Posts scheduled for posting ({len(posts)}):")
        for p in posts:
            print(f"  {p.id}")
            print(f"    Concept: {p.concept}")
            print(f"    Scheduled: {p.scheduled_time}")
            print(f"    State: {p.state.value}")
        return 0
    finally:
        store.close()


def cmd_bot(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        approval = build_approval(settings, store, build_sync(settings))
        print("gelio bot running — long-polling Telegram. Ctrl-C to stop.")
        approval.run_bot()
        return 0
    finally:
        store.close()


def cmd_render(args: argparse.Namespace) -> int:
    renderer, store = build_renderer()
    try:
        result = renderer.render(args.id, force=args.force)
        print(f"Rendered: {args.id}")
        _print_render(result)
        return 0
    finally:
        store.close()


def cmd_setup_assets(args: argparse.Namespace) -> int:
    settings = load_settings()
    paths = setup_assets(settings.fonts_dir, force=args.force, with_deps=args.with_deps)
    print("Fonts ready:")
    for p in paths:
        print(f"  {p}")
    print("Chromium installed for Playwright rendering.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gelio", description="gelio content agent")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate one daily carousel concept")
    gen.add_argument(
        "--slides",
        type=int,
        default=9,
        help="number of slides (default: 9)",
    )
    gen.add_argument(
        "--dry-run",
        action="store_true",
        help="print Brief + Content without writing DB or files",
    )
    gen.add_argument(
        "--render",
        action="store_true",
        help="also run Phase 2: composite slides + build carousel.pdf",
    )
    gen.add_argument(
        "--force",
        action="store_true",
        help="re-render over existing slides instead of skipping",
    )
    gen.add_argument(
        "--approve",
        action="store_true",
        help="after rendering, send the post to Telegram for approval",
    )
    gen.set_defaults(func=cmd_generate)

    rnd = sub.add_parser("render", help="render visuals for an existing artifact dir")
    rnd.add_argument("id", help="post id, e.g. 2026-06-11-decision-fatigue")
    rnd.add_argument(
        "--force",
        action="store_true",
        help="re-render over existing slides instead of skipping",
    )
    rnd.set_defaults(func=cmd_render)

    setup = sub.add_parser(
        "setup-assets", help="download brand fonts + install Playwright Chromium"
    )
    setup.add_argument(
        "--force", action="store_true", help="re-download even if fonts exist"
    )
    setup.add_argument(
        "--with-deps",
        action="store_true",
        help="also install OS deps for Chromium (Linux/CI)",
    )
    setup.set_defaults(func=cmd_setup_assets)

    sa = sub.add_parser("send-approval", help="send an existing rendered post for approval")
    sa.add_argument("id", help="post id, e.g. 2026-06-11-decision-fatigue")
    sa.set_defaults(func=cmd_send_approval)

    bot = sub.add_parser("bot", help="long-poll Telegram and handle approval buttons")
    bot.set_defaults(func=cmd_bot)

    sched = sub.add_parser("schedule", help="schedule an approved post for a specific time")
    sched.add_argument("id", help="post id, e.g. 2026-06-11-decision-fatigue")
    sched.add_argument(
        "time",
        help='IST time "2026-06-13 18:00" (naive = IST) or explicit UTC 2026-06-12T10:30:00Z',
    )
    sched.set_defaults(func=cmd_schedule)

    pub = sub.add_parser("publish", help="publish one approved post now (Phase 4)")
    pub.add_argument("id", help="post id, e.g. 2026-06-11-decision-fatigue")
    pub.add_argument(
        "--platforms",
        default=None,
        help="comma-separated subset: x,instagram,linkedin (default: all enabled)",
    )
    pub.set_defaults(func=cmd_publish)

    pdue = sub.add_parser(
        "publish-due",
        help="publish APPROVED posts whose schedule is due or unset (cron-safe, idempotent)",
    )
    pdue.set_defaults(func=cmd_publish_due)

    lsched = sub.add_parser("list-scheduled", help="list posts scheduled for posting")
    lsched.add_argument("--before", help="show posts scheduled before this ISO timestamp", default=None)
    lsched.set_defaults(func=cmd_list_scheduled)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        LLMError,
        TopicEngineError,
        ContentWriterError,
        CompositorError,
        AssetError,
        ApprovalError,
        PublishError,
        FileNotFoundError,
    ) as exc:
        logger.error("run failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
