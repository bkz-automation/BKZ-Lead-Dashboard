"""Run the public lead-collection workflow with one command."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time

import lead_collector


class SummaryCollector(logging.Handler):
    """Capture the collector's existing end-of-run counters without changing it."""

    patterns = {
        "businesses_discovered": re.compile(r"^Candidates discovered: (\d+)$"),
        "rejected_leads": re.compile(r"^Leads rejected after full enrichment: (\d+)$"),
        "emails_found": re.compile(r"^Leads with email: (\d+)$"),
        "mobiles_found": re.compile(r"^Leads with mobile: (\d+)$"),
        "whatsapp_found": re.compile(r"^Leads with confirmed WhatsApp: (\d+)$"),
        "accepted_dry_run": re.compile(r"^Dry-run candidates \(not appended\): (\d+)$"),
        "accepted_live": re.compile(r"^Leads appended: (\d+)$"),
        "accepted_candidates": re.compile(r"^Run summary: .*leads found=(\d+),"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        for name, pattern in self.patterns.items():
            match = pattern.match(message)
            if match:
                self.values[name] = int(match.group(1))
                return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Agadir", choices=("Agadir", "Casablanca", "Marrakech"))
    parser.add_argument("--sector", default="restaurants", help="Business sector to collect")
    parser.add_argument("--target-leads", type=int, default=20)
    parser.add_argument("--max-searches", type=int, default=4)
    parser.add_argument("--write-sheets", action="store_true", help="Enable the collector's existing Google Sheets write step")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def collector_args(args: argparse.Namespace) -> list[str]:
    forwarded = [
        "--cities", args.city,
        "--sectors", args.sector,
        "--target-leads", str(args.target_leads),
        "--max-searches", str(args.max_searches),
    ]
    if not args.write_sheets:
        forwarded.append("--dry-run")
    if args.verbose:
        forwarded.append("--verbose")
    return forwarded


def configure_logging(verbose: bool) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root.addHandler(stream)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    return root


def print_summary(values: dict[str, int], runtime_seconds: float, dry_run: bool) -> None:
    accepted = values.get("accepted_dry_run" if dry_run else "accepted_live", 0)
    rejected = values.get("rejected_leads", 0)
    lines = (
        "Lead collection summary",
        f"Businesses discovered: {values.get('businesses_discovered', 0)}",
        f"Businesses enriched: {values.get('accepted_candidates', 0) + rejected}",
        f"Accepted leads: {accepted}",
        f"Rejected leads: {rejected}",
        f"Emails found: {values.get('emails_found', 0)}",
        f"Mobiles found: {values.get('mobiles_found', 0)}",
        f"WhatsApp found: {values.get('whatsapp_found', 0)}",
        f"Runtime: {runtime_seconds:.1f}s",
    )
    for line in lines:
        logging.info(line)


def main() -> int:
    args = parse_args()
    root = configure_logging(args.verbose)
    summary = SummaryCollector()
    root.addHandler(summary)
    started = time.monotonic()
    try:
        result = lead_collector.main(collector_args(args))
    finally:
        root.removeHandler(summary)
    print_summary(summary.values, time.monotonic() - started, dry_run=not args.write_sheets)
    return result


if __name__ == "__main__":
    sys.exit(main())
