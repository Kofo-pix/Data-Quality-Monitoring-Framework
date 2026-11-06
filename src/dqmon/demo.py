"""Demo: three 'days' of an e-commerce dataset that degrades, so monitoring has something to catch.

Day 1  healthy baseline
Day 2  bad deploy: missing/invalid emails, duplicate orders, invalid statuses, negative amounts,
       orphan customer_ids, new column added to `orders`
Day 3  upstream outage: row volume collapses, data goes stale, `orders.amount` becomes TEXT
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

COUNTRIES = ["US", "NG", "GB", "DE", "IN"]
STATUSES = ["pending", "shipped", "completed", "completed", "completed", "cancelled", "returned"]
CURRENCIES = ["USD", "NGN", "GBP", "EUR"]
N_CUSTOMERS = {1: 500, 2: 520, 3: 520}
N_ORDERS = {1: 2000, 2: 2050, 3: 800}
BASE = date(2025, 3, 1)


def as_of_for(day: int) -> datetime:
    return datetime.combine(BASE + timedelta(days=day - 1), datetime.min.time()).replace(hour=12)


def build_demo_db(path: str | Path, day: int, seed: int = 7) -> None:
    rng = random.Random(seed + day)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    today = (as_of_for(day)).date()
    bad = day >= 2

    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT, email TEXT,
                   country TEXT, signup_date TEXT)""")
    amount_type = "TEXT" if day == 3 else "REAL"
    extra = ", discount REAL" if bad else ""
    con.execute(f"""CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, order_date TEXT,
                    status TEXT, amount {amount_type}, currency TEXT{extra})""")

    n_c = N_CUSTOMERS[day]
    customers = []
    for i in range(1, n_c + 1):
        email, country = f"user{i}@example.com", rng.choice(COUNTRIES)
        if bad:
            r = rng.random()
            if r < 0.15:
                email = None if rng.random() < 0.5 else ""
            elif r < 0.18:
                email = f"user{i}example.com"  # malformed
            if rng.random() < 0.02:
                country = rng.choice(["Nigeria", "usa"])
        signup = (BASE - timedelta(days=rng.randint(30, 60))).isoformat()
        customers.append((i, f"Customer {i}", email, country, signup))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

    last_order_day = today - timedelta(days=3) if day == 3 else today  # day 3: pipeline stalled
    orders = []
    for oid in range(1, N_ORDERS[day] + 1):
        cust = rng.randint(1, n_c)
        status, amount, cur = rng.choice(STATUSES), round(rng.uniform(5, 500), 2), rng.choice(CURRENCIES)
        if bad:
            if rng.random() < 0.02:
                cust = rng.randint(9000, 9500)  # orphan
            if rng.random() < 0.03:
                status = rng.choice(["COMPLETD", "unknown", "Shipped"])
            if rng.random() < 0.02:
                amount = -amount
            elif rng.random() < 0.005:
                amount = 25000.0
            if rng.random() < 0.01:
                cur = "XXX"
        odate = (last_order_day - timedelta(days=rng.randint(0, 14))).isoformat()
        row = (oid, cust, odate, status, amount, cur)
        orders.append(row + (round(rng.uniform(0, 0.2), 2),) if bad else row)
    if bad:  # duplicate order ids (retries / double-loads)
        orders += [orders[rng.randrange(len(orders))] for _ in range(int(len(orders) * 0.03))]
    marks = "?," * (len(orders[0]) - 1) + "?"
    con.executemany(f"INSERT INTO orders VALUES ({marks})", orders)
    con.commit()
    con.close()
