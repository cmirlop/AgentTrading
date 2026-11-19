"""Run the synthetic local-news laboratory (offline by default)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news_local.fixtures import build_fixture
from src.news_local.models import NewsAnalysisInput
from src.news_local.providers import NewsSummarizer


FIXTURE_NAMES = (
    "scheduled",
    "published",
    "usd_only",
    "eur_only",
    "cad_only",
    "aud_only",
    "duplicate_revision",
    "injection",
    "unavailable",
    "late_first_seen",
)
SYMBOLS = ("EURUSD", "USDCAD", "AUDUSD")


def _parse_cutoff(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of-utc requires an explicit UTC offset")
    return parsed


def _safe_output_path(value: str) -> Path:
    requested = Path(value)
    target = (ROOT / requested).resolve() if not requested.is_absolute() else requested.resolve()
    allowed_roots = [
        (ROOT / "reports" / "private" / "news_local").resolve(),
        (ROOT / "reports" / "news_local_01").resolve(),
    ]
    if not any(_is_within(target, allowed) for allowed in allowed_roots):
        raise ValueError("OUTPUT_PATH_NOT_ALLOWED")
    return target


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("offline", "mock-ollama", "live-ollama"),
        default="offline",
    )
    parser.add_argument("--fixture", choices=FIXTURE_NAMES, default="published")
    parser.add_argument("--symbol", choices=SYMBOLS, default="EURUSD")
    parser.add_argument("--as-of-utc", type=_parse_cutoff)
    parser.add_argument("--output", help="Optional JSON under reports/private/news_local or reports/news_local_01")
    args = parser.parse_args(argv)

    news_input = build_fixture(args.fixture, args.symbol)
    if args.as_of_utc is not None:
        news_input = NewsAnalysisInput.model_validate(
            {
                **news_input.model_dump(mode="python"),
                "analysis_as_of_utc": args.as_of_utc,
            }
        )
    result = NewsSummarizer().summarize(news_input, mode=args.mode)
    serialized = result.model_dump_json(indent=2)
    if args.output:
        try:
            output_path = _safe_output_path(args.output)
        except ValueError:
            parser.error("output must be inside reports/private/news_local or reports/news_local_01")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
