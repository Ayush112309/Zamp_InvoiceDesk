"""Invoice -> decision pipeline.

Design principle: AI reads, rules decide.
  * The LLM is used only where the input is unstructured (reading the PDF).
  * Every decision (tolerance, duplicates, PO balance, approval) is plain,
    deterministic code so it is repeatable, explainable and auditable.

Each stage yields events so the UI can show it running live. Every stage
returns findings with a severity:
  reject -> the invoice must not be paid as-is
  hold   -> a person needs to look at it
  info   -> recorded for the audit trail, does not block
"""
import os
import re
import time
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

from . import db
from .extract import active_provider, ai_extract, pdf_profile, provider_label, rules_extract

STAGES = [
    ("receive", "Receive document"),
    ("extract", "Read the invoice"),
    ("validate", "Check the invoice itself"),
    ("vendor", "Verify the vendor"),
    ("po", "Match a purchase order"),
    ("duplicate", "Look for duplicates"),
    ("amount", "Check amount against PO"),
    ("decide", "Decide and record"),
]
PACE = float(os.getenv("STAGE_MIN_SECONDS", "0.45"))  # so each stage is visible in the live view


def today():
    """Overridable for repeatable demos: TODAY=2026-11-05"""
    t = os.getenv("TODAY")
    return date.fromisoformat(t) if t else date.today()


def inr(x):
    if x is None:
        return "-"
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        g = []
        while len(head) > 2:
            g.insert(0, head[-2:])
            head = head[:-2]
        if head:
            g.insert(0, head)
        whole = ",".join(g) + "," + tail
    out = f"₹{whole}" + ("" if dec == "00" else f".{dec}")
    return "-" + out if neg else out


def norm_invoice_no(s):
    """'NCS/2026/0412' and 'NCS-2026-412' both -> 'ncs-2026-412'."""
    if not s:
        return ""
    parts = re.findall(r"[a-z]+|\d+", s.lower())
    return "-".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


def norm_company(s):
    s = (s or "").lower()
    s = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|co|company|india)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


STOP = {"the", "and", "of", "for", "x", "with", "a", "to", "in", "on", "batch", "november", "october", "supply"}


def tokens(s):
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").lower()) if w not in STOP}


class Ctx:
    def __init__(self, run_id, pdf_bytes, filename):
        self.run_id, self.pdf, self.filename = run_id, pdf_bytes, filename
        self.rules = db.load_rules()
        self.vendors = db.load_vendors()
        self.pos = db.load_pos()
        self.findings = []
        self.inv = None
        self.profile = None
        self.vendor = None
        self.po = None
        self.po_inferred = False
        self.net = None
        self.extractor = None
        self.stop = False


def finding(sev, code, msg, vendor_msg=None):
    f = {"severity": sev, "code": code, "message": msg}
    if vendor_msg:
        f["vendor_msg"] = vendor_msg
    return f


# ------------------------------------------------------------------ stages ----

def s_receive(ctx):
    try:
        ctx.profile = pdf_profile(ctx.pdf)
    except Exception:
        ctx.stop = True
        return "fail", "This file isn't a readable PDF.", [], [finding("hold", "UNREADABLE",
                "The file could not be opened as a PDF. Ask the vendor to resend the invoice.")]
    p = ctx.profile
    kind = "Digital PDF with a text layer" if p["kind"] == "digital" else "Scanned image, no text layer"
    return "pass", f"{kind}, {p['pages']} page{'s' if p['pages'] > 1 else ''}.", [
        ["File", ctx.filename], ["Pages", p["pages"]], ["Type", kind], ["Text characters", p["text_chars"]]], []


def _rules_good_enough(inv, rules):
    """Is the cheap text parser's result complete and self-consistent? If yes, no need for AI."""
    if any(not inv.get(f) for f in rules["required_fields"]):
        return False
    total, sub, tax = inv.get("total"), inv.get("subtotal"), inv.get("tax_total")
    if tax is None and inv.get("tax_lines"):
        tax = sum(t["amount"] or 0 for t in inv["tax_lines"])
    if inv.get("tax_inclusive"):
        return tax is not None
    if sub is not None and tax is not None:
        return abs(sub + tax - total) <= rules["arithmetic_tolerance"]
    return False


