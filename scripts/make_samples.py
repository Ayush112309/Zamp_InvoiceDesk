"""Generate the five demo invoices in data/samples/.

Each vendor uses a different layout on purpose: real AP inboxes never get two
invoices that look the same. One invoice is rendered to an image so it behaves
like a scan (no text layer).

Run:  python scripts/make_samples.py
"""
import io
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
W, H = A4


def inr(x):
    """Indian digit grouping: 283200 -> 2,83,200.00"""
    s = f"{x:.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{whole}.{dec}"


def table(c, x, y, cols, rows, widths, font=("Helvetica", 9), head_fill=None):
    c.setFont("Helvetica-Bold", font[1])
    if head_fill:
        c.setFillColor(head_fill)
        c.rect(x, y - 4, sum(widths), 16, fill=1, stroke=0)
        c.setFillColor(colors.white)
    cx = x
    for col, w in zip(cols, widths):
        c.drawString(cx + 4, y, col)
        cx += w
    c.setFillColor(colors.black)
    c.setFont(*font)
    y -= 20
    for r in rows:
        cx = x
        for i, (val, w) in enumerate(zip(r, widths)):
            if i >= 1 and i != len(r) and isinstance(val, str) and val[:1].isdigit() and i > 1:
                c.drawRightString(cx + w - 4, y, val)
            else:
                c.drawString(cx + 4, y, val)
            cx += w
        y -= 16
    return y


# 1. Happy path ---------------------------------------------------------------
def happy(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, H - 25 * mm, "SHREE GANESH PACKAGING PVT LTD")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, H - 31 * mm, "Plot 44, IDA Patancheru, Sangareddy, Telangana 502319")
    c.drawString(20 * mm, H - 36 * mm, "GSTIN: 36AAKCS4417K1Z3   |   accounts@sgpack.in")
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(W - 20 * mm, H - 25 * mm, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    c.drawRightString(W - 20 * mm, H - 33 * mm, "Invoice No: SGP/26-27/0193")
    c.drawRightString(W - 20 * mm, H - 39 * mm, "Invoice Date: 28/10/2026")
    c.drawRightString(W - 20 * mm, H - 45 * mm, "PO Reference: PO-4501")
    c.line(20 * mm, H - 52 * mm, W - 20 * mm, H - 52 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, H - 60 * mm, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, H - 66 * mm, "Kaveri Home Appliances Ltd, Gachibowli, Hyderabad 500032")
    rows = [
        ["1", "Corrugated box 12x10x8 (5-ply)", "1000", "90.00", inr(90000)],
        ["2", "Corrugated box 18x12x12 (5-ply)", "800", "125.00", inr(100000)],
        ["3", "Corrugated box 24x18x18 (7-ply)", "250", "200.00", inr(50000)],
    ]
    y = table(c, 20 * mm, H - 82 * mm, ["#", "Description", "Qty", "Rate", "Amount"], rows,
              [12 * mm, 85 * mm, 22 * mm, 25 * mm, 26 * mm], head_fill=colors.HexColor("#33415c"))
    x2 = W - 20 * mm
    y -= 8
    c.line(110 * mm, y + 10, x2, y + 10)
    for label, val in [("Sub Total", 240000), ("CGST @ 9%", 21600), ("SGST @ 9%", 21600)]:
        c.drawString(110 * mm, y, label)
        c.drawRightString(x2, y, inr(val))
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawString(110 * mm, y - 2, "Grand Total (INR)")
    c.drawRightString(x2, y - 2, inr(283200))
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, 30 * mm, "Payment terms: Net 30. Bank: HDFC Bank, A/c 50200031177420, IFSC HDFC0001234")
    c.save()


