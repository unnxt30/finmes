"""Parser tests. Every message body here is a real SMS from samples/bank_sms.txt
(amounts/names included — the samples dir is gitignored, these fixtures are the
formats we committed to supporting)."""

from datetime import date
from decimal import Decimal

from finmes.parser import parse


def test_icici_upi_debit_with_merchant():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; ZEPTOMARKETPLAC "
        "credited. UPI:203007126186. Call 18002662 for dispute. SMS BLOCK 973 to 9215676766.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("165.00")
    assert txn.date == date(2026, 6, 29)
    assert txn.counterparty == "ZEPTOMARKETPLAC"
    assert txn.account == "XX973"
    assert txn.channel == "upi"
    assert txn.ref == "203007126186"


def test_icici_upi_credit_from_person():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Dear Customer, Acct XX973 is credited with Rs 100000.00 on 01-Jul-26 from "
        "Unnat Sharma. UPI:654827784288-ICICI Bank.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("100000.00")
    assert txn.date == date(2026, 7, 1)
    assert txn.counterparty == "Unnat Sharma"
    assert txn.channel == "upi"
    assert txn.ref == "654827784288"


def test_icici_neft_credit():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "ICICI Bank Account XX973 credited:Rs. 24,946.00 on 29-Jun-26. Info "
        "NEFT-CMS1802611996782-SHIV N. Available Balance is Rs. 45,183.84.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("24946.00")
    assert txn.counterparty == "SHIV N"
    assert txn.channel == "neft"
    assert txn.balance == Decimal("45183.84")


def test_icici_imps_credit():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "ICICI Bank Account XX973 is credited with Rs 37,500.00 on 01-Jul-26 by "
        "Account linked to mobile number XXXXX03558. IMPS Ref. no. 618219528938.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("37500.00")
    assert txn.counterparty == "Account linked to mobile number XXXXX03558"
    assert txn.channel == "imps"
    assert txn.ref == "618219528938"


def test_icici_autopay_debit():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Rs 75.00 debited from ICICI Bank Savings Account XX973 on 30-Nov-25 towards "
        "APPLE MEDIA SER for Create Mandate AutoPay Retrieval Ref no 123456789012.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("75.00")
    assert txn.date == date(2025, 11, 30)
    assert txn.counterparty == "APPLE MEDIA SER"
    assert txn.channel == "autopay"


def test_icici_info_debit_with_balance():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "ICICI Bank Acc XX973 debited Rs. 17,448.77 on 03-Dec-25 InfoBIL*INFT*0011."
        "Avl Bal Rs. 19,050.40.To dispute call 18002662 or SMS BLOCK 973 to 9215676766.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("17448.77")
    assert txn.counterparty == "BIL*INFT*0011"
    assert txn.balance == Decimal("19050.40")


def test_icici_vin_debit_with_balance():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Rs. 403.70 debited from ICICI Bank Acc XX973 on 30-Nov-25 VIN*PlayStati. "
        "Bal Rs. 7,869.94. If not you call 18002662 or SMS BLOCK 973 to 9215676766.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("403.70")
    assert txn.counterparty == "VIN*PlayStati"
    assert txn.balance == Decimal("7869.94")


def test_icici_credit_card_payment_received():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Dear Customer, Payment of INR 17,448.77 has been received on your ICICI Bank "
        "Credit Card Account 4xxx3002 on 03-DEC-25.Thank you.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("17448.77")
    assert txn.date == date(2025, 12, 3)
    assert txn.account == "4xxx3002"
    assert txn.channel == "card"


def test_sbi_card_spend():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "Rs.525.00 spent on your SBI Credit Card ending 5250 at AMAZONIN on 02/07/26. "
        "Trxn. not done by you? Report at https://sbicard.com/Dispute",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("525.00")
    assert txn.date == date(2026, 7, 2)
    assert txn.counterparty == "AMAZONIN"
    assert txn.account == "5250"
    assert txn.channel == "card"