def s_extract(ctx):
    provider = active_provider()
    notes = []
    mode = os.getenv("AI_MODE", "auto").lower()  # auto | always
    if provider and mode == "auto" and ctx.profile["kind"] == "digital":
        quick = rules_extract(ctx.profile["text"], ctx.vendors)
        if _rules_good_enough(quick, ctx.rules):
            ctx.inv = quick
            ctx.extractor = "Rule-based text parser"
            notes.append("Digital PDF read completely by the text parser, so the AI reader wasn't needed.")
            provider = None
        else:
            notes.append("Text parser couldn't read every field, so escalated to the AI reader.")
    if provider:
        try:
            ctx.inv = ai_extract(ctx.pdf, provider)
            ctx.extractor = provider_label(provider)
        except Exception as e:  # provider down / bad key / rate limit
            notes.append(f"AI extraction failed: {str(e)[:300]}")
    if ctx.inv is None:
        if ctx.profile["kind"] == "scanned":
            ctx.stop = True
            ctx.extractor = "None available"
            return "fail", "Scanned invoice and no AI reader available.", [["Note", n] for n in notes], [
                finding("hold", "SCAN_NOT_READABLE",
                        "This is a scanned image and the AI reader isn't available, so nothing can be extracted "
                        "reliably. Route to an AP clerk for manual keying.")]
        ctx.inv = rules_extract(ctx.profile["text"], ctx.vendors)
        ctx.extractor = "Rule-based text parser" + (" (AI fallback)" if provider else "")
    inv = ctx.inv
    conf = inv.get("field_confidence") or {}
    low = [k.replace("_", " ") for k, v in conf.items() if isinstance(v, (int, float)) and v < 0.6 and inv.get(k)]
    details = [["Read by", ctx.extractor]] + [["Note", n] for n in notes] + [
        ["Vendor", inv.get("vendor_name") or "— not found"],
        ["GSTIN", inv.get("vendor_gstin") or "— not found"],
        ["Invoice number", inv.get("invoice_number") or "— not found"],
        ["Invoice date", inv.get("invoice_date") or "— not found"],
        ["PO reference", inv.get("po_reference") or "— not printed"],
        ["Line items", len(inv.get("line_items") or [])],
        ["Subtotal", inr(inv.get("subtotal"))],
        ["Tax", ", ".join(f"{t['name']} {inr(t['amount'])}" for t in inv.get("tax_lines") or []) or "-"],
        ["Tax inclusive pricing", "Yes" if inv.get("tax_inclusive") else "No"],
        ["Total", inr(inv.get("total"))],
    ]
    if low:
        details.append(["Low confidence", ", ".join(low)])
    failed = any(n.startswith("AI extraction failed") for n in notes)
    return ("warn" if failed else "pass"), f"Extracted with {ctx.extractor}.", details, []


