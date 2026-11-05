"""Turn an invoice PDF into structured fields.

Primary path: send the PDF to a vision-capable LLM (Claude or Gemini). That
handles digital PDFs and scans the same way, and copes with any layout.

Fallback path: a rule-based parser over the PDF text layer. It exists so the
process still runs if the AI provider is down or rate-limited, and it is
clearly labelled in the run view. It cannot read scans.
"""
import base64
import io
import json
import os
import re
from datetime import datetime

import pdfplumber
import requests

PROMPT = """You are an accounts-payable data extraction engine. Read this vendor invoice and
return ONLY a JSON object (no markdown, no commentary) with exactly these keys:

{
  "vendor_name": string|null,
  "vendor_gstin": string|null,          // tax registration id printed for the SELLER
  "invoice_number": string|null,        // exactly as printed; null if blank or absent
  "invoice_date": "YYYY-MM-DD"|null,    // Indian invoices use DD/MM/YYYY
  "po_reference": string|null,          // purchase order number if printed anywhere
  "currency": string|null,
  "line_items": [{"description": string, "quantity": number|null, "unit_price": number|null, "amount": number|null}],
  "subtotal": number|null,              // pre-tax total if printed
  "tax_lines": [{"name": string, "rate": number|null, "amount": number|null}],
  "tax_total": number|null,
  "tax_inclusive": boolean,             // true if line prices/total already include tax
  "total": number|null,                 // final amount payable
  "notes": string|null,                 // any remark relevant to AP (e.g. "re-issued copy")
  "field_confidence": {"vendor_name":0-1,"invoice_number":0-1,"invoice_date":0-1,"po_reference":0-1,"total":0-1}
}

Rules: never invent values. If a field is blank, illegible or missing, use null and give it low
confidence. Numbers must be plain numbers without commas or currency symbols."""


def pdf_profile(pdf_bytes):
    """Pages, text layer and whether it looks like a scan."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    chars = len(text.strip())
    return {"pages": pages, "text": text, "text_chars": chars, "kind": "digital" if chars > 40 else "scanned"}


def active_provider():
    forced = os.getenv("EXTRACTOR", "").lower()
    if forced == "rules":
        return None
    if forced in ("anthropic", "gemini", "openai_compat"):
        return forced
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("LLM_MODEL") and os.getenv("LLM_BASE_URL"):
        return "openai_compat"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return None


def provider_label(p):
    if p == "anthropic":
        return f"Claude ({os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-5-5')})"
    if p == "gemini":
        return f"Gemini ({os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')})"
    if p == "openai_compat":
        from urllib.parse import urlparse
        host = urlparse(os.getenv("LLM_BASE_URL", "")).hostname or "custom"
        return f"{os.getenv('LLM_MODEL')} via {host}"
    return "Rule-based text parser"


def _parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()  # reasoning models
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def _api_error(r):
    """Pull the provider's human-readable reason out of an error response."""
    try:
        e = r.json().get("error", r.text)
        return (e.get("message", str(e)) if isinstance(e, dict) else str(e))[:300]
    except ValueError:
        return r.text[:300]


def _call_anthropic(pdf_bytes):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            "max_tokens": 2000,
            "temperature": 0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                    "data": base64.b64encode(pdf_bytes).decode()}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        },
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Claude {r.status_code}: {_api_error(r)}")
    text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    return _parse_json(text)


def _gemini_once(model, pdf_bytes):
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip().strip('"').strip("'"),
                 "content-type": "application/json"},
        json={
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
                {"text": PROMPT},
            ]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        timeout=90,
    )


