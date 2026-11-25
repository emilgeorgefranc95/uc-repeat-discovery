"""CLI entry point to run one or all Stage 1 public data collectors.

Usage:
    python -m collect.run_all play_store
    python -m collect.run_all app_store
    python -m collect.run_all trustpilot
    python -m collect.run_all youtube
    python -m collect.run_all all
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from collect.app_store import collect_app_store
from collect.play_store import collect_play_store
from collect.trustpilot import collect_trustpilot
from collect.utils import load_config
from collect.youtube import collect_youtube

COLLECTORS: dict[str, Callable[[dict], dict]] = {
    "play_store": collect_play_store,
    "app_store": collect_app_store,
    "trustpilot": collect_trustpilot,
    "youtube": collect_youtube,
}


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m collect.run_all <play_store|app_store|trustpilot|youtube|all>")
        sys.exit(1)

    target = sys.argv[1].strip().lower()
    cfg = load_config()

    if target == "all":
        names = [name for name in COLLECTORS if cfg.get("sources", {}).get(name, {}).get("enabled", True)]
    elif target in COLLECTORS:
        names = [target]
    else:
        print(f"Unknown source '{target}'. Choose from: {', '.join(COLLECTORS.keys())}, all")
        sys.exit(1)

    results = []
    for name in names:
        print(f"\n=== Running collector: {name} ===")
        stats = COLLECTORS[name](cfg)
        results.append(stats)
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