def s_validate(ctx):
    inv, r = ctx.inv, ctx.rules
    out, details = [], []
    labels = {"vendor_name": "vendor name", "invoice_number": "invoice number",
              "invoice_date": "invoice date", "total": "total amount"}
    for f in r["required_fields"]:
        if not inv.get(f):
            if f == "invoice_number":
                msg = ("No invoice number on the document. Without it the duplicate check can't be trusted, "
                       "so this can't be paid automatically. Ask the vendor for a corrected invoice.")
            else:
                msg = f"The {labels.get(f, f)} is missing or unreadable."
            out.append(finding("hold", "MISSING_" + f.upper(), msg,
                               f"The invoice doesn't show the {labels.get(f, f)}."))
    details.append(["Required fields", "All present" if not out else f"{len(out)} missing"])

    tol = r["arithmetic_tolerance"]
    items = [li["amount"] for li in inv.get("line_items") or [] if li.get("amount") is not None]
    sub, tax, total = inv.get("subtotal"), inv.get("tax_total"), inv.get("total")
    if tax is None and inv.get("tax_lines"):
        tax = sum(t["amount"] or 0 for t in inv["tax_lines"])
    if items:
        s = round(sum(items), 2)
        target = sub if sub is not None else (total if inv.get("tax_inclusive") else None)
        if target is not None:
            ok = abs(s - target) <= tol
            details.append(["Line items add up", f"{inr(s)} vs {inr(target)} {'✓' if ok else '✗'}"])
            if not ok:
                out.append(finding("hold", "LINES_DONT_ADD_UP",
                                   f"Line items sum to {inr(s)} but the invoice states {inr(target)}.",
                                   f"The line items add up to {inr(s)}, but the invoice states {inr(target)}."))
    if not inv.get("tax_inclusive") and sub is not None and tax is not None and total is not None:
        ok = abs(sub + tax - total) <= tol
        details.append(["Subtotal + tax = total", f"{inr(sub)} + {inr(tax)} = {inr(sub + tax)} vs {inr(total)} {'✓' if ok else '✗'}"])
        if not ok:
            out.append(finding("hold", "TOTAL_MISMATCH",
                               f"Subtotal plus tax is {inr(sub + tax)} but the total says {inr(total)}.",
                               f"Subtotal plus tax comes to {inr(sub + tax)}, but the total shown is {inr(total)}."))

    # Normalise to a net (pre-tax) amount, because POs are raised net of GST.
    if total is not None:
        if inv.get("tax_inclusive"):
            rate = next((t["rate"] for t in inv.get("tax_lines") or [] if t.get("rate")), None)
            if tax is not None:
                ctx.net, basis = total - tax, "tax-inclusive total minus stated GST"
            elif rate:
                ctx.net, basis = round(total / (1 + rate / 100), 2), f"tax-inclusive total ÷ {1 + rate / 100:g}"
            else:
                ctx.net, basis = total, "tax-inclusive but GST not stated, so gross used"
                out.append(finding("hold", "TAX_UNCLEAR",
                                   "Prices are tax-inclusive but the GST amount isn't stated, so the net value can't be confirmed."))
        elif sub is not None:
            ctx.net, basis = sub, "stated subtotal"
        elif tax is not None:
            ctx.net, basis = total - tax, "total minus tax"
        else:
            ctx.net, basis = total, "no tax shown, total used"
        details.append(["Net amount for PO match", f"{inr(ctx.net)} ({basis})"])

    if inv.get("invoice_date"):
        d = date.fromisoformat(inv["invoice_date"])
        age = (today() - d).days
        if age < -r["future_date_grace_days"]:
            out.append(finding("hold", "FUTURE_DATE", f"Invoice is dated {inv['invoice_date']}, which is in the future."))
        elif age > r["max_invoice_age_days"]:
            out.append(finding("hold", "STALE_INVOICE", f"Invoice is {age} days old, beyond the {r['max_invoice_age_days']}-day limit."))
        details.append(["Invoice age", f"{age} days"])
    if inv.get("notes"):
        details.append(["Note on invoice", inv["notes"]])
    return ("warn" if out else "pass"), ("Invoice is complete and internally consistent." if not out
                                         else f"{len(out)} issue{'s' if len(out) > 1 else ''} on the invoice itself."), details, out