def test_sbi_upi_debit():
    txn = parse(
        "SBIUPI-S(smsft_fi)",
        "Dear UPI user A/C X1583 debited by 3358.20 on date 06Jun26 trf to District "
        "by Zoma Refno 652325120798 If not u? call-1800111109 for other services-18001234-SBI",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("3358.20")
    assert txn.date == date(2026, 6, 6)
    assert txn.counterparty == "District by Zoma"
    assert txn.account == "X1583"
    assert txn.channel == "upi"
    assert txn.ref == "652325120798"


def test_sbi_transfer_credit():
    txn = parse(
        "SBIUPI-S(smsft_fi)",
        "Dear SBI User, your A/c X1583-credited by Rs.35000 on 28May26 transfer from "
        "DINESH CHANDRA SHARMA Ref No 306967941699 -SBI",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("35000")
    assert txn.date == date(2026, 5, 28)
    assert txn.counterparty == "DINESH CHANDRA SHARMA"
    assert txn.ref == "306967941699"


def test_otp_is_skipped():
    txn = parse(
        "SBICRD-T(smsft)",
        "370012 is the OTP for Trxn. of INR 525.00 at AMAZON PAY with your credit card "
        "ending 5250. OTP is valid for 10 mins. Do not share it with anyone - SBI Card",
    )
    assert txn is None


def test_mandate_revoked_is_skipped():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Dear Customer, your AutoPay mandate is successfully revoked towards Anchorage "
        "Techn for Rs 299.00, RRN 570009113182-ICICI Bank.",
    )
    assert txn is None


def test_promo_is_skipped():
    txn = parse(
        "SWIGGY-S(smsft_or)",
        "Craving something delicious? Get flat Rs.125 OFF on your next order. "
        "Use code TASTY125. Order now!",
    )
    assert txn is None


def test_sbi_card_fx_spend():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "SGD1.38 spent on your SBI Credit Card ending 5250 at ORACLESINGAPORE on "
        "06/06/26. Trxn. not done by you? Report at https://sbicard.com/Dispute",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("1.38")
    assert txn.currency == "SGD"
    assert txn.counterparty == "ORACLESINGAPORE"
    assert txn.date == date(2026, 6, 6)


def test_sbi_card_fx_spend_subunit_amount():
    # SBI renders USD 0.87 as "USD.87"
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "USD.87 spent on your SBI Credit Card ending 5250 at NAMECHEAP5BOOB2 on "
        "12/06/26. Trxn. not done by you? Report at https://sbicard.com/Dispute",
    )
    assert txn is not None
    assert txn.amount == Decimal("0.87")
    assert txn.currency == "USD"


def test_inr_txn_has_inr_currency():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "Rs.525.00 spent on your SBI Credit Card ending 5250 at AMAZONIN on 02/07/26. "
        "Trxn. not done by you? Report at https://sbicard.com/Dispute",
    )
    assert txn is not None
    assert txn.currency == "INR"


def test_sbi_card_payment_received_uses_sms_timestamp():
    # This format carries no date in the body -> falls back to `received`
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "We have received payment of Rs.49,869.43 via UPI & the same has been credited "
        "to your SBI Credit Card. Your available limit is Rs.58,000.00.",
        received=date(2026, 6, 3),
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("49869.43")
    assert txn.channel == "card"
    assert txn.date == date(2026, 6, 3)


def test_sbi_card_payment_received_without_timestamp_is_skipped():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "We have received payment of Rs.49,869.43 via UPI & the same has been credited "
        "to your SBI Credit Card. Your available limit is Rs.58,000.00.",
    )
    assert txn is None


def test_sbi_yono_transfer_debit():
    txn = parse(
        "SBYONO-S(smsft_fi)",
        "Your Ac x1583 debited Rs.37,500.00 for transfer to UNNAT Ac x9973 dt 01.07.26 "
        "Ref 618219528938. If not done by you, call 1800111109. YONO SBI",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("37500.00")
    assert txn.counterparty == "UNNAT Ac x9973"
    assert txn.account == "x1583"
    assert txn.date == date(2026, 7, 1)
    assert txn.ref == "618219528938"


def test_one_time_password_variant_is_skipped():
    txn = parse(
        "ICICIO-T(smsft)",
        "739074 is One-Time Password for INR 5125.00 transaction towards AMAZON PAY "
        "using ICICI Bank Credit Card XX3002. OTPs are SECRET. DO NOT disclose",
    )
    assert txn is None


def test_declined_txn_is_skipped():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "Dear SBI Cardholder, Txn of Rs.10,695.50 on Card ending with XX5250 at "
        "DISTRICTMOVIETICKET on 19-02-26 has been declined. Pls call at 18601801290.",
    )
    assert txn is None


