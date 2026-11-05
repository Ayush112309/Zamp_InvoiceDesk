"""SQLite storage for run history and the posted-invoice ledger.

Master data (vendors, POs, rules) lives in CSV/JSON under data/ so a reviewer
can open and edit it without touching code.
"""
import csv
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "invoice_desk.db"))
UPLOADS = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
UPLOADS.mkdir(parents=True, exist_ok=True)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def load_rules():
    return json.loads((DATA / "rules.json").read_text())


def load_vendors():
    with open(DATA / "vendors.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["aliases"] = [a for a in (r.get("aliases") or "").split("|") if a]
    return rows


def load_pos():
    with open(DATA / "purchase_orders.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["amount_net"] = float(r["amount_net"])
    return rows


def init(reset=False):
    with conn() as c:
        if reset:
            c.execute("DROP TABLE IF EXISTS runs")
            c.execute("DROP TABLE IF EXISTS ledger")
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, source TEXT, status TEXT,
            decision TEXT, vendor TEXT, invoice_number TEXT, total REAL, currency TEXT,
            po_number TEXT, headline TEXT, extractor TEXT, duration_ms INTEGER,
            stages_json TEXT, result_json TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT,
            invoice_date TEXT, po_number TEXT, net_amount REAL, total REAL,
            run_id TEXT, posted_at TEXT, source TEXT)""")
        if c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            with open(DATA / "ledger_seed.csv") as f:
                for r in csv.DictReader(f):
                    c.execute("""INSERT INTO ledger (vendor_id, invoice_number, invoice_date, po_number,
                                 net_amount, total, run_id, posted_at, source)
                                 VALUES (?,?,?,?,?,?,NULL,?, 'opening balance')""",
                              (r["vendor_id"], r["invoice_number"], r["invoice_date"], r["po_number"],
                               float(r["net_amount"]), float(r["total"]), r["invoice_date"]))


def ledger_rows():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ledger ORDER BY invoice_date")]


def billed_against(po_number):
    with conn() as c:
        rows = c.execute("SELECT net_amount FROM ledger WHERE po_number=?", (po_number,)).fetchall()
    return sum(r[0] for r in rows), len(rows)


def post_to_ledger(vendor_id, inv, po_number, net, total, run_id, now):
    with conn() as c:
        c.execute("""INSERT INTO ledger (vendor_id, invoice_number, invoice_date, po_number, net_amount,
                     total, run_id, posted_at, source) VALUES (?,?,?,?,?,?,?,?, 'this tool')""",
                  (vendor_id, inv["invoice_number"], inv["invoice_date"], po_number, net, inv["total"], run_id, now))


def save_run(run):
    with conn() as c:
        c.execute("""INSERT OR REPLACE INTO runs (id, created_at, filename, source, status, decision, vendor,
                     invoice_number, total, currency, po_number, headline, extractor, duration_ms,
                     stages_json, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (run["id"], run["created_at"], run["filename"], run["source"], run["status"],
                   run.get("decision"), run.get("vendor"), run.get("invoice_number"), run.get("total"),
                   run.get("currency"), run.get("po_number"), run.get("headline"), run.get("extractor"),
                   run.get("duration_ms"), json.dumps(run.get("stages", [])), json.dumps(run.get("result"))))


def claim_run(run_id):
    """Atomically move a queued run to running. Returns False if someone else got it."""
    with conn() as c:
        cur = c.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,))
        return cur.rowcount == 1


def get_run(run_id):
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["stages"] = json.loads(d.pop("stages_json") or "[]")
    d["result"] = json.loads(d.pop("result_json") or "null")
    return d


def list_runs(limit=200):
    with conn() as c:
        rows = c.execute("""SELECT id, created_at, filename, source, status, decision, vendor, invoice_number,
                            total, currency, po_number, headline, extractor, duration_ms
                            FROM runs ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