def s_vendor(ctx):
    inv = ctx.inv
    g = (inv.get("vendor_gstin") or "").upper()
    name = norm_company(inv.get("vendor_name"))
    best, how, score = None, None, 0.0
    for v in ctx.vendors:
        if g and g == v["gstin"]:
            best, how, score = v, "GSTIN exact match", 1.0
            break
    if not best and name:
        for v in ctx.vendors:
            for n in [v["name"]] + v["aliases"]:
                s = SequenceMatcher(None, name, norm_company(n)).ratio()
                if s > score:
                    best, how, score = v, f"name similarity {s:.0%}", s
        if score < 0.82:
            best = None
    if not best:
        return "fail", "Vendor not found in the approved vendor master.", [["Name on invoice", inv.get("vendor_name") or "-"],
                ["GSTIN on invoice", g or "-"]], [finding("hold", "UNKNOWN_VENDOR",
                f"'{inv.get('vendor_name')}' isn't in the vendor master. Route to vendor onboarding before any payment.")]
    ctx.vendor = best
    out = []
    details = [["Matched to", f"{best['name']} ({best['vendor_id']})"], ["Matched by", how], ["Status", best["status"]]]
    if best["status"] != "approved":
        out.append(finding("reject", "BLOCKED_VENDOR", f"{best['name']} is marked '{best['status']}' in the vendor master."))
    text = " ".join([ctx.profile.get("text", ""), inv.get("notes") or ""])
    if re.search(r"(bank|account)\s+(details?\s+)?(has|have)?\s*changed|new bank (account|details)|change (of|in) bank|"
                 r"updated? (our )?bank|pay (to|into) (our|the) new", text, re.I):
        out.append(finding("hold", "BANK_CHANGE_REQUEST",
                           "The invoice asks for payment to new bank details. This is the most common invoice-fraud pattern; "
                           "bank changes must be verified with the vendor through the contact on file, never from the invoice."))
    if g and g != best["gstin"]:
        out.append(finding("hold", "GSTIN_MISMATCH",
                           f"GSTIN on the invoice ({g}) differs from the master ({best['gstin']}). "
                           "Possible impersonation or an un-notified registration change."))
    return ("fail" if any(f["severity"] == "reject" for f in out) else "warn" if out else "pass"), \
        f"Vendor verified: {best['name']}." if not out else "Vendor found, but with a problem.", details, out


def s_po(ctx):
    if not ctx.vendor:
        return "skip", "Skipped: vendor not verified.", [], []
    ref = ctx.inv.get("po_reference")
    rules = ctx.rules
    if ref:
        digits = re.sub(r"\D", "", ref)
        po = next((p for p in ctx.pos if re.sub(r"\D", "", p["po_number"]) == digits), None)
        if not po:
            return "fail", f"PO '{ref}' isn't in the PO register.", [["PO on invoice", ref]], [
                finding("hold", "PO_NOT_FOUND", f"The invoice quotes {ref}, which doesn't exist in the PO register.")]
        details = [["PO on invoice", ref], ["Matched", f"{po['po_number']} - {po['description']}"],
                   ["PO value (net)", inr(po["amount_net"])], ["PO status", po["status"]]]
        out = []
        if po["vendor_id"] != ctx.vendor["vendor_id"]:
            out.append(finding("hold", "PO_VENDOR_MISMATCH", f"{po['po_number']} was raised on a different vendor ({po['vendor_id']})."))
        if po["status"] != "open":
            out.append(finding("hold", "PO_CLOSED", f"{po['po_number']} is {po['status']}; it can't take new invoices."))
        if not out:
            ctx.po = po
        return ("warn" if out else "pass"), (f"Matched to {po['po_number']} as quoted on the invoice." if not out
                                             else "PO quoted, but it can't be used."), details, out

    # No PO printed: infer from the vendor's open POs.
    cands = [p for p in ctx.pos if p["vendor_id"] == ctx.vendor["vendor_id"] and p["status"] == "open"]
    if not cands:
        return "fail", "No PO quoted and the vendor has no open POs.", [], [
            finding("hold", "NO_PO", "No PO on the invoice and none open for this vendor. Needs a buyer to confirm.")]
    text = " ".join([li["description"] for li in ctx.inv.get("line_items") or []] + [ctx.inv.get("notes") or ""])
    if not text.strip():
        text = ctx.profile.get("text", "")
    inv_tok = tokens(text)
    scored = []
    for p in cands:
        billed, _ = db.billed_against(p["po_number"])
        remaining = p["amount_net"] - billed
        if ctx.net and remaining > 0:
            pct = abs(ctx.net - remaining) / remaining * 100
            amt_score = 1.0 if pct <= rules["tolerance_pct"] else max(0.0, 1 - pct / 25)
        else:
            amt_score = 0.0
        overlap = inv_tok & tokens(p["description"])
        desc_score = min(1.0, len(overlap) / 2)
        scored.append((round(0.7 * amt_score + 0.3 * desc_score, 2), p, remaining, sorted(overlap)))
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    margin = top[0] - (scored[1][0] if len(scored) > 1 else 0)
    conf = top[0] if margin >= 0.2 else round(top[0] * 0.6, 2)
    details = [["PO on invoice", "None printed"], ["Open POs for vendor", len(cands)]] + [
        [p["po_number"], f"score {s:.2f}: remaining {inr(rem)}; shared words: {', '.join(ov) or 'none'}"]
        for s, p, rem, ov in scored] + [["Confidence", f"{conf:.0%} (needs {rules['po_inference_min_confidence']:.0%})"]]
    if conf >= rules["po_inference_min_confidence"]:
        ctx.po, ctx.po_inferred = top[1], True
        return "warn", f"No PO printed; inferred {top[1]['po_number']} with {conf:.0%} confidence.", details, [
            finding("info", "PO_INFERRED", f"{top[1]['po_number']} inferred from amount and description "
                                           f"({conf:.0%} confidence). Ask the vendor to quote PO numbers going forward.")]
    return "fail", "No PO printed and no confident match.", details, [
        finding("hold", "PO_UNRESOLVED", "No PO on the invoice and no open PO is a confident match. A buyer needs to confirm which PO this belongs to.")]


