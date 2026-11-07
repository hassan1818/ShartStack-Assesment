#!/usr/bin/env python3
"""CLI:  python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?" """
import argparse
import json
import sys

from traceguard.agent import InvestigationAgent
from traceguard.store import EventStore
from traceguard.tools import FaultPlan, TraceGuardTools


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TraceGuard: investigate the financial state of an order.")
    p.add_argument("--events", required=True, help="JSON Lines file")
    p.add_argument("--tenant", required=True)
    p.add_argument("--order", required=True)
    p.add_argument("--question", default="What is the status of this order?")
    p.add_argument("--inject-fault", default="", help="demo: e.g. get_payment_events:timeout:2")
    p.add_argument("--max-attempts", type=int, default=3, help="hard cap on tool attempts (default 3)")
    p.add_argument("--verbose", action="store_true", help="print the tool-call trace to stderr")
    args = p.parse_args(argv)
    try:
        store = EventStore.from_file(args.events)
    except OSError as exc:
        print(json.dumps({"error": f"cannot read events file: {exc}"}), file=sys.stderr)
        return 2
    tools = TraceGuardTools(store, FaultPlan.parse(args.inject_fault))
    agent = InvestigationAgent(tools, max_attempts=max(1, args.max_attempts))
    output = agent.investigate(args.tenant, args.order, args.question)
    if args.verbose:
        print("\n".join(agent.trace), file=sys.stderr)
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
