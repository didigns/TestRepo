"""data_pipeline.py - 매출 CSV를 읽어 월별 집계 리포트를 생성하는 파이프라인."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Sale:
    date: datetime
    product: str
    region: str
    amount: int


def load_sales(path: str) -> list[Sale]:
    rows: list[Sale] = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(Sale(
                date=datetime.fromisoformat(r["date"]),
                product=r["product"],
                region=r["region"],
                amount=int(r["amount"]),
            ))
    return rows


def monthly_summary(sales: list[Sale]) -> dict[str, int]:
    summary: dict[str, int] = defaultdict(int)
    for s in sales:
        key = s.date.strftime("%Y-%m")
        summary[key] += s.amount
    return dict(sorted(summary.items()))


def top_regions(sales: list[Sale], n: int = 3) -> list[tuple[str, int]]:
    by_region: dict[str, int] = defaultdict(int)
    for s in sales:
        by_region[s.region] += s.amount
    return sorted(by_region.items(), key=lambda kv: -kv[1])[:n]


if __name__ == "__main__":
    data = load_sales("sales.csv")
    print("월별 매출:", monthly_summary(data))
    print("상위 지역:", top_regions(data))