# 2. Split invoice that overruns the PO balance --------------------------------
def split_overrun(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFillColor(colors.HexColor("#0f4c5c"))
    c.rect(0, H - 40 * mm, W, 40 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(18 * mm, H - 20 * mm, "Apex Logistics")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, H - 27 * mm, "Apex Logistics India Pvt Ltd  .  Bhiwandi, Maharashtra  .  GSTIN 27AAICA9034L1ZB")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 18 * mm, H - 20 * mm, "INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    y = H - 52 * mm
    for k, v in [("Invoice Number", "APX-INV-2287"), ("Date of Issue", "31 Oct 2026"),
                 ("Against Order No.", "PO-4502"), ("Customer", "Kaveri Home Appliances Ltd")]:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(18 * mm, y, k)
        c.setFont("Helvetica", 10)
        c.drawString(60 * mm, y, v)
        y -= 14
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Particulars")
    c.drawRightString(W - 18 * mm, y, "Amount (INR)")
    c.line(18 * mm, y - 4, W - 18 * mm, y - 4)
    c.setFont("Helvetica", 10)
    y -= 20
    c.drawString(18 * mm, y, "Freight charges - October 2026, HYD-PUN lane (38 FTL trips, consolidated)")
    c.drawRightString(W - 18 * mm, y, inr(450000))
    y -= 30
    for label, val in [("Taxable Value", 450000), ("IGST 18%", 81000)]:
        c.drawString(110 * mm, y, label)
        c.drawRightString(W - 18 * mm, y, inr(val))
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawString(110 * mm, y, "Total Amount Payable")
    c.drawRightString(W - 18 * mm, y, inr(531000))
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, 25 * mm, "Trip-wise annexure available on request. E&OE.")
    c.save()