def s_duplicate(ctx):
    if not ctx.vendor:
        return "skip", "Skipped: vendor not verified.", [], []
    inv, r = ctx.inv, ctx.rules
    n = norm_invoice_no(inv.get("invoice_number"))
    rows = [x for x in db.ledger_rows() if x["vendor_id"] == ctx.vendor["vendor_id"]]
    details = [["Compared against", f"{len(rows)} posted invoice{'s' if len(rows) != 1 else ''} from this vendor"]]
    if n:
        details.append(["Normalised number", n])
    for x in rows:
        xn = norm_invoice_no(x["invoice_number"])
        if n and xn == n:
            same_exact = (x["invoice_number"] == inv["invoice_number"])
            return "fail", "Already paid: this invoice is in the ledger.", details + [
                ["Matches", f"{x['invoice_number']} posted {x['posted_at'][:10]} for {inr(x['total'])}"]], [
                finding("reject", "DUPLICATE",
                        (f"Invoice {inv['invoice_number']} was already posted on {x['posted_at'][:10]} for {inr(x['total'])}. "
                         if same_exact else
                         f"Invoice {inv['invoice_number']} is {x['invoice_number']} written differently, already posted on "
                         f"{x['posted_at'][:10]} for {inr(x['total'])}. ") + "Paying it again would be a double payment.",
                        f"This appears to be the same invoice as {x['invoice_number']} ({inr(x['total'])}), which we have already processed.")]
    for x in rows:
        same_amt = inv.get("total") is not None and abs((x["total"] or 0) - inv["total"]) <= 1
        sim = SequenceMatcher(None, n, norm_invoice_no(x["invoice_number"])).ratio() if n else 0
        days = None
        if inv.get("invoice_date") and x["invoice_date"]:
            days = abs((date.fromisoformat(inv["invoice_date"]) - date.fromisoformat(x["invoice_date"])).days)
        if same_amt and days is not None and days <= 3:
            return "warn", "Possible duplicate: same amount, same dates.", details, [finding("hold", "POSSIBLE_DUPLICATE",
                    f"Same vendor, same amount ({inr(inv['total'])}) and dated within {days} days of {x['invoice_number']}.")]
        if same_amt and sim >= r["duplicate_similarity_threshold"] and days is not None and days <= r["duplicate_window_days"]:
            return "warn", "Possible duplicate: near-identical number and amount.", details, [finding("hold", "POSSIBLE_DUPLICATE",
                    f"Number is {sim:.0%} similar to {x['invoice_number']} with the same amount.")]
    if not n:
        return "warn", "No invoice number, so the check is only partial.", details, []
    return "pass", "No duplicate found.", details, []


