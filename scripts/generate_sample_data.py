"""Regenerate the CSVs in sample_data/.

Usage:
    python scripts/generate_sample_data.py --orders 200 --per-type 3 --seed 42
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon.synthetic import generate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--per-type", type=int, default=3, help="discrepancies injected per type")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=ROOT / "sample_data")
    args = parser.parse_args()

    data = generate(n_orders=args.orders, per_type=args.per_type, seed=args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in data.to_csv().items():
        (args.out / f"{name}.csv").write_text(text, encoding="utf-8")
    print(f"Wrote {len(data.orders)} orders, {len(data.payments)} payments, "
          f"{len(data.settlements)} settlements to {args.out}")
    print(f"Injected {len(data.expected)} known discrepancies; {data.expected_pending} payments pending settlement")


if __name__ == "__main__":
    main()