# 3. Near-duplicate of an invoice already paid --------------------------------
def duplicate(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#6b7280"))
    c.drawString(22 * mm, H - 20 * mm, "Nimbus Cloud Services LLP")
    c.drawString(22 * mm, H - 25 * mm, "Koramangala, Bengaluru 560034  |  GSTIN 29AAQFN8812M1ZK")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(22 * mm, H - 45 * mm, "Invoice")
    c.setFont("Helvetica", 10)
    c.drawString(22 * mm, H - 56 * mm, "Invoice # NCS-2026-412")
    c.drawString(22 * mm, H - 62 * mm, "Issued 01 Oct 2026")
    c.drawString(22 * mm, H - 68 * mm, "Your ref: PO 4503")
    c.drawString(120 * mm, H - 56 * mm, "Billed to")
    c.drawString(120 * mm, H - 62 * mm, "Kaveri Home Appliances Ltd")
    c.drawString(120 * mm, H - 68 * mm, "Hyderabad, Telangana")
    y = H - 90 * mm
    c.setStrokeColor(colors.HexColor("#d1d5db"))
    c.line(22 * mm, y + 12, W - 22 * mm, y + 12)
    c.drawString(22 * mm, y, "Managed cloud hosting - October 2026 (plan: Business 8 vCPU)")
    c.drawRightString(W - 22 * mm, y, inr(30000))
    c.line(22 * mm, y - 8, W - 22 * mm, y - 8)
    y -= 30
    for label, val in [("Subtotal", 30000), ("IGST (18%)", 5400)]:
        c.drawString(120 * mm, y, label)
        c.drawRightString(W - 22 * mm, y, inr(val))
        y -= 15
    c.setFont("Helvetica-Bold", 12)
    c.drawString(120 * mm, y - 4, "Amount due")
    c.drawRightString(W - 22 * mm, y - 4, "INR " + inr(35400))
    c.setFont("Helvetica", 8)
    c.drawString(22 * mm, 25 * mm, "Re-issued copy. Please ignore if already paid.")
    c.save()


# 4. No PO number, tax-inclusive pricing --------------------------------------
def no_po(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Times-Bold", 18)
    c.drawCentredString(W / 2, H - 22 * mm, "DECCAN OFFICE SUPPLIES")
    c.setFont("Times-Roman", 10)
    c.drawCentredString(W / 2, H - 28 * mm, "Abids Road, Hyderabad 500001  -  GSTIN 36ABHPD5521Q1ZS  -  Ph 040-2475 1188")
    c.line(20 * mm, H - 32 * mm, W - 20 * mm, H - 32 * mm)
    c.setFont("Times-Bold", 12)
    c.drawCentredString(W / 2, H - 40 * mm, "BILL")
    c.setFont("Times-Roman", 11)
    c.drawString(20 * mm, H - 50 * mm, "Bill No. DOS-1187")
    c.drawRightString(W - 20 * mm, H - 50 * mm, "Date: 03-11-2026")
    c.drawString(20 * mm, H - 57 * mm, "To: Kaveri Home Appliances Ltd (Admin dept.)")
    c.drawString(20 * mm, H - 64 * mm, "Annual stationery supply - November batch")
    rows = [
        ["A4 copier paper 75gsm (ream)", "120", "295.00", inr(35400)],
        ["Box files, lever arch", "60", "118.00", inr(7080)],
        ["Gel pens (box of 10)", "40", "177.00", inr(7080)],
        ["Whiteboard markers (set)", "40", "177.00", inr(7080)],
    ]
    y = table(c, 20 * mm, H - 80 * mm, ["Item", "Qty", "Rate", "Amount"], rows,
              [90 * mm, 20 * mm, 30 * mm, 30 * mm], font=("Times-Roman", 10))
    y -= 8
    c.setFont("Times-Bold", 12)
    c.drawString(110 * mm, y, "Total (incl. GST)")
    c.drawRightString(W - 20 * mm, y, inr(56640))
    c.setFont("Times-Italic", 9)
    c.drawString(20 * mm, y - 20, "All prices are inclusive of GST. GST included @18%: 8,640.00")
    c.drawString(20 * mm, 25 * mm, "Goods once sold will not be taken back. Subject to Hyderabad jurisdiction.")
    c.save()


# 5. Scanned image, invoice number left blank ----------------------------------
def scanned(path):
    from PIL import Image, ImageFilter
    import pypdfium2 as pdfium

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, H - 25 * mm, "SHREE GANESH PACKAGING PVT LTD")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, H - 31 * mm, "Plot 44, IDA Patancheru, Sangareddy, Telangana 502319  GSTIN: 36AAKCS4417K1Z3")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20 * mm, H - 44 * mm, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, H - 52 * mm, "Invoice No: ____________")
    c.drawString(20 * mm, H - 58 * mm, "Invoice Date: 02/11/2026")
    c.drawString(20 * mm, H - 64 * mm, "PO Reference: PO-4507")
    rows = [["1", "Wooden pallet 1200x1000, heat treated (ISPM-15)", "150", "500.00", inr(75000)]]
    y = table(c, 20 * mm, H - 80 * mm, ["#", "Description", "Qty", "Rate", "Amount"], rows,
              [10 * mm, 95 * mm, 18 * mm, 22 * mm, 25 * mm])
    y -= 8
    for label, val in [("Sub Total", 75000), ("CGST @ 9%", 6750), ("SGST @ 9%", 6750)]:
        c.drawString(110 * mm, y, label)
        c.drawRightString(W - 20 * mm, y, inr(val))
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawString(110 * mm, y, "Grand Total (INR)")
    c.drawRightString(W - 20 * mm, y, inr(88500))
    c.save()

    page = pdfium.PdfDocument(buf.getvalue())[0]
    img = page.render(scale=2).to_pil().convert("L")
    random.seed(7)
    img = img.rotate(0.8, expand=False, fillcolor=255, resample=Image.BICUBIC)
    px = img.load()
    for _ in range(9000):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice([170, 200, 225])
    img = img.filter(ImageFilter.GaussianBlur(0.6)).point(lambda v: min(255, int(v * 0.97 + 6)))
    img.convert("RGB").save(str(path), "PDF", resolution=144)


if __name__ == "__main__":
    happy(OUT / "01_happy_path_sgp.pdf")
    split_overrun(OUT / "02_split_invoice_over_po_balance.pdf")
    duplicate(OUT / "03_near_duplicate_nimbus.pdf")
    no_po(OUT / "04_no_po_tax_inclusive_deccan.pdf")
    scanned(OUT / "05_scanned_missing_invoice_no.pdf")
    print("Samples written to", OUT)