def s_amount(ctx):
    if not ctx.po or ctx.net is None:
        return "skip", "Skipped: no usable PO or amount.", [], []
    r, po = ctx.rules, ctx.po
    billed, count = db.billed_against(po["po_number"])
    remaining = po["amount_net"] - billed
    details = [["PO value", inr(po["amount_net"])], ["Already billed", f"{inr(billed)} across {count} invoice{'s' if count != 1 else ''}"],
               ["Remaining", inr(remaining)], ["This invoice (net)", inr(ctx.net)]]
    out = []
    if ctx.net <= remaining + 0.5:
        details.append(["Balance after this invoice", inr(remaining - ctx.net)])
        status, summary = "pass", f"Within PO balance; {inr(remaining - ctx.net)} will remain."
    else:
        over = ctx.net - remaining
        if remaining <= 0:
            details.append(["Over by", inr(over)])
            out.append(finding("hold", "PO_FULLY_BILLED",
                               f"{po['po_number']} is already fully billed ({inr(billed)} across {count} invoice{'s' if count != 1 else ''}), "
                               f"so there is nothing left for this {inr(ctx.net)} invoice.",
                               f"{po['po_number']} has already been fully invoiced, so we can't accept further invoices against it."))
            return "fail", "PO already fully billed.", details, out
        pct = over / remaining * 100
        details.append(["Over by", f"{inr(over)} ({pct:.1f}%)"])
        details.append(["Tolerance", f"{r['tolerance_pct']}% and at most {inr(r['tolerance_abs'])}"])
        if pct <= r["tolerance_pct"] and over <= r["tolerance_abs"]:
            out.append(finding("info", "WITHIN_TOLERANCE", f"{inr(over)} over the PO balance, within tolerance."))
            status, summary = "warn", "Slightly over, within tolerance."
        else:
            split = (f" {count} earlier invoice{'s' if count != 1 else ''} already drew {inr(billed)} from this PO, "
                     "so this looks like split billing that overran the order.") if count else ""
            out.append(finding("hold", "OVER_PO_BALANCE",
                               f"Net {inr(ctx.net)} exceeds the remaining PO balance of {inr(remaining)} by {inr(over)} "
                               f"({pct:.1f}%), outside tolerance.{split}",
                               f"The invoice value of {inr(ctx.net)} before tax is more than the {inr(remaining)} still open on "
                               f"{po['po_number']}, by {inr(over)}."))
            status, summary = "fail", "Exceeds the remaining PO balance."
    if (ctx.inv.get("total") or 0) > r["auto_approve_limit"]:
        out.append(finding("hold", "ABOVE_LIMIT", f"Total is above the {inr(r['auto_approve_limit'])} auto-approval limit; needs a manager."))
        status = "warn" if status == "pass" else status
    return status, summary, details, out


NEXT = {
    "DUPLICATE": "Do not pay. Mark as duplicate and reply to the vendor referencing the original payment.",
    "BLOCKED_VENDOR": "Do not pay. Escalate to procurement; this vendor is blocked.",
    "OVER_PO_BALANCE": "Ask the buyer to amend the PO or ask the vendor for a credit note for the excess.",
    "MISSING_INVOICE_NUMBER": "Ask the vendor to reissue the invoice with an invoice number.",
    "SCAN_NOT_READABLE": "Route to an AP clerk to key the invoice manually.",
    "UNKNOWN_VENDOR": "Send to vendor onboarding before processing.",
    "GSTIN_MISMATCH": "Verify the vendor's GSTIN by phone using contact details from the master, not the invoice.",
    "PO_UNRESOLVED": "Ask the requester or buyer which PO this belongs to.",
    "POSSIBLE_DUPLICATE": "Compare with the earlier invoice before paying.",
    "PO_FULLY_BILLED": "Ask the buyer whether a new PO is needed, or return the invoice to the vendor.",
    "BANK_CHANGE_REQUEST": "Do not update bank details from an invoice. Call the vendor on the number in the master to verify.",
}