def test_limit_change_notice_is_skipped():
    txn = parse(
        "SBICRD-S(smsft)",
        "Dear SBI Cardholder, your request to re-set the limit for Domestic online "
        "Trxns. on your SBI Credit Card ending 5250 has been accepted and is set at Rs. 50000.",
    )
    assert txn is None


def test_future_autopay_notice_is_skipped():
    txn = parse(
        "ICICIT-T(smsft_fi)",
        "ICICI Bank SAVINGS Account XX973 will be debited for Rs 75.00 on 30-Mar-26 "
        "towards Autopay for APPLE MEDIA SER, Create Mandate, Unique Mandate Number ABC.",
    )
    assert txn is None


def test_icici_card_spend_using_variant():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "INR 1,168.00 spent using ICICI Bank Card XX3002 on 07-Dec-25 on DECATHLON. "
        "Avl Limit: INR 43,832.00. If not you, call 1800 2662/SMS BLOCK 3002 to 9215676766.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("1168.00")
    assert txn.date == date(2025, 12, 7)
    assert txn.counterparty == "DECATHLON"
    assert txn.account == "XX3002"
    assert txn.channel == "card"


def test_icici_card_spend_at_variant_strips_counterparty():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Rs 5,125.00 spent on ICICI Bank Card XX3002 on 16-Dec-25 at Amazon Pay Indi. "
        "Avl Lmt: Rs 37,278.75. To dispute, call 18002662/SMS BLOCK 3002 to 9215676766.",
    )
    assert txn is not None
    assert txn.amount == Decimal("5125.00")
    assert txn.counterparty == "Amazon Pay Indi"
    assert txn.channel == "card"


def test_icici_cc_payment_via_bbps():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Payment of Rs 22,680.00 has been received on your ICICI Bank Credit Card "
        "XX3002 through Bharat Bill Payment System on 30-JAN-26.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("22680.00")
    assert txn.date == date(2026, 1, 30)
    assert txn.account == "XX3002"


