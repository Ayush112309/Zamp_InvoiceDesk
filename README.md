# Invoice Desk: from PDF to payment decision

**Zamp AI Solutions Analyst case study, PS-1 (Finance / AP).**

Upload a vendor invoice and the process reads it, checks it against the vendor master, the PO register and the payment ledger, then returns **Approve, Hold or Reject**. Every decision comes with the reason, the next step, and (when the fix is on the vendor's side) a draft reply. Each of the 8 stages is visible as it runs, and every run is saved to a history dashboard.

## Design principle: AI reads, rules decide

| Step | Tool | Why this tool |
|---|---|---|
| Read the PDF (any layout, including scans) | Vision LLM (Gemini or Claude) | Invoices are unstructured, and templates break on every new vendor |
| Validate, match, detect duplicates, decide | Plain Python rules | Payment decisions must be repeatable, explainable and auditable. Same input, same answer. |
| Fallback reader | Rule-based text parser | If the AI is down, digital PDFs still process. The run says which reader was used. |

## The 8 stages

1. **Receive document.** Digital PDF or scan?
2. **Read the invoice.** Extract vendor, GSTIN, invoice number, date, PO, lines, tax and total.
3. **Check the invoice itself.** Required fields, whether lines add up, whether subtotal plus tax equals the total, date sanity, and normalising to a **net (pre-GST)** amount.
4. **Verify the vendor.** Match to the master by GSTIN, then by name. Blocked vendors are rejected. A GSTIN mismatch triggers a hold.
5. **Match a purchase order.** Use the quoted PO if there is one. Otherwise **infer** it from open POs (70% amount, 30% description) with a confidence threshold.
6. **Look for duplicates.** Invoice numbers are normalised (`NCS/2026/0412` = `NCS-2026-412`), plus same-amount and close-date checks.
7. **Check amount against PO.** Compare against the *remaining* balance after earlier invoices, with tolerance of 2% and at most ₹5,000, and an auto-approval limit.
8. **Decide and record.** Any reject finding means Reject. Any hold finding means Hold. Otherwise Approve, and the invoice is posted to the ledger, which reduces the PO balance.

## Demo invoices and edge cases

| File | Scenario | Expected |
|---|---|---|
| `01_happy_path_sgp.pdf` | Clean invoice, PO quoted, CGST and SGST shown separately | **Approve** |
| `02_split_invoice_over_po_balance.pdf` | Second invoice on a ₹10L freight PO. ₹6.2L is already billed, so this ₹4.5L invoice overruns by ₹70k (18.4%) | **Hold**, plus a vendor email |
| `03_near_duplicate_nimbus.pdf` | Re-sent invoice: `NCS-2026-412` was already paid as `NCS/2026/0412` | **Reject** as a duplicate |
| `04_no_po_tax_inclusive_deccan.pdf` | No PO number, and prices include GST | PO-4505 inferred, GST backed out, **Approve** with an audit note |
| `05_scanned_missing_invoice_no.pdf` | Image-only scan with a blank invoice number | With AI on: **Hold** (no number means the duplicate check is unreliable). With AI off: **Hold** and route to manual keying. |

### Extra test bills (`data/test-bills/`)

Marketplace-style invoices (an Amazon-like layout the parser was not tuned on). Upload them with **Choose a PDF**, in this order:

| File | Scenario | Expected |
|---|---|---|
| `A_unknown_vendor_kairos.pdf` | Seller not in the vendor master | **Hold**: route to vendor onboarding |
| `B_marketplace_no_po_brightline.pdf` | No PO on the bill | PO-4509 inferred, **Approve** |
| `C_within_tolerance_brightline.pdf` | ₹1,800 (1.5%) over PO-4508 | Within tolerance, **Approve** with a note |
| `D_bank_change_gstin_mismatch_brightline.pdf` | Asks for payment to a new bank account, GSTIN off by one character | **Hold**: likely invoice fraud |

Bonus: run the happy path **twice**. The second run is rejected as a duplicate, because the first approval posted it to the ledger.

Use **Reset demo data** in the sidebar before every demo.

## Run locally (Windows PowerShell)

```powershell
python -m pip install -r requirements.txt
copy .env.example .env        # then open .env and paste your key
python scripts/check_ai.py    # optional: confirms the AI reader works
python -m uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Always use `python -m pip` and `python -m uvicorn` so both use the same Python.

**AI reader options** (set in `.env`, see `.env.example`):
- **Groq** (free tier): `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_API_KEY=gsk_...`, `LLM_MODEL=qwen/qwen3.8-27b`
- **Any OpenAI-compatible provider**: OpenRouter, OpenAI, Mistral, or Ollama on your laptop
- **Gemini**: `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-3.8-flash`
- **Claude**: `ANTHROPIC_API_KEY`

**Smart routing (`AI_MODE=auto`, the default):** digital PDFs are read by the fast text parser. The AI reader is used only for scans, or when the parser can't find every required field or the numbers don't add up. This is cheaper, faster and stays inside free-tier rate limits. Set `AI_MODE=always` to send every invoice to the AI.

To list the Groq models your key can use: `python -c "import re,requests;k=re.search(r'^LLM_API_KEY=(\S+)',open('.env').read(),re.M).group(1);print([m['id'] for m in requests.get('https://api.groq.com/openai/v1/models',headers={'Authorization':'Bearer '+k}).json()['data']])"`

Without any key, everything works except reading scans, which are routed to a person. If the AI call fails (busy, rate-limited, bad key), the app retries, then falls back to the rule-based reader for digital PDFs, and the run shows why.

To regenerate the sample PDFs, run `python scripts/make_samples.py`.

## Deploy (to get the live link for submission)

**Render (free):**
1. Push this folder to a GitHub repo.
2. On render.com, choose New, then Blueprint, and pick the repo (it reads `render.yaml`).
3. In Environment, add the same settings as your `.env` (for Groq: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). The `.env` file itself is not uploaded.
4. Deploy and you get `https://invoice-desk-xxxx.onrender.com`.

The free tier sleeps after about 15 minutes idle, and the first load then takes 30–60 seconds. **Open the link 2 minutes before the interview.** Data resets on redeploy, which is fine for a demo.

Any Docker host works too (Railway, Fly.io, Hugging Face Spaces with the Docker SDK) using the included `Dockerfile`.

## Changing the rules

Edit `data/rules.json`, `data/vendors.csv`, `data/purchase_orders.csv` or `data/ledger_seed.csv`. No code changes are needed. These files stand in for the ERP.

## 5-minute demo video script

- **0:00–0:30, the problem.** "AP teams open hundreds of PDF invoices, find the PO in a spreadsheet, check the numbers and decide. It's slow, and tired people make expensive mistakes. This takes a PDF and returns a decision with the reasoning."
- **0:30–1:45, happy path.** Click *Clean invoice*. Narrate the stages as they light up. Open *Read the invoice*. Point out the stamp, the posting, and the PO balance in the PO register.
- **1:45–2:45, split billing.** "The most expensive mistake is the one where every invoice looks fine on its own." Show stage 7: ₹6.2L already billed, ₹4.5L more requested, ₹70k over. Show the drafted vendor email.
- **2:45–3:30, duplicate.** "Same invoice, re-sent, number formatted differently. A human matching by eye misses this." Show the normalised number in stage 6.
- **3:30–4:15, no PO plus scan.** Inferred PO with confidence, then the scan held because there's no invoice number.
- **4:15–5:00, design choices.** AI reads and rules decide, confidence thresholds, nothing pays or sends automatically, run history as the audit trail. Then what's next: 3-way match with goods receipts, an email inbox trigger, ERP integration.

## Likely interview questions

- **Why not let the LLM decide?** Decisions about money must be deterministic and explainable to an auditor. The LLM does the one thing rules can't do: read messy documents.
- **What if extraction is wrong?** The arithmetic checks catch most misreads (lines must add up, and subtotal plus tax must equal the total). Low-confidence fields are flagged. Required fields that are missing block approval.
- **Why hold, not reject, a missing invoice number?** The invoice may be legitimate. The problem is that we can't prove it isn't a duplicate. A person, or a corrected invoice, resolves it.
- **Why compare net amounts?** POs are raised before tax. Comparing gross amounts against a net PO would falsely flag every invoice as 18% over.
- **What would you build next?** A 3-way match against goods receipts, an email inbox trigger, a reviewer queue with approve and override actions, learning from overrides to tune thresholds, and an ERP connector.
