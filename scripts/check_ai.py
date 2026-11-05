"""Check that your AI reader works, using the same code the app uses.

Run from the invoice-desk folder:   python scripts/check_ai.py
Reads settings from .env. Prints only the start of your key.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.main  # noqa: F401  (loads .env)
from app.extract import active_provider, ai_extract, provider_label

p = active_provider()
if not p:
    sys.exit("No AI reader configured. Add GEMINI_API_KEY, or LLM_BASE_URL + LLM_API_KEY + LLM_MODEL, to .env")
print("Using:", provider_label(p))
pdf = (ROOT / "data" / "samples" / "05_scanned_missing_invoice_no.pdf").read_bytes()
try:
    out = ai_extract(pdf, p)
except Exception as e:
    sys.exit(f"FAILED: {e}")
print("OK. Read from the scanned invoice:")
for k in ("vendor_name", "invoice_number", "invoice_date", "po_reference", "subtotal", "total"):
    print(f"  {k:15} {out.get(k)}")
print("Expected: invoice_number = None (it is blank on the scan), total = 88500")