def test_cbs_sbi_credit():
    txn = parse(
        "CBSSBI-S(smsft_fi)",
        "Your A/C XXXXX261583 Credited INR 777.00 on 21/02/26 -Deposit by transfer "
        "from Mr. UnnatSharma. Avl Bal INR 29,314.47-SBI",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("777.00")
    assert txn.date == date(2026, 2, 21)
    assert txn.counterparty == "Mr. UnnatSharma"
    assert txn.balance == Decimal("29314.47")


def test_cbs_sbi_debit_lakh_grouping():
    txn = parse(
        "CBSSBI-S(smsft_fi)",
        "Your A/C XXXXX261583 Debited INR 1,00,000.00 on 01/07/26 -Transferred to "
        "Mr. UnnatSharma. Avl Balance INR 1,94,455.92-SBI",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("100000.00")
    assert txn.counterparty == "Mr. UnnatSharma"
    assert txn.balance == Decimal("194455.92")


def test_iob_cheque_debit():
    txn = parse(
        "IOBCHN-S(smsft_fi)",
        "Rs.150000.00 Debited to SB-xxx5472 AcBal:151790.00 CLRBal: 151790.00 "
        "[Mr Unnat ] CBO-CHENNAI on 22-06-2026 13:05:41.IOB.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("150000.00")
    assert txn.date == date(2026, 6, 22)
    assert txn.account == "SB-xxx5472"
    assert txn.balance == Decimal("151790.00")


def test_iob_payee_debit():
    txn = parse(
        "IOBCHN-S(smsft_fi)",
        "Your a/c XXXXX72 debited for payee Utsav Sharma for Rs. 1.00 on 2026-06-05, "
        "ref 307439154207.If not you, report to your bank immediately.",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("1.00")
    assert txn.date == date(2026, 6, 5)
    assert txn.counterparty == "Utsav Sharma"
    assert txn.ref == "307439154207"


def test_sbi_cashback_credit_uses_sms_timestamp():
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "It's Cashback Time! Happy to share that Rs. 2378 has been credited to your "
        "CASHBACK SBI Card account for the month of Mar'26 - SBI Card",
        received=date(2026, 4, 2),
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("2378")
    assert txn.date == date(2026, 4, 2)


def test_icici_upi_reversal_credit():
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Dear Customer, your ICICI Bank Account XXX973 has been credited with Rs 60.00 "
        "on 23-Apr-26 as reversal of transaction with UPI: 912712042175.",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("60.00")
    assert txn.date == date(2026, 4, 23)


def test_sbi_card_payment_processed_is_skipped_as_duplicate():
    # "successfully processed" precedes "received payment ... & credited" for the
    # same payment — parsing both would double-count.
    txn = parse(
        "SBICRD-S(smsft_fi)",
        "Dear SBI Cardholder, payment of Rs. 25844.42 for your SBI Credit Card has "
        "been successfully processed. ref no : CSBIPSH1DFZ3CE.",
    )
    assert txn is None


def test_estatement_notice_is_skipped():
    txn = parse(
        "MYSBIC-S(smsft_rm)",
        "E-statement of SBI Credit Card ending XX50 dated 19/03/2026 has been mailed. "
        "If not received, SMS ENRS to 5676791. Total Amt Due Rs 2545.00.",
    )
    assert txn is None


def test_icici_card_spend_with_emi_footer_is_parsed():
    # The "give a missed call" EMI footer must not trip the missed-call skip.
    txn = parse(
        "ICICIT-S(smsft_fi)",
        "Rs 5,125.00 spent on ICICI Bank Card XX3002 on 16-Dec-25 at Amazon Pay Indi. "
        "Avl Lmt: Rs 37,278.75. To dispute, call 18002662/SMS BLOCK 3002 to 9215676766. "
        "To convert this txn to EMI give a missed call on 9924667667. Know more about "
        "EMI conversion at https://icici.co/ICICIT/j/2134ffce",
    )
    assert txn is not None
    assert txn.direction == "debit"
    assert txn.amount == Decimal("5125.00")
    assert txn.date == date(2025, 12, 16)
    assert txn.counterparty == "Amazon Pay Indi"
    assert txn.account == "XX3002"


def test_airtel_missed_call_notice_still_skipped():
    txn = parse(
        "AIRMCA-S(smsft_or)",
        "7 missed call(s) received on 20-12-2025 while your Airtel number was not "
        "reachable. Get details of your missed calls ONLY on Airtel Thanks App "
        "https://i.airtel.in/1_MissedCall",
    )
    assert txn is None


def test_sbi_neft_credit_with_utr():
    txn = parse(
        "SBIPSG-S(smsft_fi)",
        "Dear Customer, INR 1,50,434.00 credited to your A/c No XX1583 on 01/06/2026 "
        "through NEFT with UTR IDFB6152M7750755 by RZPX PVT LTD PAYROLL COMPLIANCE "
        "ESCROW ACCOUNT, INFO: BATCHID:0031 SALARY DEEPSOURCE PRIVATE LIMI-SBI",
    )
    assert txn is not None
    assert txn.direction == "credit"
    assert txn.amount == Decimal("150434.00")
    assert txn.date == date(2026, 6, 1)
    assert txn.counterparty == "RZPX PVT LTD PAYROLL COMPLIANCE ESCROW ACCOUNT"
    assert txn.account == "XX1583"
    assert txn.channel == "neft"
    assert txn.ref == "IDFB6152M7750755"


def test_sbi_card_info_missed_call_promo_is_skipped():
    from finmes.parser import SKIP_RE

    body = (
        "Give a missed call for instant information related to your SBI Credit Card. "
        "A/c Summary: 9619742906"
    )
    assert parse("SBICRD-S(smsft)", body) is None
    assert SKIP_RE.search(body)  # deliberate skip, not review-queue noise


def test_hdfc_gateway_receipt_is_skipped():
    # Merchant payment-gateway receipt duplicates the card-spend SMS for the
    # same transaction — must not become a second journal entry.
    txn = parse(
        "HDFCBK-S(smsft_fi)",
        "INR 2100.00 paid successfully to YUTAI PRIME FOODS PRIVATE LIMITED on "
        "15th Mar 2026, 13:21:17 IST. Get Receipt: https://1.hdfc.bank.in/HDFCBK/a/y0mFaH "
        "-HDFC Bank SmartGATEWAY",
    )
    assert txn is None