def vendor_email(ctx, decision, blocking):
    codes = {f["code"] for f in blocking}
    vendor_side = codes & {"DUPLICATE", "OVER_PO_BALANCE", "MISSING_INVOICE_NUMBER", "POSSIBLE_DUPLICATE", "TOTAL_MISMATCH", "LINES_DONT_ADD_UP"}
    if not vendor_side or not ctx.vendor:
        return None
    inv = ctx.inv
    ref = inv.get("invoice_number") or f"dated {inv.get('invoice_date') or 'unknown'} for {inr(inv.get('total'))}"
    lines = [f"Dear {ctx.vendor['name']} accounts team,", "",
             f"We've received your invoice {ref}. We can't process it yet:"]
    for f in blocking:
        if f["code"] in vendor_side:
            lines.append(f"- {f.get('vendor_msg', f['message'])}")
    ask = {
        "DUPLICATE": "If this is a genuinely new charge, please send a fresh invoice with a new number and supporting details.",
        "OVER_PO_BALANCE": "Please send a revised invoice within the PO balance, or a credit note for the excess, while we check the PO with our buyer.",
        "MISSING_INVOICE_NUMBER": "Please reissue the invoice with a unique invoice number.",
    }
    for c in vendor_side:
        if c in ask:
            lines += ["", ask[c]]
    lines += ["", "Thank you,", "Accounts Payable"]
    return "\n".join(lines)


def s_decide(ctx):
    blocking = [f for f in ctx.findings if f["severity"] in ("reject", "hold")]
    rejects = [f for f in blocking if f["severity"] == "reject"]
    decision = "REJECT" if rejects else "HOLD" if blocking else "APPROVE"
    ordered = rejects + [f for f in blocking if f["severity"] == "hold"]
    if decision == "APPROVE":
        headline = f"Approved for payment: {inr(ctx.inv['total'])} against {ctx.po['po_number']}."
        next_action = "Posted to the ledger and queued for the next payment run."
        db.post_to_ledger(ctx.vendor["vendor_id"], ctx.inv, ctx.po["po_number"], ctx.net, ctx.inv["total"],
                          ctx.run_id, datetime.now(timezone.utc).isoformat())
    else:
        headline = ordered[0]["message"]
        next_action = NEXT.get(ordered[0]["code"], "Send to an AP reviewer.")
    result = {
        "decision": decision, "headline": headline, "next_action": next_action,
        "reasons": [f["message"] for f in ordered] or ["All checks passed."],
        "notes": [f["message"] for f in ctx.findings if f["severity"] == "info"],
        "invoice": ctx.inv, "net_amount": ctx.net,
        "vendor": ctx.vendor and {k: ctx.vendor[k] for k in ("vendor_id", "name", "status")},
        "po": ctx.po and {**ctx.po, "inferred": ctx.po_inferred},
        "vendor_email": vendor_email(ctx, decision, ordered) if decision != "APPROVE" else None,
        "extractor": ctx.extractor,
    }
    ctx.result = result
    status = {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[decision]
    return status, next_action, [["Decision", decision], ["Blocking issues", len(blocking)],
                                                         ["Notes", len(result["notes"])]], []


FUNCS = {"receive": s_receive, "extract": s_extract, "validate": s_validate, "vendor": s_vendor,
         "po": s_po, "duplicate": s_duplicate, "amount": s_amount, "decide": s_decide}


def run(run_id, pdf_bytes, filename):
    """Generator of UI events. The final event carries the result."""
    ctx = Ctx(run_id, pdf_bytes, filename)
    stages = []
    t_all = time.time()
    for key, title in STAGES:
        yield {"type": "stage_start", "key": key, "title": title}
        t0 = time.time()
        if ctx.stop and key != "decide":
            status, summary, details, out = "skip", "Skipped: earlier stage couldn't complete.", [], []
        else:
            try:
                status, summary, details, out = FUNCS[key](ctx)
            except Exception as e:
                status, summary, details = "fail", f"Stage error: {type(e).__name__}", [["Error", str(e)[:300]]]
                out = [finding("hold", "STAGE_ERROR", f"The '{title}' step failed unexpectedly, so a person must review this invoice.")]
                ctx.stop = key in ("receive", "extract")
        ctx.findings += out
        elapsed = time.time() - t0
        if elapsed < PACE and status != "skip":
            time.sleep(PACE - elapsed)
        stage = {"key": key, "title": title, "status": status, "summary": summary,
                 "details": [[str(a), str(b)] for a, b in details], "findings": out,
                 "duration_ms": int((time.time() - t0) * 1000)}
        stages.append(stage)
        yield {"type": "stage_end", "stage": stage}
    yield {"type": "done", "stages": stages, "result": ctx.result, "duration_ms": int((time.time() - t_all) * 1000),
           "extractor": ctx.extractor}
