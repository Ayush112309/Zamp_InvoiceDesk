"""Web server for Invoice Desk.

Run locally:  uvicorn app.main:app --reload
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import os


def _load_env_file():
    """Read KEY=value lines from a .env file in the project folder (if present).
    Values already set in the terminal take priority."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.split(" #")[0].strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


_load_env_file()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import db, pipeline
from .extract import active_provider, provider_label

app = FastAPI(title="Invoice Desk")
STATIC = Path(__file__).parent / "static"
SAMPLES = db.DATA / "samples"
db.init()

SAMPLE_INFO = {
    "01_happy_path_sgp.pdf": ("Clean invoice, PO quoted", "Happy path. Everything matches."),
    "02_split_invoice_over_po_balance.pdf": ("Split billing over the PO", "Second invoice on a freight PO pushes it past its value."),
    "03_near_duplicate_nimbus.pdf": ("Re-sent invoice, new formatting", "Already paid last month, number written differently."),
    "04_no_po_tax_inclusive_deccan.pdf": ("No PO number, GST included", "Process has to infer the PO and back out the tax."),
    "05_scanned_missing_invoice_no.pdf": ("Scanned, invoice number blank", "Image-only PDF with a critical field missing."),
    "06_unknown_vendor_kairos.pdf": ("Seller not on the vendor list", "Marketplace bill from a vendor nobody onboarded."),
    "07_marketplace_no_po_brightline.pdf": ("Marketplace bill, no PO", "Amazon-style layout; the PO has to be found."),
    "08_within_tolerance_brightline.pdf": ("Slightly over the PO", "₹1,800 (1.5%) over, inside the tolerance."),
    "09_bank_change_request_brightline.pdf": ("Asks to pay a new bank account", "Classic invoice fraud, plus a GSTIN off by one character."),
}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    p = active_provider()
    return {"rules": db.load_rules(), "extractor": provider_label(p), "ai_enabled": bool(p),
            "vendors": db.load_vendors()}


@app.get("/api/samples")
def samples():
    return [{"name": f.name, "title": SAMPLE_INFO.get(f.name, (f.stem, ""))[0],
             "blurb": SAMPLE_INFO.get(f.name, ("", ""))[1]} for f in sorted(SAMPLES.glob("*.pdf"))]


@app.get("/api/samples/{name}")
def sample_file(name: str):
    f = SAMPLES / Path(name).name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{f.name}"'})


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None)):
    if sample:
        src = SAMPLES / Path(sample).name
        if not src.exists():
            raise HTTPException(404, "Sample not found")
        data, filename, source = src.read_bytes(), src.name, "sample"
    elif file:
        data, filename, source = await file.read(), file.filename or "upload.pdf", "upload"
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "File is larger than 15 MB")
    else:
        raise HTTPException(400, "Upload a PDF or choose a sample")
    run_id = datetime.now(timezone.utc).strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    (db.UPLOADS / f"{run_id}.pdf").write_bytes(data)
    db.save_run({"id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "filename": filename,
                 "source": source, "status": "queued"})
    return {"id": run_id}


def sse(obj):
    return f"data: {json.dumps(obj, default=str)}\n\n"


@app.get("/api/runs/{run_id}/stream")
def stream(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)

    def gen():
        if db.claim_run(run_id):
            pdf = (db.UPLOADS / f"{run_id}.pdf").read_bytes()
            yield sse({"type": "meta", "filename": run["filename"], "stages": [{"key": k, "title": t} for k, t in pipeline.STAGES]})
            for ev in pipeline.run(run_id, pdf, run["filename"]):
                if ev["type"] == "done":
                    res = ev["result"] or {}
                    inv = res.get("invoice") or {}
                    run.update({
                        "status": "done", "decision": res.get("decision"), "headline": res.get("headline"),
                        "vendor": (res.get("vendor") or {}).get("name") or inv.get("vendor_name"),
                        "invoice_number": inv.get("invoice_number"), "total": inv.get("total"),
                        "currency": inv.get("currency") or "INR", "po_number": (res.get("po") or {}).get("po_number"),
                        "extractor": ev["extractor"], "duration_ms": ev["duration_ms"],
                        "stages": ev["stages"], "result": res})
                    db.save_run(run)
                    yield sse({"type": "done", "run": db.get_run(run_id)})
                else:
                    yield sse(ev)
        else:
            # Already ran (or running elsewhere): wait for it, then replay.
            for _ in range(240):
                r = db.get_run(run_id)
                if r["status"] == "done":
                    break
                time.sleep(0.5)
            yield sse({"type": "meta", "filename": r["filename"], "stages": [{"key": k, "title": t} for k, t in pipeline.STAGES]})
            for s in r["stages"]:
                yield sse({"type": "stage_end", "stage": s})
            yield sse({"type": "done", "run": r})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    f = db.UPLOADS / f"{Path(run_id).name}.pdf"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="application/pdf")


@app.get("/api/pos")
def pos():
    ledger = db.ledger_rows()
    vendors = {v["vendor_id"]: v["name"] for v in db.load_vendors()}
    out = []
    for p in db.load_pos():
        inv = [l for l in ledger if l["po_number"] == p["po_number"]]
        billed = sum(l["net_amount"] for l in inv)
        out.append({**p, "vendor": vendors.get(p["vendor_id"], p["vendor_id"]), "billed": billed,
                    "remaining": p["amount_net"] - billed, "invoices": inv})
    return out


@app.post("/api/reset")
def reset():
    db.init(reset=True)
    for f in db.UPLOADS.glob("*.pdf"):
        f.unlink()
    return {"ok": True}
