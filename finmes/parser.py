"""Parse Indian bank transaction SMS into structured records.

Deterministic by design: a message either matches a known pattern or returns
None. No guessing — unmatched messages go to a review queue downstream.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass
class Txn:
    date: date
    amount: Decimal
    direction: str  # "debit" | "credit"
    counterparty: str | None
    account: str | None
    channel: str  # "upi" | "card" | "neft" | "imps" | "autopay" | "transfer" | "other"
    ref: str | None = None
    balance: Decimal | None = None
    currency: str = "INR"
    raw: str = ""


# Matches 29-Jun-26, 03-DEC-25, 06Jun26, 02/07/26
DATE = r"(?P<date>\d{1,2}(?:-[A-Za-z]{3}-|[A-Za-z]{3}|/\d{2}/)\d{2})"
AMT = r"(?P<amt>[\d,]+(?:\.\d+)?)"

# Messages that mention money but move none: OTPs, declines, mandate lifecycle
# notices, limit/application admin. Explicit so they never reach the review queue.
SKIP_RE = re.compile(
    r"\bOTP\b|One.?Time Password|will be debited|mandate is successfully revoked|has requested money"
    r"|AutoPay request|declined|re-set the limit|increase the Credit Limit"
    r"|successfully created towards|is scheduled on|has been activated"
    r"|check your billed|application|e-Sign|not yet active"
    # "successfully processed" duplicates the "received payment ... & credited"
    # SMS for the same card payment — keep only the latter.
    # "missed call(s)" = Airtel missed-call alerts only; a bare "missed call"
    # also matches the "convert this txn to EMI give a missed call" footer on
    # real ICICI card spends.
    r"|successfully processed|E-statement|Statement is sent|missed call\(s\)"
    r"|missed call for instant information"
    # Payment-gateway receipts duplicate the card-spend SMS for the same txn.
    r"|HDFC Bank SmartGATEWAY"
    r"|Accepted in PPS|alerts will now be sent|biometric|cooling period"
    r"|UPI Registration|activating ICICI Bank iMobile|not debited|is inactive",
    re.IGNORECASE,
)

# (regex, direction, channel) — first match wins, so more specific patterns first.
PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; ZEPTOMARKETPLAC credited. UPI:203007126186.
    (
        re.compile(
            rf"Acct (?P<acct>\S+) debited for Rs {AMT} on {DATE}; (?P<cp>.+?) credited\. UPI:(?P<ref>\d+)"
        ),
        "debit",
        "upi",
    ),
    # Dear Customer, Acct XX973 is credited with Rs 100000.00 on 01-Jul-26 from Unnat Sharma. UPI:654827784288
    (
        re.compile(
            rf"Acct (?P<acct>\S+) is credited with Rs {AMT} on {DATE} from (?P<cp>.+?)\. UPI:(?P<ref>\d+)"
        ),
        "credit",
        "upi",
    ),
    # ICICI Bank Account XX973 is credited with Rs 37,500.00 on 01-Jul-26 by Account linked to
    # mobile number XXXXX03558. IMPS Ref. no. 618219528938.
    (
        re.compile(
            rf"Account (?P<acct>\S+) is credited with Rs {AMT} on {DATE} by (?P<cp>.+?)\. IMPS Ref\. no\. (?P<ref>\d+)"
        ),
        "credit",
        "imps",
    ),
    # ICICI Bank Account XX973 credited:Rs. 24,946.00 on 29-Jun-26. Info NEFT-CMS...-SHIV N.
    # Available Balance is Rs. 45,183.84.
    (
        re.compile(
            rf"Account (?P<acct>\S+) credited:Rs\.? {AMT} on {DATE}\. Info NEFT-(?P<ref>\S+?)-(?P<cp>.+?)\."
            rf" Available Balance is Rs\.? (?P<bal>[\d,]+(?:\.\d+)?)"
        ),
        "credit",
        "neft",
    ),
    # Rs 75.00 debited from ICICI Bank Savings Account XX973 on 30-Nov-25 towards APPLE MEDIA SER for ...
    (
        re.compile(
            rf"Rs\.? {AMT} debited from ICICI Bank Savings Account (?P<acct>\S+) on {DATE} towards (?P<cp>.+?) for "
        ),
        "debit",
        "autopay",
    ),
    # ICICI Bank Acc XX973 debited Rs. 17,448.77 on 03-Dec-25 InfoBIL*INFT*0011.Avl Bal Rs. 19,050.40.
    (
        re.compile(
            rf"Acc (?P<acct>\S+) debited Rs\.? {AMT} on {DATE} Info(?P<cp>.+?)\.\s*Avl Bal Rs\.? (?P<bal>[\d,]+(?:\.\d+)?)"
        ),
        "debit",
        "other",
    ),
    # Rs. 403.70 debited from ICICI Bank Acc XX973 on 30-Nov-25 VIN*PlayStati. Bal Rs. 7,869.94.
    (
        re.compile(
            rf"Rs\.? {AMT} debited from ICICI Bank Acc (?P<acct>\S+) on {DATE} (?P<cp>.+?)\. Bal Rs\.? (?P<bal>[\d,]+(?:\.\d+)?)"
        ),
        "debit",
        "other",
    ),
    # Payment of INR 17,448.77 has been received on your ICICI Bank Credit Card Account 4xxx3002 on 03-DEC-25.
    # Payment of Rs 22,680.00 has been received on your ICICI Bank Credit Card XX3002 through Bharat Bill Payment System on 30-JAN-26.
    (
        re.compile(
            rf"Payment of (?:INR|Rs\.?) {AMT} has been received on your ICICI Bank Credit Card (?:Account )?(?P<acct>\S+?)(?: through [\w ]+?)? on {DATE}"
        ),
        "credit",
        "card",
    ),
    # INR 1,168.00 spent using ICICI Bank Card XX3002 on 07-Dec-25 on DECATHLON. Avl Limit: INR 43,832.00.
    # Rs 5,125.00 spent on ICICI Bank Card XX3002 on 16-Dec-25 at Amazon Pay Indi. Avl Lmt: Rs 37,278.75.
    (
        re.compile(
            rf"(?:INR|Rs\.?) {AMT} spent (?:using|on) ICICI Bank Card (?P<acct>\S+) on {DATE} (?:on|at) (?P<cp>.+?)\. Avl L(?:imi|m)t:"
        ),
        "debit",
        "card",
    ),
    # Rs.525.00 spent on your SBI Credit Card ending 5250 at AMAZONIN on 02/07/26.
    (
        re.compile(
            rf"Rs\.?{AMT} spent on your SBI Credit Card ending (?P<acct>\d+) at (?P<cp>\S+) on {DATE}"
        ),
        "debit",
        "card",
    ),
    # Dear UPI user A/C X1583 debited by 3358.20 on date 06Jun26 trf to District by Zoma Refno 652325120798
    (
        re.compile(
            rf"A/C (?P<acct>\S+) debited by {AMT} on date {DATE} trf to (?P<cp>.+?) Refno (?P<ref>\d+)"
        ),
        "debit",
        "upi",
    ),
    # Dear SBI User, your A/c X1583-credited by Rs.35000 on 28May26 transfer from DINESH ... Ref No 306967941699
    (
        re.compile(
            rf"A/c (?P<acct>\w+)-credited by Rs\.?{AMT} on {DATE} transfer from (?P<cp>.+?) Ref No (?P<ref>\d+)"
        ),
        "credit",
        "transfer",
    ),
    # SGD1.38 spent on your SBI Credit Card ending 5250 at ORACLESINGAPORE on 06/06/26.
    # (USD 0.87 is rendered "USD.87" — amount may start with the decimal point)
    (
        re.compile(
            rf"\b(?P<ccy>[A-Z]{{3}})(?P<amt>[\d,]*\.?\d+) spent on your SBI Credit Card ending (?P<acct>\d+) at (?P<cp>\S+) on {DATE}"
        ),
        "debit",
        "card",
    ),
    # We have received payment of Rs.49,869.43 via UPI & the same has been credited to
    # your SBI Credit Card. (no date in body — uses the SMS timestamp)
    (
        re.compile(
            rf"received payment of Rs\.?{AMT} via \w+ & the same has been credited to your SBI Credit Card"
        ),
        "credit",
        "card",
    ),
    # Your Ac x1583 debited Rs.37,500.00 for transfer to UNNAT Ac x9973 dt 01.07.26 Ref 618219528938.
    (
        re.compile(
            rf"Your Ac (?P<acct>\w+) debited Rs\.?{AMT} for transfer to (?P<cp>.+?) dt (?P<date>\d{{2}}\.\d{{2}}\.\d{{2}}) Ref (?P<ref>\d+)"
        ),
        "debit",
        "transfer",
    ),
    # Your A/C XXXXX261583 Credited INR 777.00 on 21/02/26 -Deposit by transfer from Mr. UnnatSharma. Avl Bal INR 29,314.47-SBI
    (
        re.compile(
            rf"Your A/C (?P<acct>\S+) Credited INR {AMT} on {DATE} -Deposit by transfer from (?P<cp>.+?)\. Avl Bal(?:ance)? INR (?P<bal>[\d,]+(?:\.\d+)?)"
        ),
        "credit",
        "transfer",
    ),
    # Your A/C XXXXX261583 Debited INR 1,00,000.00 on 01/07/26 -Transferred to Mr. UnnatSharma. Avl Balance INR 1,94,455.92-SBI
    (
        re.compile(
            rf"Your A/C (?P<acct>\S+) Debited INR {AMT} on {DATE} -Transferred to (?P<cp>.+?)\. Avl Bal(?:ance)? INR (?P<bal>[\d,]+(?:\.\d+)?)"
        ),
        "debit",
        "transfer",
    ),
    # Rs.150000.00 Debited to SB-xxx5472 AcBal:151790.00 CLRBal: 151790.00 [Mr Unnat ] CBO-CHENNAI on 22-06-2026 13:05:41.IOB.
    (
        re.compile(
            rf"Rs\.?{AMT} Debited to (?P<acct>\S+) AcBal:(?P<bal>[\d,]+(?:\.\d+)?).*? on (?P<date>\d{{2}}-\d{{2}}-\d{{4}})"
        ),
        "debit",
        "other",
    ),
    # Your a/c XXXXX72 debited for payee Utsav Sharma for Rs. 1.00 on 2026-06-05, ref 307439154207.
    (
        re.compile(
            rf"a/c (?P<acct>\S+) debited for payee (?P<cp>.+?) for Rs\.? ?{AMT} on (?P<date>\d{{4}}-\d{{2}}-\d{{2}}), ref (?P<ref>\d+)"
        ),
        "debit",
        "upi",
    ),
    # Dear Customer, INR 1,50,434.00 credited to your A/c No XX1583 on 01/06/2026
    # through NEFT with UTR IDFB6152M7750755 by RZPX PVT LTD PAYROLL..., INFO: ...
    (
        re.compile(
            rf"INR {AMT} credited to your A/c No (?P<acct>\S+) on (?P<date>\d{{2}}/\d{{2}}/\d{{4}}) "
            rf"through NEFT with UTR (?P<ref>\S+) by (?P<cp>.+?),"
        ),
        "credit",
        "neft",
    ),
    # It's Cashback Time! ... Rs. 2378 has been credited to your CASHBACK SBI Card account for the month of Mar'26
    # (no date in body — uses the SMS timestamp)
    (
        re.compile(
            rf"Rs\.? ?{AMT} has been credited to your CASHBACK SBI Card account"
        ),
        "credit",
        "card",
    ),
    # ...ICICI Bank Account XXX973 has been credited with Rs 60.00 on 23-Apr-26 as reversal of transaction with UPI: 912712042175.
    (
        re.compile(
            rf"Account (?P<acct>\S+) has been credited with Rs\.? {AMT} on {DATE} as reversal"
        ),
        "credit",
        "upi",
    ),
]

_DATE_FORMATS = (
    "%d-%b-%y", "%d%b%y", "%d/%m/%y", "%d.%m.%y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y",
)


def _parse_date(s: str) -> date:
    s = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", s)  # 15th Mar 2026 -> 15 Mar 2026
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.title(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {s!r}")


def _amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def parse(sender: str, text: str, received: date | None = None) -> Txn | None:
    """Return a Txn for a recognized transaction message, else None.

    `received` is the SMS delivery date, used as fallback for the few formats
    that carry no date in the body (those return None without it).
    """
    body = " ".join(text.split())
    if SKIP_RE.search(body):
        return None
    for pattern, direction, channel in PATTERNS:
        m = pattern.search(body)
        if not m:
            continue
        groups = m.groupdict()
        txn_date = _parse_date(groups["date"]) if groups.get("date") else received
        if txn_date is None:
            return None
        return Txn(
            date=txn_date,
            amount=_amount(groups["amt"]),
            direction=direction,
            counterparty=(groups.get("cp") or "").strip() or None,
            account=groups.get("acct"),
            channel=channel,
            ref=groups.get("ref"),
            balance=_amount(groups["bal"]) if groups.get("bal") else None,
            currency=groups.get("ccy") or "INR",
            raw=body,
        )
    return None