def _call_gemini(pdf_bytes):
    """Try the main model, retrying when Google is busy (429/503), then the backup model."""
    import time
    models = [os.getenv("GEMINI_MODEL", "gemini-3.8-flash")]
    if os.getenv("GEMINI_FALLBACK_MODEL"):
        models.append(os.getenv("GEMINI_FALLBACK_MODEL"))
    last = None
    for model in models:
        for attempt in range(3):
            r = _gemini_once(model, pdf_bytes)
            if r.ok:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return _parse_json("".join(p.get("text", "") for p in parts if not p.get("thought")))
            last = f"Gemini {r.status_code} ({model}): {_api_error(r)}"
            if r.status_code not in (429, 500, 503):
                break  # bad key / bad model: retrying won't help
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def _pdf_to_images(pdf_bytes, max_pages=3):
    """Render pages to JPEG so any vision model can read them (most can't take PDFs)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    out = []
    for i in range(min(len(doc), max_pages)):
        img = doc[i].render(scale=2).to_pil().convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def _call_openai_compat(pdf_bytes):
    """Works with OpenRouter, Groq, OpenAI, Mistral, Together, Ollama... anything with /chat/completions."""
    import time
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    headers = {"content-type": "application/json"}
    if os.getenv("LLM_API_KEY"):
        headers["authorization"] = "Bearer " + os.environ["LLM_API_KEY"].strip().strip('"').strip("'")
    content = [{"type": "text", "text": PROMPT}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}} for b64 in _pdf_to_images(pdf_bytes)]
    body = {"model": os.environ["LLM_MODEL"], "temperature": 0,
            "messages": [{"role": "user", "content": content}]}
    last = None
    for attempt in range(3):
        r = requests.post(base + "/chat/completions", headers=headers, json=body, timeout=120)
        if r.ok:
            msg = r.json()["choices"][0]["message"]["content"]
            if isinstance(msg, list):
                msg = "".join(p.get("text", "") for p in msg)
            return _parse_json(msg)
        last = f"{r.status_code} ({body['model']}): {_api_error(r)}"
        if r.status_code not in (429, 500, 502, 503):
            break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


def ai_extract(pdf_bytes, provider):
    raw = {"anthropic": _call_anthropic, "gemini": _call_gemini,
           "openai_compat": _call_openai_compat}[provider](pdf_bytes)
    return normalise(raw)


# ---------------------------------------------------------------- helpers ----

def to_num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s not in ("", ".", "-") else None
    except ValueError:
        return None


DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%d-%b-%Y"]


def to_iso_date(v):
    if not v:
        return None
    s = str(v).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def normalise(d):
    d = dict(d or {})
    for k in ("subtotal", "tax_total", "total"):
        d[k] = to_num(d.get(k))
    d["invoice_date"] = to_iso_date(d.get("invoice_date")) or (d.get("invoice_date") if re.match(r"\d{4}-\d{2}-\d{2}$", str(d.get("invoice_date") or "")) else None)
    for k in ("invoice_number", "po_reference", "vendor_name", "vendor_gstin"):
        v = d.get(k)
        d[k] = v.strip() if isinstance(v, str) and v.strip() and not re.fullmatch(r"[_\-\s.]+", v.strip()) else None
    items = []
    for li in d.get("line_items") or []:
        items.append({"description": li.get("description") or "", "quantity": to_num(li.get("quantity")),
                      "unit_price": to_num(li.get("unit_price")), "amount": to_num(li.get("amount"))})
    d["line_items"] = items
    d["tax_lines"] = [{"name": t.get("name") or "Tax", "rate": to_num(t.get("rate")), "amount": to_num(t.get("amount"))}
                      for t in (d.get("tax_lines") or [])]
    if d["tax_total"] is None and d["tax_lines"] and all(t["amount"] is not None for t in d["tax_lines"]):
        d["tax_total"] = round(sum(t["amount"] for t in d["tax_lines"]), 2)
    d["tax_inclusive"] = bool(d.get("tax_inclusive"))
    d["currency"] = (d.get("currency") or "INR").upper().replace("RS.", "INR").replace("₹", "INR")
    d.setdefault("field_confidence", {})
    d.setdefault("notes", None)
    return d


# ------------------------------------------------------- rule-based fallback --

AMT = r"([\d,]+\.\d{2}|[\d,]{3,})"


def _find(patterns, text, flags=re.I):
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return None


def rules_extract(text, vendors):
    t = text
    out = {
        "invoice_number": _find([r"invoice\s*(?:no|number|#)\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{2,})",
                                 r"bill\s*no\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{2,})"], t),
        "invoice_date": _find([r"invoice\s*date\s*[:]?\s*([\d]{1,2}[/\-.][\d]{1,2}[/\-.][\d]{4})",
                               r"date of issue\s*[:]?\s*(\d{1,2} \w{3,9} \d{4})",
                               r"issued\s*[:]?\s*(\d{1,2} \w{3,9} \d{4})",
                               r"date\s*[:]?\s*([\d]{1,2}[/\-.][\d]{1,2}[/\-.][\d]{4})"], t),
        "po_reference": _find([r"(?:po\s*reference|purchase\s*order|order\s*no\.?|your\s*ref)\s*[:.]?\s*(PO[\s\-]?\d{3,})",
                               r"\b(PO[\s\-]?\d{3,})\b"], t),
        "vendor_gstin": _find([r"GSTIN\s*[:]?\s*([0-9]{2}[A-Z0-9]{13})"], t),
        "subtotal": _find([r"(?:sub\s*total|taxable\s*value)\s*(?:\(INR\))?\s*" + AMT], t),
        "total": _find([r"grand\s*total\s*(?:\(INR\))?\s*" + AMT, r"total\s*amount\s*payable\s*" + AMT,
                        r"amount\s*due\s*(?:INR)?\s*" + AMT, r"total\s*\(incl\.?\s*GST\)\s*" + AMT], t),
    }
    tax_lines = []
    for m in re.finditer(r"\b(CGST|SGST|IGST)\s*(?:@|\()?\s*(\d+(?:\.\d+)?)\s*%\)?\s*" + AMT, t, re.I):
        tax_lines.append({"name": m.group(1).upper(), "rate": float(m.group(2)), "amount": to_num(m.group(3))})
    inclusive = bool(re.search(r"inclusive of (?:GST|tax)|incl\.?\s*GST", t, re.I))
    tax_total = None
    if inclusive:
        m = re.search(r"GST included\s*@?\s*(\d+(?:\.\d+)?)%?\s*:?\s*" + AMT, t, re.I)
        if m:
            tax_lines.append({"name": "GST (included)", "rate": float(m.group(1)), "amount": to_num(m.group(2))})
    # Vendor: match master names/aliases against text (GSTIN first)
    vendor_name = None
    low = t.lower()
    for v in vendors:
        if out["vendor_gstin"] and out["vendor_gstin"] == v["gstin"]:
            vendor_name = v["name"]
            break
    if not vendor_name:
        for v in vendors:
            for n in [v["name"]] + v["aliases"]:
                if len(n) > 4 and n.lower() in low:
                    vendor_name = v["name"]
                    break
            if vendor_name:
                break
    if not vendor_name:
        vendor_name = next((ln.strip() for ln in t.splitlines() if ln.strip()), None)
    out.update({
        "vendor_name": vendor_name, "tax_lines": tax_lines, "tax_total": tax_total, "tax_inclusive": inclusive,
        "currency": "INR", "line_items": [], "notes": "Re-issued copy" if re.search(r"re-?issued", t, re.I) else None,
        "field_confidence": {k: (0.7 if out.get(k) else 0.0) for k in ("invoice_number", "invoice_date", "po_reference", "total")},
    })
    out["field_confidence"]["vendor_name"] = 0.7 if vendor_name else 0.0
    return normalise(out)
