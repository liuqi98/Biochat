"""Run with python -m biochat.papers scan|updates."""

from __future__ import annotations

import argparse
import json
import sys

from biochat.papers.watcher import read_updates, scan_papers


def main(argv=None):
    parser = argparse.ArgumentParser(description="BioChat 论文监测器：增量发现与版本记录，不执行论文代码")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "updates"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--state-dir", help="持久化目录，默认 ~/.local/share/biochat/paper_monitor")
        cmd.add_argument("--config", help="自定义主题 JSON：name、topic_groups")
        cmd.add_argument("--json", action="store_true", help="输出完整 JSON（包含摘要）")
        if name == "scan":
            cmd.add_argument("--sources", nargs="+", choices=("europepmc", "biorxiv"), default=["europepmc", "biorxiv"])
            cmd.add_argument("--lookback-days", type=int, default=7)
            cmd.add_argument("--overlap-days", type=int, default=2)
            cmd.add_argument("--max-pages", type=int, default=20, help="每来源本次最多分页数；未结束则下次继续")
        else:
            cmd.add_argument("--after-id", type=int, default=0)
            cmd.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    common = {"state_dir": args.state_dir, "config_path": args.config}
    try:
        if args.command == "scan":
            def progress(event):
                print(f"[{event['source']}] 第 {event['page']} 页 · {event['window_start']} → {event['window_end']}",
                      file=sys.stderr, flush=True)
            result = scan_papers(**common, sources=tuple(args.sources), lookback_days=args.lookback_days,
                                 overlap_days=args.overlap_days, max_pages=args.max_pages, on_progress=progress)
        else:
            result = read_updates(**common, after_id=args.after_id, limit=args.limit)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"paper-monitor: {exc}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.command == "scan":
            print(f"状态: {result['status']} · 新增/更新计数: {result.get('counts', {})}")
            for source, status in result.get("sources", {}).items():
                print(f"  {source}: {status['status']}, 扫描 {status['records_scanned']} 条，匹配 {status['matched_records']} 条")
                if status.get("error"):
                    print(f"  错误: {status['error']}")
            print(f"报告: {result.get('report_path', '')}")
        for event in result.get("changes", result.get("events", [])):
            paper = event["paper"]
            print(f"[{event['event_id']} · {event['kind']}] {paper['title']}\n  {paper['url']}")
        if args.command == "updates":
            print(f"next_after_id: {result['next_after_id']}")
    return 2 if result.get("status") in ("partial", "busy") else 0


if __name__ == "__main__":
    raise SystemExit(main())
