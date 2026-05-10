"""Audit existing scrapers' outputs to plan fixes."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
from pathlib import Path

dirs = [
    "abk", "bathco", "casalgrande padana", "euval", "fantini sanitare",
    "fir italia", "fondovalle ceramice", "fondovalle mobilier", "grandinetti",
    "gsg sanitare", "milano", "more mobilier", "omnia",
    "rosa splendiani mobilier", "sanycces", "terrazzo italiano", "unikolegno",
]
for d in dirs:
    p = Path(d) / "products.csv"
    if not p.exists():
        print(f"\n=== {d} (no products.csv) ===")
        continue
    df = pd.read_csv(p, encoding="utf-8-sig", keep_default_na=False, dtype=str)
    print(f"\n=== {d} ({len(df)} products) ===")
    cols = ["title", "category", "collection", "sizes", "thickness", "material", "finishes", "shape", "cut"]
    show = [c for c in cols if c in df.columns]
    if df.empty:
        continue
    sample = df.head(2)
    for _, row in sample.iterrows():
        for c in show:
            v = (row[c] or "")
            if c == "title" or c == "category":
                pass
            if len(v) > 80:
                v = v[:77] + "..."
            print(f"  {c}: {v!r}")
        d_text = (row.get("description") or "")
        if len(d_text) > 100:
            d_text = d_text[:100] + "..."
        print(f"  description: {d_text!r}")
        print()
