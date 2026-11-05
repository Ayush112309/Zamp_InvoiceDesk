from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from make_samples import inr

W, H = A4
import os
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "test-bills") + os.sep

def bill(path, seller, gstin, inv_no, inv_date, order_no, items, po=None, note=None):
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(15*mm, H-18*mm, seller)
    c.setFont("Helvetica", 8.5)
    c.drawString(15*mm, H-23*mm, "Warehouse: Plot 12, KIADB Industrial Area, Hoskote, Bengaluru 562114, Karnataka")
    c.drawString(15*mm, H-27.5*mm, f"GSTIN: {gstin}    PAN: {gstin[2:12]}")
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(W-15*mm, H-23*mm, "Tax Invoice/Bill of Supply/Cash Memo")
    c.setFont("Helvetica", 8.5)
    c.drawRightString(W-15*mm, H-27.5*mm, "(Original for Recipient)")
    c.line(15*mm, H-31*mm, W-15*mm, H-31*mm)
    y = H-38*mm
    c.setFont("Helvetica-Bold", 9); c.drawString(15*mm, y, "Billing Address:")
    c.drawString(110*mm, y, "Order details")
    c.setFont("Helvetica", 9)
    for i, ln in enumerate(["Kaveri Home Appliances Ltd", "Plot 7, Gachibowli, Hyderabad 500032", "Telangana, IN", "State/UT Code: 36"]):
        c.drawString(15*mm, y-12-i*11, ln)
    right = [f"Order Number: {order_no}", f"Invoice Number : {inv_no}", f"Invoice Date : {inv_date}", "Place of supply: TELANGANA"]
    if po: right.append(f"Buyer PO Ref: {po}")
    for i, ln in enumerate(right):
        c.drawString(110*mm, y-12-i*11, ln)
    y -= 85
    cols = ["Sl.", "Description", "Unit Price", "Qty", "Net Amount", "Tax Rate", "Tax Type", "Tax Amount", "Total"]
    xs = [15, 21, 86, 104, 113, 134, 147, 160, 179]
    c.setFillColor(colors.HexColor("#e9ecef")); c.rect(14*mm, y-4, W-28*mm, 15, fill=1, stroke=0); c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 7.5)
    for col, x in zip(cols, xs): c.drawString(x*mm, y, col)
    c.setFont("Helvetica", 7.5)
    y -= 16
    net_t = tax_t = 0
    for n, (desc, price, qty) in enumerate(items, 1):
        net = price*qty; tax = round(net*0.18, 2); net_t += net; tax_t += tax
        vals = [str(n), desc, inr(price), str(qty), inr(net), "18%", "IGST", inr(tax), inr(net+tax)]
        for v, x in zip(vals, xs): c.drawString(x*mm, y, v)
        y -= 13
    c.line(15*mm, y+4, W-15*mm, y+4)
    y -= 12
    c.setFont("Helvetica", 9)
    for label, val in [("Taxable Value", net_t), ("IGST @ 18%", tax_t)]:
        c.drawString(130*mm, y, label); c.drawRightString(W-15*mm, y, inr(val)); y -= 13
    c.setFont("Helvetica-Bold", 10)
    c.drawString(130*mm, y, "Grand Total"); c.drawRightString(W-15*mm, y, inr(net_t+tax_t))
    c.setFont("Helvetica", 8)
    y -= 26
    if note: c.drawString(15*mm, y, note); y -= 12
    c.drawString(15*mm, y, "Whether tax is payable under reverse charge - No")
    c.drawString(15*mm, 20*mm, f"For {seller}:  Authorized Signatory")
    c.save()
    return net_t, tax_t, net_t+tax_t

r = {}
r["A"] = bill(OUT + "A_unknown_vendor_kairos.pdf", "Kairos Electronics LLP", "07AAKFK3321D1Z8", "KEL-DL-25-00871",
              "03.11.2026", "408-5521873-1190452", [("USB-C docking station, 11-in-1", 8400, 4)])
r["B"] = bill(OUT + "B_marketplace_no_po_brightline.pdf", "Brightline Retail Pvt Ltd", "29AABCB7712H1Z4", "BLR1-1127",
              "02.11.2026", "406-7733190-5562811", [("Wireless keyboard and mouse combo", 2100, 20)])
r["C"] = bill(OUT + "C_within_tolerance_brightline.pdf", "Brightline Retail Pvt Ltd", "29AABCB7712H1Z4", "BLR1-1131",
              "04.11.2026", "406-1120934-8841207", [("Business laptop 14-inch, 16GB RAM, 512GB SSD", 60900, 2)], po="PO-4508",
              note="Price revised by vendor after PO date due to memory cost increase.")
r["D"] = bill(OUT + "D_bank_change_gstin_mismatch_brightline.pdf", "Brightline Retail Pvt Ltd", "29AABCB7712H1Z9", "BLR1-1140",
              "05.11.2026", "406-9902211-3307765", [("Laptop backpack, water resistant", 1850, 10)], po="PO-4509",
              note="Note: our bank account has changed. Please pay to Axis Bank A/c 922020058813309, IFSC UTIB0004012.")
for k, v in r.items(): print(k, v)
