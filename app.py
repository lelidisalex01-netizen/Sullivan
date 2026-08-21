import os, re, sqlite3, hashlib, json, zipfile, shutil, uuid, secrets, hmac, base64
import textwrap
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timedelta
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from io import BytesIO
from dotenv import load_dotenv, set_key
from openai import OpenAI
import stripe
import requests
from pydantic import BaseModel, Field

st.set_page_config(page_title="Sullivan V19.6", page_icon="S", layout="wide")


# Sullivan V19 visual system: calm navy + blue + mint + warm amber.
APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
DB_PATH = APP_DIR / "sullivan.db"
DOC_DIR = APP_DIR / "documents"
EXPORT_DIR = APP_DIR / "exports"
DOC_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)
load_dotenv(ENV_PATH)

MODEL = "gpt-5.6-luna"

# Québec standard taxable-supply rates used by the V10.6 test engine.
# These rates should ultimately become jurisdiction/date-driven configuration.
GST_RATE_QC = 0.05
QST_RATE_QC = 0.09975
QC_COMBINED_RATE = GST_RATE_QC + QST_RATE_QC

DEFAULT_ACCOUNTS = {
    "1000 Bank": ("Asset", "Debit", "Money in bank"),
    "1010 Savings": ("Asset", "Debit", "Money in bank"),
    "1020 Undeposited Funds": ("Asset", "Debit", "Money in bank"),
    "1100 Accounts Receivable": ("Asset", "Debit", "Receivable"),
    "1200 GST/HST Receivable": ("Asset", "Debit", "Tax"),
    "1210 QST Receivable": ("Asset", "Debit", "Tax"),
    "1300 Prepaid Expenses": ("Asset", "Debit", "Current Asset"),
    "1500 Equipment": ("Asset", "Debit", "Fixed Asset"),
    "1510 Computer Equipment": ("Asset", "Debit", "Fixed Asset"),
    "1520 Vehicles": ("Asset", "Debit", "Fixed Asset"),
    "1590 Accumulated Depreciation": ("Asset", "Credit", "Contra Asset"),
    "2000 Accounts Payable": ("Liability", "Credit", "Payable"),
    "2100 Credit Card Payable": ("Liability", "Credit", "Current Liability"),
    "2200 Loan Payable": ("Liability", "Credit", "Long-Term Liability"),
    "2250 Accrued Expenses": ("Liability", "Credit", "Current Liability"),
    "2300 GST/HST Payable": ("Liability", "Credit", "Tax"),
    "2310 QST Payable": ("Liability", "Credit", "Tax"),
    "2500 Customer Deposits": ("Liability", "Credit", "Current Liability"),
    "3000 Owner Equity": ("Equity", "Credit", "Equity"),
    "3100 Owner Draw": ("Equity", "Debit", "Equity"),
    "3200 Retained Earnings": ("Equity", "Credit", "Equity"),
    "4000 Sales Revenue": ("Revenue", "Credit", "Operating Revenue"),
    "4100 Service Revenue": ("Revenue", "Credit", "Operating Revenue"),
    "4200 Other Income": ("Revenue", "Credit", "Other Income"),
    "5000 Cost of Goods Sold": ("Expense", "Debit", "COGS"),
    "5100 Materials & Supplies": ("Expense", "Debit", "Operating Expense"),
    "5200 Office Supplies": ("Expense", "Debit", "Operating Expense"),
    "5300 Software / Subscriptions": ("Expense", "Debit", "Operating Expense"),
    "5400 Phone / Internet": ("Expense", "Debit", "Operating Expense"),
    "5500 Rent / Occupancy": ("Expense", "Debit", "Operating Expense"),
    "5600 Utilities": ("Expense", "Debit", "Operating Expense"),
    "5700 Insurance": ("Expense", "Debit", "Operating Expense"),
    "5800 Professional Fees": ("Expense", "Debit", "Operating Expense"),
    "5900 Advertising / Marketing": ("Expense", "Debit", "Operating Expense"),
    "6000 Vehicle / Fuel": ("Expense", "Debit", "Operating Expense"),
    "6100 Travel": ("Expense", "Debit", "Operating Expense"),
    "6200 Meals": ("Expense", "Debit", "Operating Expense"),
    "6300 Payroll": ("Expense", "Debit", "Operating Expense"),
    "6400 Bank / Merchant Fees": ("Expense", "Debit", "Operating Expense"),
    "6500 Interest Expense": ("Expense", "Debit", "Other Expense"),
    "6600 Repairs & Maintenance": ("Expense", "Debit", "Operating Expense"),
    "6700 Depreciation Expense": ("Expense", "Debit", "Operating Expense"),
    "6800 Bad Debt Expense": ("Expense", "Debit", "Operating Expense"),
    "6900 Other Expense": ("Expense", "Debit", "Other Expense"),
    "6999 Uncategorized Expense": ("Expense", "Debit", "Uncategorized"),
}

CATEGORY_TO_ACCOUNT = {
    "Income":"4000 Sales Revenue","Cost of Goods Sold":"5000 Cost of Goods Sold",
    "Materials & Supplies":"5100 Materials & Supplies","Office Supplies":"5200 Office Supplies",
    "Software / Subscriptions":"5300 Software / Subscriptions","Phone / Internet":"5400 Phone / Internet",
    "Rent / Occupancy":"5500 Rent / Occupancy","Utilities":"5600 Utilities","Insurance":"5700 Insurance",
    "Professional Fees":"5800 Professional Fees","Advertising / Marketing":"5900 Advertising / Marketing",
    "Vehicle / Fuel":"6000 Vehicle / Fuel","Travel / Local Transport":"6100 Travel",
    "Travel / Airfare":"6100 Travel","Travel / Lodging":"6100 Travel","Meals":"6200 Meals",
    "Payroll":"6300 Payroll","Bank / Merchant Fees":"6400 Bank / Merchant Fees",
    "Interest Expense":"6500 Interest Expense","Repairs & Maintenance":"6600 Repairs & Maintenance",
    "Possible Capital Asset":"1500 Equipment","Loan Payment / Transfer":"2200 Loan Payable",
    "Owner Contribution / Draw":"3000 Owner Equity","Uncategorized":"6999 Uncategorized Expense",
    "Needs Review":"6999 Uncategorized Expense",
}

CATEGORIES = list(CATEGORY_TO_ACCOUNT)

# Sullivan V19 membership foundation.
# Stripe is intentionally NOT connected yet. These definitions control the
# internal subscription/credit model that Stripe will activate later.
SULLIVAN_PLANS = {
    "Trial": {
        "price": 0,
        "ai_credits": 0,
        "seat_limit": 1,
        "label": "Free preview",
    },
    "Starter": {
        "price": 19,
        "ai_credits": 500,
        "seat_limit": 1,
        "label": "Solo business",
    },
    "Business": {
        "price": 49,
        "ai_credits": 2500,
        "seat_limit": 5,
        "label": "Small business team",
    },
    "Pro": {
        "price": 99,
        "ai_credits": 10000,
        "seat_limit": 15,
        "label": "Growing business",
    },
    "Accounting Firm": {
        "price": 250,
        "ai_credits": 30000,
        "seat_limit": 50,
        "label": "Accounting teams and multi-client firms",
    },
    "Enterprise": {
        "price": None,
        "ai_credits": 0,
        "seat_limit": 51,
        "label": "Custom teams with 51+ people",
    },
}

AI_CREDIT_COSTS = {
    "transaction_categorization": 1,
    "transaction_resolution": 1,
    "receipt_analysis": 2,
    "invoice_analysis": 2,
    "accounting_question": 2,
    "reconciliation_assist": 5,
    "month_end_review": 15,
}

RULES = [
    (r"\b(shell|esso|petro[- ]?canada|ultramar|chevron|exxon|mobil)\b","Vehicle / Fuel",.97),
    (r"\b(home depot|lowe'?s|rona|canac)\b","Materials & Supplies",.94),
    (r"\b(bell|rogers|telus|videotron)\b","Phone / Internet",.97),
    (r"\b(air canada|westjet|porter|delta|united|american airlines)\b","Travel / Airfare",.91),
    (r"\b(adobe|microsoft|google workspace|dropbox|slack|zoom)\b","Software / Subscriptions",.96),
]
ALWAYS_REVIEW = ["transfer","e-transfer","etransfer","wire","best buy","apple store","loan","vehicle"]

class Classification(BaseModel):
    category: str
    confidence: float = Field(ge=0, le=1)
    review_required: bool
    explanation: str
    question_for_owner: Optional[str] = None
    counterparty_name: Optional[str] = None
    counterparty_type: Optional[str] = None

class Resolution(BaseModel):
    category: str
    confidence: float = Field(ge=0, le=1)
    resolved: bool
    explanation: str
    follow_up_question: Optional[str] = None
    counterparty_name: Optional[str] = None
    counterparty_type: Optional[str] = None

# V19.3 workspace isolation.
#
# Authentication, memberships, billing and AI allowance tables remain shared.
# Every bookkeeping/accounting table is transparently routed to a physical
# workspace-specific table. This means Personal, Company A and Company B
# genuinely have separate books even though Sullivan still uses one SQLite file.
WORKSPACE_ACCOUNTING_TABLES = (
    "business_profile",
    "accounts",
    "counterparties",
    "transactions",
    "learned_rules",
    "journal_entries",
    "audit_log",
    "close_periods",
    "invoices",
    "bills",
    "manual_journals",
    "manual_journal_lines",
    "documents",
    "customers",
    "vendors",
    "invoice_payments",
    "bill_payments",
    "bank_reconciliations",
    "bank_reconciliation_items",
    "estimates",
    "estimate_lines",
    "invoice_lines",
    "bill_lines",
    "credit_notes",
    "purchase_orders",
    "purchase_order_lines",
    "recurring_invoices",
    "accounting_periods",
)

SHARED_SULLIVAN_TABLES = (
    "companies",
    "app_users",
    "company_members",
    "company_invites",
    "ai_usage",
    "ai_demo_results",
    "enterprise_quotes",
    "workspace_migrations",
)


def _workspace_storage_key():
    """
    Return the currently active bookkeeping workspace key.

    Company books:  c<company_id>
    Personal books: u<user_id>

    During startup before authentication is established, return None so schema
    bootstrap/migrations continue to operate on the legacy base tables.
    """
    try:
        company = st.session_state.get("auth_company")
        user = st.session_state.get("auth_user")
        role = st.session_state.get("auth_role")

        if company and int(company.get("company_id", 0) or 0) > 0:
            return f"c{int(company['company_id'])}"

        if user and role == "Personal":
            return f"u{int(user['id'])}"
    except Exception:
        pass

    return None


def _workspace_table_name(table_name, workspace_key=None):
    key = workspace_key if workspace_key is not None else _workspace_storage_key()
    if not key:
        return table_name
    return f"ws_{key}__{table_name}"


def _scope_sql(sql):
    """
    Rewrite bookkeeping table names to the active workspace's physical tables.

    Shared identity/billing tables are intentionally never rewritten.
    """
    key = _workspace_storage_key()
    if not key or not isinstance(sql, str):
        return sql

    scoped = sql

    # Longer names first prevents a short table name from touching a longer one.
    for table in sorted(WORKSPACE_ACCOUNTING_TABLES, key=len, reverse=True):
        physical = _workspace_table_name(table, key)
        scoped = re.sub(
            rf'(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])',
            physical,
            scoped,
        )

    return scoped


class WorkspaceCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        return super().execute(_scope_sql(sql), parameters)

    def executemany(self, sql, seq_of_parameters):
        return super().executemany(_scope_sql(sql), seq_of_parameters)

    def executescript(self, sql_script):
        return super().executescript(_scope_sql(sql_script))


class WorkspaceConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or WorkspaceCursor)

    def execute(self, sql, parameters=()):
        cur = self.cursor()
        return cur.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        cur = self.cursor()
        return cur.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        cur = self.cursor()
        return cur.executescript(sql_script)


def raw_connect():
    """Open Sullivan's SQLite database without workspace SQL rewriting."""
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA busy_timeout=15000;")
    c.execute("PRAGMA foreign_keys=ON;")
    return c


def connect():
    c = sqlite3.connect(
        DB_PATH,
        timeout=15,
        isolation_level=None,
        factory=WorkspaceConnection,
    )
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA busy_timeout=15000;")
    c.execute("PRAGMA foreign_keys=ON;")
    return c

def write(fn):
    c=connect()
    try:
        c.execute("BEGIN IMMEDIATE;"); result=fn(c); c.execute("COMMIT;"); return result
    except Exception:
        try: c.execute("ROLLBACK;")
        except: pass
        raise
    finally: c.close()

def read(q,p=()):
    c=connect()
    try: return pd.read_sql_query(q,c,params=p)
    finally: c.close()

def audit_row(c,event,etype,eid,details,actor="User"):
    c.execute("INSERT INTO audit_log(ts,event_type,entity_type,entity_id,actor,details) VALUES(?,?,?,?,?,?)",
              (datetime.now().isoformat(timespec="seconds"),event,etype,str(eid),actor,json.dumps(details)))

def log(event,etype,eid,details): write(lambda c:audit_row(c,event,etype,eid,details))

def init_db():
    def f(c):
        c.execute("""CREATE TABLE IF NOT EXISTS business_profile(
        id INTEGER PRIMARY KEY CHECK(id=1),business_name TEXT,country TEXT,region TEXT,entity_type TEXT,
        industry TEXT,fiscal_year_end TEXT,gst_registered INTEGER DEFAULT 0,qst_registered INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS accounts(
        code TEXT PRIMARY KEY,name TEXT,type TEXT,natural_balance TEXT,group_name TEXT,
        active INTEGER DEFAULT 1,system_account INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS counterparties(
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE,type TEXT,default_category TEXT,
        default_account TEXT,email TEXT,phone TEXT,notes TEXT,active INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,date TEXT,description TEXT,amount REAL,category TEXT,
        confidence REAL,review INTEGER,explanation TEXT,question TEXT,owner_answer TEXT,source TEXT,
        status TEXT,source_file TEXT,fingerprint TEXT UNIQUE,account TEXT,receipt_name TEXT,
        receipt_attached INTEGER DEFAULT 0,tax_included INTEGER DEFAULT 0,gst_amount REAL DEFAULT 0,
        qst_amount REAL DEFAULT 0,business_use_pct REAL DEFAULT 100,tax_eligible INTEGER DEFAULT 0,
        counterparty_name TEXT,counterparty_type TEXT,reconciled INTEGER DEFAULT 0,
        period_locked INTEGER DEFAULT 0,tax_reviewed INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS learned_rules(
        normalized_description TEXT PRIMARY KEY,category TEXT,times_confirmed INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS journal_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,transaction_id INTEGER,date TEXT,memo TEXT,account TEXT,
        debit REAL DEFAULT 0,credit REAL DEFAULT 0,source_type TEXT DEFAULT 'Bank',source_id TEXT,
        reversal_of INTEGER,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,event_type TEXT,entity_type TEXT,entity_id TEXT,
        actor TEXT,details TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS close_periods(
        id INTEGER PRIMARY KEY AUTOINCREMENT,period_end TEXT UNIQUE,status TEXT,checklist_json TEXT,
        locked INTEGER DEFAULT 0,closed_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_no TEXT UNIQUE,customer_name TEXT,invoice_date TEXT,
        due_date TEXT,revenue_account TEXT,subtotal REAL,gst REAL DEFAULT 0,qst REAL DEFAULT 0,total REAL,
        status TEXT DEFAULT 'Open',memo TEXT,posted INTEGER DEFAULT 0,paid_date TEXT,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,bill_no TEXT UNIQUE,vendor_name TEXT,bill_date TEXT,due_date TEXT,
        expense_account TEXT,subtotal REAL,gst REAL DEFAULT 0,qst REAL DEFAULT 0,total REAL,
        status TEXT DEFAULT 'Open',memo TEXT,posted INTEGER DEFAULT 0,paid_date TEXT,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS manual_journals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,journal_no TEXT UNIQUE,journal_date TEXT,memo TEXT,
        posted INTEGER DEFAULT 0,reversal_of TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS manual_journal_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,journal_id INTEGER,account TEXT,debit REAL DEFAULT 0,
        credit REAL DEFAULT 0,FOREIGN KEY(journal_id) REFERENCES manual_journals(id) ON DELETE CASCADE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT,entity_id TEXT,file_name TEXT,
        stored_path TEXT,uploaded_at TEXT,notes TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,email TEXT,phone TEXT,
        address1 TEXT,address2 TEXT,city TEXT,province_state TEXT,postal_zip TEXT,country TEXT,
        payment_terms_days INTEGER DEFAULT 30,notes TEXT,active INTEGER DEFAULT 1,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vendors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,email TEXT,phone TEXT,
        address1 TEXT,address2 TEXT,city TEXT,province_state TEXT,postal_zip TEXT,country TEXT,
        payment_terms_days INTEGER DEFAULT 30,notes TEXT,active INTEGER DEFAULT 1,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS invoice_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_id INTEGER,payment_date TEXT,amount REAL,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bill_payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,bill_id INTEGER,payment_date TEXT,amount REAL,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bank_reconciliations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,statement_name TEXT,statement_date TEXT,
        opening_balance REAL DEFAULT 0,ending_balance REAL DEFAULT 0,
        status TEXT DEFAULT 'Open',created_at TEXT,completed_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bank_reconciliation_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,reconciliation_id INTEGER,
        journal_entry_id INTEGER,cleared INTEGER DEFAULT 0,created_at TEXT,
        UNIQUE(reconciliation_id,journal_entry_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS estimates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,estimate_no TEXT UNIQUE,customer_name TEXT,
        estimate_date TEXT,expiry_date TEXT,status TEXT DEFAULT 'Draft',notes TEXT,
        subtotal REAL DEFAULT 0,gst REAL DEFAULT 0,qst REAL DEFAULT 0,total REAL DEFAULT 0,
        converted_invoice_id INTEGER,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS estimate_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,estimate_id INTEGER,description TEXT,
        quantity REAL,rate REAL,taxable INTEGER DEFAULT 1,line_total REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS invoice_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,invoice_id INTEGER,description TEXT,
        quantity REAL,rate REAL,taxable INTEGER DEFAULT 1,line_total REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bill_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,bill_id INTEGER,description TEXT,
        quantity REAL,rate REAL,taxable INTEGER DEFAULT 1,line_total REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS credit_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,credit_no TEXT UNIQUE,customer_name TEXT,
        invoice_id INTEGER,credit_date TEXT,amount REAL,reason TEXT,status TEXT DEFAULT 'Posted',
        created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,po_no TEXT UNIQUE,vendor_name TEXT,
        po_date TEXT,expected_date TEXT,status TEXT DEFAULT 'Open',notes TEXT,
        subtotal REAL DEFAULT 0,gst REAL DEFAULT 0,qst REAL DEFAULT 0,total REAL DEFAULT 0,
        converted_bill_id INTEGER,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_order_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,po_id INTEGER,description TEXT,
        quantity REAL,rate REAL,taxable INTEGER DEFAULT 1,line_total REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS recurring_invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,description TEXT,
        amount REAL,frequency TEXT,next_date TEXT,revenue_account TEXT,active INTEGER DEFAULT 1,
        taxable INTEGER DEFAULT 1,created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS accounting_periods(
        id INTEGER PRIMARY KEY AUTOINCREMENT,period_start TEXT,period_end TEXT,status TEXT DEFAULT 'Open',
        closed_at TEXT,UNIQUE(period_start,period_end))""")

        # V10.7 migration for databases created by earlier versions.
        transaction_cols = [r[1] for r in c.execute("PRAGMA table_info(transactions)").fetchall()]
        if "tax_reviewed" not in transaction_cols:
            c.execute("ALTER TABLE transactions ADD COLUMN tax_reviewed INTEGER DEFAULT 0")

        # V11.1 profile-table migrations for older databases.
        for table in ("customers","vendors"):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            wanted = {
                "email":"TEXT","phone":"TEXT","address1":"TEXT","address2":"TEXT","city":"TEXT",
                "province_state":"TEXT","postal_zip":"TEXT","country":"TEXT",
                "payment_terms_days":"INTEGER DEFAULT 30","notes":"TEXT",
                "active":"INTEGER DEFAULT 1","created_at":"TEXT"
            }
            for col,ctype in wanted.items():
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")

        # V12 migrations: safely upgrade older invoice/bill databases in place.
        table_migrations = {
            "invoices": {
                "created_at":"TEXT"
            },
            "bills": {
                "created_at":"TEXT"
            },
            "recurring_invoices": {
                "taxable":"INTEGER DEFAULT 1"
            }
        }
        for table,wanted in table_migrations.items():
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            for col,ctype in wanted.items():
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")

        for full,(typ,nat,grp) in DEFAULT_ACCOUNTS.items():
            code,name=full.split(" ",1)
            c.execute("""INSERT OR IGNORE INTO accounts(code,name,type,natural_balance,group_name,active,system_account)
            VALUES(?,?,?,?,?,1,1)""",(code,name,typ,nat,grp))
    write(f)

def account_full(code):
    d=read("SELECT code,name FROM accounts WHERE code=?",(code,))
    return f"{d.iloc[0].code} {d.iloc[0]['name']}" if not d.empty else code

def active_accounts(types=None):
    q="SELECT code,name,type,natural_balance,group_name FROM accounts WHERE active=1"
    d=read(q)
    if types: d=d[d.type.isin(types)]
    return [f"{r.code} {r['name']}" for _,r in d.iterrows()]

def account_meta(full):
    code=str(full).split(" ",1)[0]
    d=read("SELECT * FROM accounts WHERE code=?",(code,))
    return d.iloc[0].to_dict() if not d.empty else {"type":"Other","natural_balance":"Debit","group_name":"Other"}

def norm(s):
    s=re.sub(r"#?\d{2,}","",str(s).lower()); return re.sub(r"\s+"," ",s).strip(" -_*#")

def fp(dt,desc,amt): return hashlib.sha256(f"{dt}|{norm(desc)}|{float(amt):.2f}".encode()).hexdigest()
def key(): return st.session_state.get("api_key","").strip() or os.getenv("OPENAI_API_KEY","").strip()

def locked(d):
    day=str(pd.to_datetime(d).date())

    # Legacy close-period lock retained for backward compatibility.
    legacy=read(
        """SELECT period_end FROM close_periods
           WHERE locked=1 AND period_end>=?
           ORDER BY period_end LIMIT 1""",
        (day,)
    )
    if not legacy.empty:
        return True

    # V12 Accounting Periods: lock only dates actually inside a closed range.
    periods=read(
        """SELECT id FROM accounting_periods
           WHERE status='Closed' AND ? BETWEEN period_start AND period_end
           LIMIT 1""",
        (day,)
    )
    return not periods.empty

def closed_period_message(d, action="post this transaction"):
    day=str(pd.to_datetime(d).date())
    p=read(
        """SELECT period_start,period_end FROM accounting_periods
           WHERE status='Closed' AND ? BETWEEN period_start AND period_end
           ORDER BY period_start LIMIT 1""",
        (day,)
    )
    if not p.empty:
        r=p.iloc[0]
        return (
            f"Accounting period closed: cannot {action} dated {day} because it falls within "
            f"the closed period {r.period_start} to {r.period_end}. Reopen the period to make changes."
        )
    return f"Accounting period closed: cannot {action} dated {day}."

def profile():
    d=read("SELECT * FROM business_profile WHERE id=1")
    if d.empty:return {"business_name":"","country":"Canada","region":"Quebec","entity_type":"Corporation",
        "industry":"Electrical contractor","fiscal_year_end":"December 31","gst_registered":False,"qst_registered":False}
    r=d.iloc[0]
    return {"business_name":r.business_name or "","country":r.country or "Canada","region":r.region or "Quebec",
        "entity_type":r.entity_type or "Corporation","industry":r.industry or "",
        "fiscal_year_end":r.fiscal_year_end or "December 31","gst_registered":bool(r.gst_registered),
        "qst_registered":bool(r.qst_registered)}

def save_profile(p):
    def f(c):
        c.execute("""INSERT INTO business_profile VALUES(1,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET business_name=excluded.business_name,country=excluded.country,
        region=excluded.region,entity_type=excluded.entity_type,industry=excluded.industry,
        fiscal_year_end=excluded.fiscal_year_end,gst_registered=excluded.gst_registered,
        qst_registered=excluded.qst_registered""",(p["business_name"],p["country"],p["region"],p["entity_type"],
        p["industry"],p["fiscal_year_end"],int(p["gst_registered"]),int(p["qst_registered"])))
        audit_row(c,"profile_updated","business",1,p)
    write(f)

def normalize_csv(df):
    m={}
    for col in df.columns:
        z=col.strip().lower()
        if z in ["date","transaction date","posted date"]:m[col]="date"
        elif z in ["description","details","merchant","name","transaction"]:m[col]="description"
        elif z in ["amount","transaction amount"]:m[col]="amount"
        elif z=="debit":m[col]="debit"
        elif z=="credit":m[col]="credit"
    df=df.rename(columns=m)
    if "amount" not in df and ("debit" in df or "credit" in df):
        df["amount"]=pd.to_numeric(df.get("credit",0),errors="coerce").fillna(0)-pd.to_numeric(df.get("debit",0),errors="coerce").fillna(0)
    missing={"date","description","amount"}-set(df.columns)
    if missing:raise ValueError(f"Missing columns: {sorted(missing)}")
    out=df[["date","description","amount"]].copy();out["amount"]=pd.to_numeric(out.amount,errors="coerce")
    return out.dropna(subset=["amount"])

def cps():return read("SELECT * FROM counterparties ORDER BY name")
def trans():return read("SELECT * FROM transactions ORDER BY id DESC")
def gl():
    # Always read directly from SQLite. No Streamlit session-state cache.
    return read("SELECT * FROM journal_entries ORDER BY id")

def gl_status():
    j = gl()
    if j.empty:
        return {
            "rows": 0,
            "debits": 0.0,
            "credits": 0.0,
            "difference": 0.0
        }
    debits = round(float(j.debit.sum()), 2)
    credits = round(float(j.credit.sum()), 2)
    return {
        "rows": len(j),
        "debits": debits,
        "credits": credits,
        "difference": round(debits - credits, 2)
    }

def invs():return read("SELECT * FROM invoices ORDER BY id DESC")
def bills():return read("SELECT * FROM bills ORDER BY id DESC")
def audits():return read("SELECT * FROM audit_log ORDER BY id DESC")
def docs():return read("SELECT * FROM documents ORDER BY id DESC")

def lookup_cp(desc):
    d=norm(desc)
    for _,r in cps().iterrows():
        if norm(r["name"]) and norm(r["name"]) in d:return r["name"],r["type"],r["default_category"],r["default_account"]
    return None

def learned(desc):
    c=connect()
    try:return c.execute("SELECT category,times_confirmed FROM learned_rules WHERE normalized_description=?",(norm(desc),)).fetchone()
    finally:c.close()

def learn(desc,cat):
    write(lambda c:c.execute("""INSERT INTO learned_rules VALUES(?,?,1)
    ON CONFLICT(normalized_description) DO UPDATE SET category=excluded.category,times_confirmed=times_confirmed+1""",(norm(desc),cat)))

def classify_rule(desc,amt):
    cp=lookup_cp(desc)
    if cp:
        n,t,cat,acct=cp
        return dict(category=cat or "Needs Review",confidence=.99,review=False,explanation=f"Matched saved counterparty {n}.",
                    question="",source="counterparty",counterparty_name=n,counterparty_type=t)
    lr=learned(desc)
    if lr:
        cat,n=lr
        return dict(category=cat,confidence=min(.90+n*.02,.99),review=False,explanation=f"Learned from {n} confirmation(s).",
                    question="",source="memory",counterparty_name="",counterparty_type="")
    text=str(desc).lower()
    for pat,cat,conf in RULES:
        if re.search(pat,text,re.I):
            rev=any(x in text for x in ALWAYS_REVIEW) or abs(float(amt))>=2500
            return dict(category=cat,confidence=min(conf,.82) if rev else conf,review=rev,
                explanation="Matched merchant rule.",question="What exactly was purchased and how was it used?" if rev else "",
                source="rule",counterparty_name="",counterparty_type="")
    return None

def ai_classify(client,p,desc,amt):
    credit_cost=AI_CREDIT_COSTS["transaction_categorization"]
    company_id=v18_require_ai_credits("transaction_categorization",credit_cost)
    r=client.responses.parse(model=MODEL,input=[{"role":"system","content":"You are a conservative bookkeeping preparation assistant."},
    {"role":"user","content":f"Business {p['entity_type']} in {p['region']}, {p['country']}; industry {p['industry']}. Transaction {desc}; amount {amt}. Allowed categories: {', '.join(CATEGORIES)}."}],
    text_format=Classification).output_parsed
    cat=r.category if r.category in CATEGORIES else "Needs Review"
    rev=r.review_required or r.confidence<.85 or any(x in str(desc).lower() for x in ALWAYS_REVIEW)
    result=dict(category=cat,confidence=float(r.confidence),review=rev,explanation=r.explanation,
        question=r.question_for_owner or "",source="ai",counterparty_name=r.counterparty_name or "",
        counterparty_type=r.counterparty_type or "Unknown")
    v18_consume_ai_credits(company_id,"transaction_categorization",credit_cost,f"{desc} | {amt}")
    return result

def analyze(df,p):
    client=OpenAI(api_key=key()) if key() else None;rows=[];bar=st.progress(0)
    for pos,(_,r) in enumerate(df.iterrows(),1):
        x=classify_rule(r.description,r.amount)
        if not x:
            try:x=ai_classify(client,p,r.description,float(r.amount)) if client else None
            except Exception as e:x=None
            if not x:x=dict(category="Needs Review",confidence=0,review=True,explanation="Needs owner review.",
                question="What was this transaction for?",source="review",counterparty_name="",counterparty_type="Unknown")
        rows.append(dict(date=r.date,description=str(r.description),amount=float(r.amount),**x,owner_answer="",
            status="Needs your answer" if x["review"] else "Ready for books",
            account=CATEGORY_TO_ACCOUNT.get(x["category"],"6999 Uncategorized Expense"),receipt_name="",
            receipt_attached=0,tax_included=0,gst_amount=0,qst_amount=0,business_use_pct=100,tax_eligible=0,
            reconciled=0,period_locked=0,tax_reviewed=0))
        bar.progress(pos/len(df))
    bar.empty();return pd.DataFrame(rows)

def resolve_ai(client,p,row,answer):
    credit_cost=AI_CREDIT_COSTS["transaction_resolution"]
    company_id=v18_require_ai_credits("transaction_resolution",credit_cost)
    r=client.responses.parse(model=MODEL,input=[{"role":"system","content":"Resolve ambiguous bookkeeping conservatively."},
    {"role":"user","content":f"Business {p['entity_type']} in {p['region']}, {p['country']}; industry {p['industry']}. Transaction {row['description']}; amount {row['amount']}; current {row['category']}; owner answer: {answer}. Allowed categories: {', '.join(CATEGORIES)}."}],
    text_format=Resolution).output_parsed
    cat=r.category if r.category in CATEGORIES else "Needs Review";ok=bool(r.resolved) and r.confidence>=.85
    result=dict(category=cat,confidence=float(r.confidence),review=not ok,explanation=r.explanation,
        question=r.follow_up_question or "",source="ai+owner",owner_answer=answer,status="Ready for books" if ok else "Needs your answer",
        counterparty_name=r.counterparty_name or "",counterparty_type=r.counterparty_type or "Unknown")
    v18_consume_ai_credits(company_id,"transaction_resolution",credit_cost,f"{row['description']} | owner answer")
    return result

def split_quebec_tax_from_gross(gross_amount):
    """
    Split a tax-included Québec total using the standard 5% GST + 9.975% QST
    rates, both calculated on the pre-tax selling price.

    Returns (subtotal, gst, qst, rounding_adjustment).
    """
    gross = round(abs(float(gross_amount)), 2)
    if gross <= 0:
        return 0.0, 0.0, 0.0, 0.0

    subtotal = round(gross / (1 + QC_COMBINED_RATE), 2)
    gst = round(subtotal * GST_RATE_QC, 2)
    qst = round(subtotal * QST_RATE_QC, 2)

    # Invoice-level rounding can make the three rounded components miss the
    # bank total by a cent. Push only that rounding penny into QST so the
    # accounting entry always equals the actual bank amount.
    rounding = round(gross - subtotal - gst - qst, 2)
    qst = round(qst + rounding, 2)

    return subtotal, gst, qst, rounding


def calculate_quebec_tax_from_subtotal(subtotal_amount):
    subtotal = round(abs(float(subtotal_amount)), 2)
    gst = round(subtotal * GST_RATE_QC, 2)
    qst = round(subtotal * QST_RATE_QC, 2)
    total = round(subtotal + gst + qst, 2)
    return subtotal, gst, qst, total


def transaction_is_posted(transaction_id):
    d = read(
        """SELECT COUNT(*) AS n FROM journal_entries
           WHERE transaction_id=? AND source_type='Bank'""",
        (int(transaction_id),)
    )
    return int(d.iloc[0]["n"]) > 0


def apply_tax_treatment(
    transaction_id,
    treatment,
    business_use_pct=100.0,
    eligible=True,
    manual_gst=None,
    manual_qst=None
):
    """
    Store transaction-level tax treatment before GL posting.

    treatment:
      - "No tax / exempt / zero-rated"
      - "Québec GST + QST included in bank amount"
      - "Manual tax amounts"
    """
    tid = int(transaction_id)

    if transaction_is_posted(tid):
        raise ValueError(
            "This transaction is already posted to the General Ledger. "
            "Reverse/correct the posted source before changing its tax treatment."
        )

    t = read("SELECT * FROM transactions WHERE id=?", (tid,))
    if t.empty:
        raise ValueError("Transaction not found.")

    row = t.iloc[0]
    gross = abs(float(row.amount))

    if treatment == "No tax / exempt / zero-rated":
        gst = 0.0
        qst = 0.0
        included = 0
        tax_eligible = 0
        subtotal = gross

    elif treatment == "Québec GST + QST included in bank amount":
        subtotal, gst, qst, rounding = split_quebec_tax_from_gross(gross)
        included = 1
        tax_eligible = 1 if eligible else 0

    elif treatment == "Manual tax amounts":
        gst = round(float(manual_gst or 0), 2)
        qst = round(float(manual_qst or 0), 2)
        subtotal = round(gross - gst - qst, 2)
        if subtotal < 0:
            raise ValueError("GST/QST amounts cannot exceed the gross transaction amount.")
        included = 1
        tax_eligible = 1 if eligible else 0

    else:
        raise ValueError("Unknown tax treatment.")

    pct = max(0.0, min(100.0, float(business_use_pct)))

    # For purchases, only the eligible business-use share is booked as ITC/ITR.
    # The non-recoverable portion stays in the expense/asset cost.
    if float(row.amount) < 0 and tax_eligible:
        gst_book = round(gst * pct / 100.0, 2)
        qst_book = round(qst * pct / 100.0, 2)
    else:
        gst_book = gst
        qst_book = qst

    def f(c):
        # Re-check inside the write transaction.
        posted = c.execute(
            """SELECT COUNT(*) FROM journal_entries
               WHERE transaction_id=? AND source_type='Bank'""",
            (tid,)
        ).fetchone()[0]
        if posted:
            raise ValueError(
                "This transaction became posted before the tax treatment was saved. "
                "No tax changes were made."
            )

        c.execute(
            """UPDATE transactions
               SET tax_included=?,
                   gst_amount=?,
                   qst_amount=?,
                   business_use_pct=?,
                   tax_eligible=?,
                   tax_reviewed=1,
                   explanation=COALESCE(explanation,'') || ?
               WHERE id=?""",
            (
                int(included),
                float(gst_book),
                float(qst_book),
                pct,
                int(tax_eligible),
                f" | Tax treatment: {treatment}; pre-tax ${subtotal:.2f}; "
                f"GST ${gst_book:.2f}; QST ${qst_book:.2f}.",
                tid
            )
        )

        audit_row(
            c, "tax_treatment_saved", "transaction", tid,
            {
                "treatment": treatment,
                "gross": gross,
                "subtotal": subtotal,
                "gst": gst_book,
                "qst": qst_book,
                "business_use_pct": pct,
                "eligible": bool(tax_eligible)
            }
        )

    write(f)

    return {
        "transaction_id": tid,
        "gross": gross,
        "subtotal": subtotal,
        "gst": gst_book,
        "qst": qst_book,
        "total_check": round(subtotal + gst + qst, 2)
    }


def save_rows(df,filename):
    def f(c):
        s=d=0
        for _,r in df.iterrows():
            fingerprint=fp(r.date,r.description,r.amount)
            existing=c.execute("SELECT id FROM transactions WHERE fingerprint=?",(fingerprint,)).fetchone()
            vals=(str(r.date),r.description,float(r.amount),r.category,float(r.confidence),int(r.review),r.explanation,r.question,
                  r.owner_answer,r.source,r.status,filename,fingerprint,r.account,r.get("receipt_name",""),int(r.get("receipt_attached",0)),
                  int(r.get("tax_included",0)),float(r.get("gst_amount",0)),float(r.get("qst_amount",0)),float(r.get("business_use_pct",100)),
                  int(r.get("tax_eligible",0)),r.get("counterparty_name",""),r.get("counterparty_type","Unknown"),
                  int(r.get("reconciled",0)),int(r.get("period_locked",0)),int(r.get("tax_reviewed",0)))
            if existing:
                tid=existing[0]
                c.execute("""UPDATE transactions SET date=?,description=?,amount=?,category=?,confidence=?,review=?,explanation=?,question=?,
                owner_answer=?,source=?,status=?,source_file=?,fingerprint=?,account=?,receipt_name=?,receipt_attached=?,tax_included=?,
                gst_amount=?,qst_amount=?,business_use_pct=?,tax_eligible=?,counterparty_name=?,counterparty_type=?,
                reconciled=MAX(reconciled,?),period_locked=MAX(period_locked,?),
                tax_reviewed=MAX(tax_reviewed,?) WHERE id=?""",vals+(tid,))
                audit_row(c,"transaction_updated","transaction",tid,{"category":r.category,"status":r.status});d+=1
            else:
                c.execute("""INSERT INTO transactions(date,description,amount,category,confidence,review,explanation,question,owner_answer,
                source,status,source_file,fingerprint,account,receipt_name,receipt_attached,tax_included,gst_amount,qst_amount,business_use_pct,
                tax_eligible,counterparty_name,counterparty_type,reconciled,period_locked,tax_reviewed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",vals)
                tid=c.execute("SELECT last_insert_rowid()").fetchone()[0];audit_row(c,"transaction_saved","transaction",tid,{"category":r.category});s+=1
        return s,d
    return write(f)

def post_bank():
    """
    V10.7 posting control.

    For businesses registered for GST/HST or QST, every saved bank transaction
    requires an explicit tax decision before posting. The decision can be:
      - taxable GST+QST included,
      - no tax / exempt / zero-rated,
      - manual tax amounts.

    This prevents a taxable gross receipt from being posted entirely to revenue.
    """
    def f(c):
        profile_row = c.execute(
            "SELECT gst_registered,qst_registered FROM business_profile WHERE id=1"
        ).fetchone()

        registered = bool(profile_row and (profile_row[0] or profile_row[1]))

        ready = pd.read_sql_query(
            """SELECT * FROM transactions t
               WHERE status='Ready for books'
                 AND period_locked=0
                 AND NOT EXISTS(
                     SELECT 1 FROM journal_entries j
                     WHERE j.transaction_id=t.id
                       AND j.source_type='Bank'
                 )
               ORDER BY t.id""",
            c
        )

        posted_sources = 0
        posted_rows = 0
        skipped_locked = 0
        skipped_tax_review = []

        for _, r in ready.iterrows():
            tid = int(r.id)

            if registered and int(getattr(r, "tax_reviewed", 0) or 0) != 1:
                skipped_tax_review.append(tid)
                continue

            locked_row = c.execute(
                """SELECT 1 FROM close_periods
                   WHERE locked=1 AND period_end>=?
                   ORDER BY period_end LIMIT 1""",
                (str(r.date),)
            ).fetchone()

            if locked_row:
                skipped_locked += 1
                continue

            already = c.execute(
                """SELECT COUNT(*) FROM journal_entries
                   WHERE transaction_id=? AND source_type='Bank'""",
                (tid,)
            ).fetchone()[0]
            if already:
                continue

            gross = round(abs(float(r.amount)), 2)
            gst = round(float(r.gst_amount or 0), 2)
            qst = round(float(r.qst_amount or 0), 2)
            net = round(gross - gst - qst, 2)

            if gross <= 0:
                raise ValueError(f"Transaction {tid} has a zero amount and cannot be posted.")
            if net < 0:
                raise ValueError(
                    f"Transaction {tid} has GST/QST larger than its gross amount."
                )

            stamp = datetime.now().isoformat(timespec="seconds")
            entries = []

            if float(r.amount) < 0:
                # Purchase / cash outflow
                entries.append((r.account, net, 0.0))
                if gst:
                    entries.append(("1200 GST/HST Receivable", gst, 0.0))
                if qst:
                    entries.append(("1210 QST Receivable", qst, 0.0))
                entries.append(("1000 Bank", 0.0, gross))
            else:
                # Sale / cash inflow
                entries.append(("1000 Bank", gross, 0.0))
                entries.append((r.account, 0.0, net))
                if gst:
                    entries.append(("2300 GST/HST Payable", 0.0, gst))
                if qst:
                    entries.append(("2310 QST Payable", 0.0, qst))

            total_debit = round(sum(x[1] for x in entries), 2)
            total_credit = round(sum(x[2] for x in entries), 2)

            if total_debit != total_credit:
                raise RuntimeError(
                    f"Transaction {tid} would post out of balance: "
                    f"debits ${total_debit:.2f}, credits ${total_credit:.2f}."
                )

            for account, debit, credit in entries:
                c.execute(
                    """INSERT INTO journal_entries(
                        transaction_id,date,memo,account,debit,credit,
                        source_type,source_id,created_at
                    ) VALUES(?,?,?,?,?,?,'Bank',?,?)""",
                    (
                        tid, str(r.date), str(r.description), account,
                        float(debit), float(credit), str(tid), stamp
                    )
                )

            created = c.execute(
                """SELECT COUNT(*) FROM journal_entries
                   WHERE transaction_id=? AND source_type='Bank'""",
                (tid,)
            ).fetchone()[0]

            if created != len(entries):
                raise RuntimeError(
                    f"Posting verification failed for transaction {tid}: "
                    f"expected {len(entries)} GL rows, found {created}."
                )

            src = c.execute(
                """SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0)
                   FROM journal_entries
                   WHERE transaction_id=? AND source_type='Bank'""",
                (tid,)
            ).fetchone()

            if round(float(src[0]) - float(src[1]), 2) != 0:
                raise RuntimeError(
                    f"Posting verification failed for transaction {tid}: source is not balanced."
                )

            audit_row(
                c, "posted_to_gl", "transaction", tid,
                {
                    "gross": gross,
                    "net": net,
                    "gst": gst,
                    "qst": qst,
                    "gl_rows_created": created,
                    "tax_reviewed": int(getattr(r, "tax_reviewed", 0) or 0),
                    "debit": float(src[0]),
                    "credit": float(src[1])
                }
            )

            posted_sources += 1
            posted_rows += created

        return {
            "posted_sources": posted_sources,
            "posted_rows": posted_rows,
            "skipped_locked": skipped_locked,
            "skipped_tax_review": skipped_tax_review
        }

    return write(f)

def create_simple_doc(entity_type,entity_id,uploaded,notes):
    safe=re.sub(r"[^A-Za-z0-9._-]","_",uploaded.name)
    dest=DOC_DIR/f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    dest.write_bytes(uploaded.getbuffer())
    def f(c):
        c.execute("INSERT INTO documents(entity_type,entity_id,file_name,stored_path,uploaded_at,notes) VALUES(?,?,?,?,?,?)",
                  (entity_type,str(entity_id),uploaded.name,str(dest),datetime.now().isoformat(timespec="seconds"),notes))
        did=c.execute("SELECT last_insert_rowid()").fetchone()[0];audit_row(c,"document_attached","document",did,{"entity_type":entity_type,"entity_id":entity_id,"file":uploaded.name})
    write(f)

def generate_unique_journal_no(prefix="JE"):
    """
    Generate a human-readable journal number with a collision-resistant suffix.
    We still verify against the database before returning it.
    """
    for _ in range(20):
        candidate = (
            f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-"
            f"{uuid.uuid4().hex[:6].upper()}"
        )
        c = connect()
        try:
            exists = c.execute(
                "SELECT 1 FROM manual_journals WHERE journal_no=? LIMIT 1",
                (candidate,)
            ).fetchone()
        finally:
            c.close()
        if not exists:
            return candidate
    raise RuntimeError("Could not generate a unique journal number. Please try again.")


def create_journal(no,jdate,memo,lines,reversal_of=None,source="Manual Journal"):
    td = sum(float(x[1]) for x in lines)
    tc = sum(float(x[2]) for x in lines)

    if round(td-tc,2) != 0 or td <= 0:
        raise ValueError("Journal must balance and cannot be empty.")

    if locked(jdate):
        raise ValueError(closed_period_message(jdate,"create a journal"))

    no = str(no).strip() or generate_unique_journal_no()

    def f(c):
        if c.execute(
            "SELECT 1 FROM manual_journals WHERE journal_no=? LIMIT 1",
            (no,)
        ).fetchone():
            raise ValueError(
                "That journal number already exists. Sullivan generated a new number; please save again."
            )

        # Defense-in-depth: re-check the locked period from this DB connection
        # immediately before writing accounting data.
        locked_row = c.execute(
            """SELECT 1 FROM close_periods
               WHERE locked=1 AND period_end>=?
               ORDER BY period_end LIMIT 1""",
            (str(jdate),)
        ).fetchone()
        new_period_lock = c.execute(
            """SELECT 1 FROM accounting_periods
               WHERE status='Closed' AND ? BETWEEN period_start AND period_end
               LIMIT 1""",
            (str(pd.to_datetime(jdate).date()),)
        ).fetchone()
        if locked_row or new_period_lock:
            raise ValueError(closed_period_message(jdate,"create a journal"))

        c.execute(
            """INSERT INTO manual_journals(
                journal_no,journal_date,memo,posted,reversal_of
            ) VALUES(?,?,?,0,?)""",
            (no,str(jdate),memo,reversal_of)
        )
        jid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        for acct,dr,cr in lines:
            if dr or cr:
                if dr and cr:
                    raise ValueError(
                        f"Journal line for {acct} cannot contain both a debit and a credit."
                    )
                c.execute(
                    """INSERT INTO manual_journal_lines(
                        journal_id,account,debit,credit
                    ) VALUES(?,?,?,?)""",
                    (jid,acct,float(dr),float(cr))
                )

        audit_row(
            c,"manual_journal_created","journal",jid,
            {
                "journal_no":no,
                "debit":td,
                "credit":tc,
                "reversal_of":reversal_of
            }
        )
        return jid

    try:
        return write(f)
    except sqlite3.IntegrityError as e:
        if "manual_journals.journal_no" in str(e):
            raise ValueError(
                "That journal number was already used. Sullivan will generate a fresh number automatically."
            ) from None
        raise

def post_journal(jid):
    def f(c):
        h = c.execute(
            """SELECT journal_no,journal_date,memo,posted,reversal_of
               FROM manual_journals WHERE id=?""",
            (jid,)
        ).fetchone()

        if not h:
            raise ValueError("Journal not found.")
        if h[3]:
            return 0

        locked_row = c.execute(
            """SELECT 1 FROM close_periods
               WHERE locked=1 AND period_end>=?
               ORDER BY period_end LIMIT 1""",
            (str(h[1]),)
        ).fetchone()
        new_period_lock = c.execute(
            """SELECT 1 FROM accounting_periods
               WHERE status='Closed' AND ? BETWEEN period_start AND period_end
               LIMIT 1""",
            (str(pd.to_datetime(h[1]).date()),)
        ).fetchone()
        if locked_row or new_period_lock:
            raise ValueError(closed_period_message(h[1],f"post journal {h[0]}"))

        lines = c.execute(
            "SELECT account,debit,credit FROM manual_journal_lines WHERE journal_id=?",
            (jid,)
        ).fetchall()

        if not lines:
            raise ValueError("Journal has no lines.")

        if round(sum(x[1] for x in lines)-sum(x[2] for x in lines),2) != 0:
            raise ValueError("Journal is not balanced.")

        for acct,dr,cr in lines:
            if dr < 0 or cr < 0:
                raise ValueError("Journal debits and credits cannot be negative.")
            if dr > 0 and cr > 0:
                raise ValueError(
                    f"Journal line for {acct} cannot contain both a debit and a credit."
                )

        stamp = datetime.now().isoformat(timespec="seconds")

        for acct,dr,cr in lines:
            c.execute(
                """INSERT INTO journal_entries(
                    transaction_id,date,memo,account,debit,credit,
                    source_type,source_id,created_at
                ) VALUES(NULL,?,?,?,?,?,'Manual Journal',?,?)""",
                (h[1],h[2],acct,float(dr),float(cr),str(jid),stamp)
            )

        created = c.execute(
            """SELECT COUNT(*) FROM journal_entries
               WHERE source_type='Manual Journal' AND source_id=?""",
            (str(jid),)
        ).fetchone()[0]

        if created != len(lines):
            raise RuntimeError(
                f"Journal posting verification failed: expected {len(lines)} GL rows, found {created}."
            )

        c.execute("UPDATE manual_journals SET posted=1 WHERE id=?",(jid,))
        audit_row(
            c,"manual_journal_posted","journal",jid,
            {"journal_no":h[0],"gl_rows_created":created}
        )
        return created

    return write(f)

def reverse_source(source_type, source_id, reversal_date, reason):
    if locked(reversal_date):
        raise ValueError("Reversal date must be in an open period.")

    source_type = str(source_type)
    source_id = str(source_id)
    reversal_source_id = f"{source_type}:{source_id}"
    reason = str(reason).strip()

    if not reason:
        raise ValueError("A correction reason is required.")

    def f(c):
        rows = c.execute(
            """SELECT id,date,memo,account,debit,credit
               FROM journal_entries
               WHERE source_type=? AND source_id=?
               ORDER BY id""",
            (source_type, source_id)
        ).fetchall()

        if not rows:
            raise ValueError("No posted journal rows found for that source.")

        existing = c.execute(
            """SELECT COUNT(*) FROM journal_entries
               WHERE source_type='Reversal' AND source_id=?""",
            (reversal_source_id,)
        ).fetchone()[0]

        if existing:
            raise ValueError("This source has already been reversed.")

        stamp = datetime.now().isoformat(timespec="seconds")

        for original_id, original_date, memo, account, debit, credit in rows:
            c.execute(
                """INSERT INTO journal_entries(
                    transaction_id,date,memo,account,debit,credit,
                    source_type,source_id,reversal_of,created_at
                ) VALUES(NULL,?,?,?,?,?,'Reversal',?,?,?)""",
                (
                    str(reversal_date),
                    f"REVERSAL: {reason} | {memo}",
                    account,
                    float(credit or 0),
                    float(debit or 0),
                    reversal_source_id,
                    int(original_id),
                    stamp,
                )
            )

        created = c.execute(
            """SELECT COUNT(*) FROM journal_entries
               WHERE source_type='Reversal' AND source_id=?""",
            (reversal_source_id,)
        ).fetchone()[0]

        if created != len(rows):
            raise RuntimeError(
                f"Reversal verification failed: expected {len(rows)} rows but found {created}."
            )

        audit_row(
            c, "source_reversed", source_type, source_id,
            {
                "date": str(reversal_date),
                "reason": reason,
                "original_rows": len(rows),
                "reversal_rows_created": created,
                "reversal_source_id": reversal_source_id,
            }
        )
        return created

    return write(f)

def opening_balance(asof,lines):
    return create_journal(f"OPEN-{str(asof)}",asof,"Opening balances",lines,None)

def customer_profiles(active_only=False):
    sql = "SELECT * FROM customers"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY name"
    return read(sql)


def vendor_profiles(active_only=False):
    sql = "SELECT * FROM vendors"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY name"
    return read(sql)


def save_party_profile(kind, name, email="", phone="", address1="", address2="", city="",
                       province_state="", postal_zip="", country="Canada",
                       payment_terms_days=30, notes="", active=True):
    table = "customers" if kind=="customer" else "vendors"
    entity = "customer" if kind=="customer" else "vendor"
    name = str(name).strip()
    if not name:
        raise ValueError("Name is required.")
    terms = int(payment_terms_days)
    if terms < 0 or terms > 365:
        raise ValueError("Payment terms must be between 0 and 365 days.")

    stamp = datetime.now().isoformat(timespec="seconds")

    def f(c):
        row = c.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()
        if row:
            pid = int(row[0])
            c.execute(
                f"""UPDATE {table}
                    SET email=?,phone=?,address1=?,address2=?,city=?,province_state=?,postal_zip=?,
                        country=?,payment_terms_days=?,notes=?,active=?
                    WHERE id=?""",
                (
                    email,phone,address1,address2,city,province_state,postal_zip,country,
                    terms,notes,1 if active else 0,pid
                )
            )
            action = f"{entity}_updated"
        else:
            c.execute(
                f"""INSERT INTO {table}(
                    name,email,phone,address1,address2,city,province_state,postal_zip,country,
                    payment_terms_days,notes,active,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    name,email,phone,address1,address2,city,province_state,postal_zip,country,
                    terms,notes,1 if active else 0,stamp
                )
            )
            pid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
            action = f"{entity}_created"

        audit_row(
            c, action, entity, pid,
            {"name":name,"payment_terms_days":terms,"active":bool(active)}
        )
        return pid

    return write(f)


def party_balance(kind, name):
    name = str(name)
    if kind=="customer":
        d = read(
            """SELECT COALESCE(SUM(i.total),0) AS billed,
                      COALESCE(SUM((
                          SELECT COALESCE(SUM(p.amount),0)
                          FROM invoice_payments p WHERE p.invoice_id=i.id
                      )),0) AS paid
               FROM invoices i
               WHERE i.customer_name=? AND i.posted=1""",
            (name,)
        )
        billed = float(d.iloc[0]["billed"]) if not d.empty else 0.0
        paid = float(d.iloc[0]["paid"]) if not d.empty else 0.0
        return round(billed-paid,2)
    else:
        d = read(
            """SELECT COALESCE(SUM(b.total),0) AS billed,
                      COALESCE(SUM((
                          SELECT COALESCE(SUM(p.amount),0)
                          FROM bill_payments p WHERE p.bill_id=b.id
                      )),0) AS paid
               FROM bills b
               WHERE b.vendor_name=? AND b.posted=1""",
            (name,)
        )
        billed = float(d.iloc[0]["billed"]) if not d.empty else 0.0
        paid = float(d.iloc[0]["paid"]) if not d.empty else 0.0
        return round(billed-paid,2)


def customer_history(name):
    inv = read(
        """SELECT id,invoice_no,invoice_date,due_date,total,status,posted,paid_date
           FROM invoices WHERE customer_name=? ORDER BY invoice_date DESC,id DESC""",
        (name,)
    )
    pay = read(
        """SELECT p.id,i.invoice_no,p.payment_date,p.amount,p.created_at
           FROM invoice_payments p
           JOIN invoices i ON i.id=p.invoice_id
           WHERE i.customer_name=?
           ORDER BY p.payment_date DESC,p.id DESC""",
        (name,)
    )
    return inv, pay


def vendor_history(name):
    bills_df = read(
        """SELECT id,bill_no,bill_date,due_date,total,status,posted,paid_date
           FROM bills WHERE vendor_name=? ORDER BY bill_date DESC,id DESC""",
        (name,)
    )
    pay = read(
        """SELECT p.id,b.bill_no,p.payment_date,p.amount,p.created_at
           FROM bill_payments p
           JOIN bills b ON b.id=p.bill_id
           WHERE b.vendor_name=?
           ORDER BY p.payment_date DESC,p.id DESC""",
        (name,)
    )
    return bills_df, pay



def next_business_number(kind, doc_date=None):
    d=pd.to_datetime(doc_date or date.today()).date()
    code=d.strftime("%Y%m%d")
    mapping={
        "estimate":("EST","estimates","estimate_no"),
        "credit":("CR","credit_notes","credit_no"),
        "po":("PO","purchase_orders","po_no"),
    }
    prefix,table,col=mapping[kind]
    base=f"{prefix}-{code}-"
    df=read(f"SELECT {col} AS n FROM {table} WHERE {col} LIKE ?",(base+"%",))
    used=set()
    if not df.empty:
        for v in df.n.astype(str):
            m=re.match(re.escape(base)+r"(\d+)$",v)
            if m: used.add(int(m.group(1)))
    n=1
    while n in used:n+=1
    return f"{base}{n:04d}"

def calc_lines(lines, gst_rate=0.05, qst_rate=0.09975):
    subtotal=round(sum(float(x["quantity"])*float(x["rate"]) for x in lines),2)
    taxable=round(sum(float(x["quantity"])*float(x["rate"]) for x in lines if x.get("taxable",True)),2)
    gst=round(taxable*gst_rate,2)
    qst=round(taxable*qst_rate,2)
    return subtotal,gst,qst,round(subtotal+gst+qst,2)

def create_estimate(customer,estimate_date,expiry_date,notes,lines,gst_rate=0.0,qst_rate=0.0):
    no=next_business_number("estimate",estimate_date)
    subtotal,gst,qst,total=calc_lines(lines,gst_rate,qst_rate)
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO estimates(estimate_no,customer_name,estimate_date,expiry_date,status,notes,
                   subtotal,gst,qst,total,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (no,customer,str(estimate_date),str(expiry_date),"Draft",notes,subtotal,gst,qst,total,stamp))
        eid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        for x in lines:
            lt=round(float(x["quantity"])*float(x["rate"]),2)
            c.execute("""INSERT INTO estimate_lines(estimate_id,description,quantity,rate,taxable,line_total)
                         VALUES(?,?,?,?,?,?)""",(eid,x["description"],x["quantity"],x["rate"],1 if x.get("taxable",True) else 0,lt))
        audit_row(c,"estimate_created","estimate",eid,{"estimate_no":no,"total":total})
        return eid
    return write(f)

def convert_estimate_to_invoice(eid):
    eid=int(eid); stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        e=c.execute("SELECT * FROM estimates WHERE id=?",(eid,)).fetchone()
        if not e: raise ValueError("Estimate not found.")
        cols=[x[1] for x in c.execute("PRAGMA table_info(estimates)").fetchall()]
        E=dict(zip(cols,e))
        if E["converted_invoice_id"]: raise ValueError("Estimate already converted.")
        customer=E["customer_name"]; inv_date=date.today()
        if locked(inv_date):
            raise ValueError(closed_period_message(inv_date,"convert this estimate to an invoice"))
        termsrow=c.execute("SELECT payment_terms_days FROM customers WHERE name=?",(customer,)).fetchone()
        terms=int(termsrow[0]) if termsrow and termsrow[0] is not None else 30
        due=inv_date+timedelta(days=terms)
        invno_prefix=f"INV-{inv_date.strftime('%Y%m%d')}-"
        nums=[x[0] for x in c.execute("SELECT invoice_no FROM invoices WHERE invoice_no LIKE ?",(invno_prefix+"%",)).fetchall()]
        used=set()
        for value in nums:
            m=re.match(re.escape(invno_prefix)+r"(\d+)$",str(value))
            if m: used.add(int(m.group(1)))
        seq=1
        while seq in used: seq+=1
        invno=f"{invno_prefix}{seq:04d}"
        c.execute("""INSERT INTO invoices(invoice_no,customer_name,invoice_date,due_date,revenue_account,
                   subtotal,gst,qst,total,status,posted,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'Open',0,?)""",
                  (invno,customer,str(inv_date),str(due),"4100 Service Revenue",
                   E["subtotal"],E["gst"],E["qst"],E["total"],stamp))
        iid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        lines=c.execute("SELECT description,quantity,rate,taxable,line_total FROM estimate_lines WHERE estimate_id=?",(eid,)).fetchall()
        for x in lines:
            c.execute("""INSERT INTO invoice_lines(invoice_id,description,quantity,rate,taxable,line_total)
                         VALUES(?,?,?,?,?,?)""",(iid,*x))
        c.execute("UPDATE estimates SET status='Converted',converted_invoice_id=? WHERE id=?",(iid,eid))
        audit_row(c,"estimate_converted","estimate",eid,{"invoice_id":iid,"invoice_no":invno})
        return iid
    return write(f)

def create_credit_note(customer,invoice_id,credit_date,amount,reason):
    amount=round(float(amount),2)
    if amount<=0: raise ValueError("Credit amount must be positive.")
    if locked(credit_date): raise ValueError(closed_period_message(credit_date,"post this credit note"))
    no=next_business_number("credit",credit_date); stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        inv=c.execute("""SELECT total,subtotal,gst,qst,posted,revenue_account
                         FROM invoices WHERE id=?""",(int(invoice_id),)).fetchone()
        if not inv or not inv[4]: raise ValueError("Choose a posted invoice.")
        prior=float(c.execute("SELECT COALESCE(SUM(amount),0) FROM credit_notes WHERE invoice_id=?",(int(invoice_id),)).fetchone()[0])
        payments=float(c.execute("SELECT COALESCE(SUM(amount),0) FROM invoice_payments WHERE invoice_id=?",(int(invoice_id),)).fetchone()[0])
        remaining=round(float(inv[0])-prior-payments,2)
        if amount>remaining+.001:
            raise ValueError(f"Credit exceeds the remaining receivable balance of ${remaining:,.2f}.")

        ratio=amount/float(inv[0]) if float(inv[0]) else 0
        revenue_credit=round(float(inv[1])*ratio,2)
        gst_credit=round(float(inv[2])*ratio,2)
        qst_credit=round(float(inv[3])*ratio,2)
        # absorb rounding into revenue so total reversal always equals requested credit
        revenue_credit=round(amount-gst_credit-qst_credit,2)

        c.execute("""INSERT INTO credit_notes(credit_no,customer_name,invoice_id,credit_date,amount,reason,status,created_at)
                     VALUES(?,?,?,?,?,?,'Posted',?)""",(no,customer,int(invoice_id),str(credit_date),amount,reason,stamp))
        cid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

        rows=[(inv[5] or "4100 Service Revenue",revenue_credit,0.0)]
        if gst_credit: rows.append(("2300 GST/HST Payable",gst_credit,0.0))
        if qst_credit: rows.append(("2310 QST Payable",qst_credit,0.0))
        rows.append(("1100 Accounts Receivable",0.0,amount))

        for acct,dr,cr in rows:
            c.execute("""INSERT INTO journal_entries(transaction_id,date,memo,account,debit,credit,source_type,source_id,created_at)
                         VALUES(NULL,?,?,?,?,?,'Credit Note',?,?)""",
                      (str(credit_date),f"{no} - {reason}",acct,dr,cr,str(cid),stamp))

        total_credits=round(prior+amount,2)
        receivable_remaining=round(float(inv[0])-payments-total_credits,2)
        if receivable_remaining<=.005:
            c.execute("UPDATE invoices SET status='Credited' WHERE id=? AND status!='Paid'",(int(invoice_id),))

        audit_row(c,"credit_note_posted","credit_note",cid,{
            "amount":amount,"invoice_id":invoice_id,"revenue_reversal":revenue_credit,
            "gst_reversal":gst_credit,"qst_reversal":qst_credit
        })
        return cid
    return write(f)

def create_po(vendor,po_date,expected_date,notes,lines,gst_rate=0.0,qst_rate=0.0):
    no=next_business_number("po",po_date); subtotal,gst,qst,total=calc_lines(lines,gst_rate,qst_rate); stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO purchase_orders(po_no,vendor_name,po_date,expected_date,status,notes,subtotal,gst,qst,total,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(no,vendor,str(po_date),str(expected_date),"Open",notes,subtotal,gst,qst,total,stamp))
        pid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        for x in lines:
            lt=round(float(x["quantity"])*float(x["rate"]),2)
            c.execute("""INSERT INTO purchase_order_lines(po_id,description,quantity,rate,taxable,line_total)
                         VALUES(?,?,?,?,?,?)""",(pid,x["description"],x["quantity"],x["rate"],1 if x.get("taxable",True) else 0,lt))
        audit_row(c,"purchase_order_created","purchase_order",pid,{"po_no":no,"total":total})
        return pid
    return write(f)

def convert_po_to_bill(pid):
    pid=int(pid); stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        row=c.execute("SELECT * FROM purchase_orders WHERE id=?",(pid,)).fetchone()
        cols=[x[1] for x in c.execute("PRAGMA table_info(purchase_orders)").fetchall()]
        if not row: raise ValueError("PO not found.")
        P=dict(zip(cols,row))
        if P["converted_bill_id"]: raise ValueError("PO already converted.")
        bd=date.today()
        if locked(bd):
            raise ValueError(closed_period_message(bd,"convert this purchase order to a bill"))
        tr=c.execute("SELECT payment_terms_days FROM vendors WHERE name=?",(P["vendor_name"],)).fetchone()
        terms=int(tr[0]) if tr and tr[0] is not None else 30
        due=bd+timedelta(days=terms)
        bprefix=f"BILL-{bd.strftime('%Y%m%d')}-"
        nums=[x[0] for x in c.execute("SELECT bill_no FROM bills WHERE bill_no LIKE ?",(bprefix+"%",)).fetchall()]
        used=set()
        for value in nums:
            m=re.match(re.escape(bprefix)+r"(\d+)$",str(value))
            if m: used.add(int(m.group(1)))
        seq=1
        while seq in used: seq+=1
        bno=f"{bprefix}{seq:04d}"
        c.execute("""INSERT INTO bills(bill_no,vendor_name,bill_date,due_date,expense_account,subtotal,gst,qst,total,status,posted,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,'Open',0,?)""",
                  (bno,P["vendor_name"],str(bd),str(due),"5100 Materials & Supplies",P["subtotal"],P["gst"],P["qst"],P["total"],stamp))
        bid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        for x in c.execute("SELECT description,quantity,rate,taxable,line_total FROM purchase_order_lines WHERE po_id=?",(pid,)).fetchall():
            c.execute("""INSERT INTO bill_lines(bill_id,description,quantity,rate,taxable,line_total) VALUES(?,?,?,?,?,?)""",(bid,*x))
        c.execute("UPDATE purchase_orders SET status='Billed',converted_bill_id=? WHERE id=?",(bid,pid))
        audit_row(c,"purchase_order_converted","purchase_order",pid,{"bill_id":bid,"bill_no":bno})
        return bid
    return write(f)

def create_recurring_invoice(customer,description,amount,frequency,next_date,revenue_account,taxable=True):
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO recurring_invoices(customer_name,description,amount,frequency,next_date,revenue_account,active,taxable,created_at)
                     VALUES(?,?,?,?,?,?,1,?,?)""",(customer,description,float(amount),frequency,str(next_date),revenue_account,1 if taxable else 0,stamp))
    write(f)

def generate_due_recurring(asof=None):
    asof=pd.to_datetime(asof or date.today()).date(); rows=read("SELECT * FROM recurring_invoices WHERE active=1")
    count=0
    for _,r in rows.iterrows():
        nd=pd.to_datetime(r.next_date).date()
        if nd>asof: continue
        cust=r.customer_name
        termsdf=read("SELECT payment_terms_days FROM customers WHERE name=?",(cust,))
        terms=int(termsdf.iloc[0,0]) if not termsdf.empty else 30
        invno=next_document_number("invoice",nd)
        prof=profile()
        taxable=bool(int(r.taxable)) if "taxable" in rows.columns and pd.notna(r.taxable) else True
        gst_amt=round(float(r.amount)*0.05,2) if taxable and prof["gst_registered"] else 0.0
        qst_amt=round(float(r.amount)*0.09975,2) if taxable and prof["qst_registered"] else 0.0
        create_invoice_record(invno,cust,nd,nd+timedelta(days=terms),r.revenue_account,float(r.amount),gst_amt,qst_amt,r.description)
        if r.frequency=="Monthly":
            nxt=(pd.Timestamp(nd)+pd.DateOffset(months=1)).date()
        elif r.frequency=="Quarterly":
            nxt=(pd.Timestamp(nd)+pd.DateOffset(months=3)).date()
        else:
            nxt=(pd.Timestamp(nd)+pd.DateOffset(years=1)).date()
        write(lambda c, rid=int(r.id), nxt=nxt: c.execute("UPDATE recurring_invoices SET next_date=? WHERE id=?",(str(nxt),rid)))
        count+=1
    return count

def customer_statement_df(customer):
    inv=read("""SELECT i.id,i.invoice_no,i.invoice_date AS date,i.due_date,i.total,
               COALESCE((SELECT SUM(p.amount) FROM invoice_payments p WHERE p.invoice_id=i.id),0) paid,
               COALESCE((SELECT SUM(c.amount) FROM credit_notes c WHERE c.invoice_id=i.id),0) credits
               FROM invoices i WHERE i.customer_name=? AND i.posted=1 ORDER BY i.invoice_date,i.id""",(customer,))
    if inv.empty:return inv
    inv["balance"]=(inv.total-inv.paid-inv.credits).round(2)
    return inv

def period_is_closed(d):
    x=str(pd.to_datetime(d).date())
    q=read("SELECT 1 FROM accounting_periods WHERE status='Closed' AND ? BETWEEN period_start AND period_end LIMIT 1",(x,))
    return not q.empty

def close_period(start,end):
    if pd.to_datetime(end)<pd.to_datetime(start): raise ValueError("End must be after start.")
    overlap=read("""SELECT id FROM accounting_periods
                    WHERE status='Closed' AND NOT (period_end < ? OR period_start > ?)""",
                 (str(start),str(end)))
    if not overlap.empty:
        raise ValueError("This period overlaps an already closed accounting period.")
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO accounting_periods(period_start,period_end,status,closed_at)
                     VALUES(?,?,'Closed',?)
                     ON CONFLICT(period_start,period_end) DO UPDATE SET status='Closed',closed_at=excluded.closed_at""",
                  (str(start),str(end),stamp))
    write(f)

def reopen_period(pid):
    write(lambda c: c.execute("UPDATE accounting_periods SET status='Open',closed_at=NULL WHERE id=?",(int(pid),)))

def simple_document_pdf(title,number,party,date_value,due_value,lines,subtotal,gst,qst,total,notes=""):
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    buf=BytesIO(); doc=SimpleDocTemplate(buf,pagesize=letter,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40)
    styles=getSampleStyleSheet(); story=[Paragraph(f"<b>{title}</b>",styles["Title"]),Spacer(1,8),
        Paragraph(f"<b>{number}</b><br/>Customer/Vendor: {party}<br/>Date: {date_value}<br/>Due/Expiry: {due_value}",styles["Normal"]),Spacer(1,14)]
    data=[["Description","Qty","Rate","Amount"]]
    for x in lines:data.append([str(x["description"]),f'{float(x["quantity"]):g}',f'${float(x["rate"]):,.2f}',f'${float(x["quantity"])*float(x["rate"]):,.2f}'])
    t=Table(data,colWidths=[260,60,80,90]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.5,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("ALIGN",(1,1),(-1,-1),"RIGHT")]))
    story += [t,Spacer(1,12),Paragraph(f"Subtotal: ${subtotal:,.2f}<br/>GST/HST: ${gst:,.2f}<br/>QST: ${qst:,.2f}<br/><b>Total: ${total:,.2f}</b>",styles["Normal"])]
    if notes: story += [Spacer(1,12),Paragraph(f"Notes: {notes}",styles["Normal"])]
    doc.build(story); return buf.getvalue()

def next_document_number(kind, doc_date=None):
    """
    Return the next collision-safe invoice or bill number.

    Invoice format: INV-YYYYMMDD-0001
    Bill format:    BILL-YYYYMMDD-0001
    """
    if doc_date is None:
        doc_date = date.today()
    d = pd.to_datetime(doc_date).date()
    date_code = d.strftime("%Y%m%d")

    if kind == "invoice":
        prefix = f"INV-{date_code}-"
        table = "invoices"
        col = "invoice_no"
    elif kind == "bill":
        prefix = f"BILL-{date_code}-"
        table = "bills"
        col = "bill_no"
    else:
        raise ValueError("kind must be invoice or bill")

    existing = read(
        f"""SELECT {col} AS num FROM {table}
            WHERE {col} LIKE ? ORDER BY {col}""",
        (prefix + "%",)
    )

    used = set()
    if not existing.empty:
        for value in existing["num"].astype(str):
            m = re.match(re.escape(prefix) + r"(\d+)$", value)
            if m:
                used.add(int(m.group(1)))

    n = 1
    while n in used:
        n += 1

    return f"{prefix}{n:04d}"


def create_bank_reconciliation(statement_name, statement_date, opening_balance, ending_balance):
    statement_name = str(statement_name).strip() or f"Bank statement {statement_date}"
    opening_balance = round(float(opening_balance), 2)
    ending_balance = round(float(ending_balance), 2)
    stamp = datetime.now().isoformat(timespec="seconds")

    def f(c):
        c.execute(
            """INSERT INTO bank_reconciliations(
               statement_name,statement_date,opening_balance,ending_balance,status,created_at)
               VALUES(?,?,?,?, 'Open', ?)""",
            (statement_name, str(statement_date), opening_balance, ending_balance, stamp)
        )
        rid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        audit_row(
            c, "bank_reconciliation_created", "bank_reconciliation", rid,
            {
                "statement_date": str(statement_date),
                "opening_balance": opening_balance,
                "ending_balance": ending_balance
            }
        )
        return rid

    return write(f)


def ensure_reconciliation_items(rid):
    rid = int(rid)
    rec = read("SELECT * FROM bank_reconciliations WHERE id=?", (rid,))
    if rec.empty:
        raise ValueError("Bank reconciliation not found.")

    statement_date = str(rec.iloc[0]["statement_date"])

    def f(c):
        rows = c.execute(
            """SELECT id FROM journal_entries
               WHERE account='1000 Bank'
                 AND date<=?
               ORDER BY date,id""",
            (statement_date,)
        ).fetchall()

        for row in rows:
            c.execute(
                """INSERT OR IGNORE INTO bank_reconciliation_items(
                   reconciliation_id,journal_entry_id,cleared,created_at)
                   VALUES(?,?,0,?)""",
                (rid, int(row[0]), datetime.now().isoformat(timespec="seconds"))
            )

    write(f)


def reconciliation_detail(rid):
    rid = int(rid)
    ensure_reconciliation_items(rid)

    return read(
        """SELECT ri.id AS item_id,ri.cleared,
                  j.id AS journal_entry_id,j.date,j.memo,j.debit,j.credit,
                  ROUND(j.debit-j.credit,2) AS bank_effect,
                  j.source_type,j.source_id
           FROM bank_reconciliation_items ri
           JOIN journal_entries j ON j.id=ri.journal_entry_id
           WHERE ri.reconciliation_id=?
           ORDER BY j.date,j.id""",
        (rid,)
    )


def set_reconciliation_item(rid, journal_entry_id, cleared):
    rid = int(rid)
    jid = int(journal_entry_id)
    flag = 1 if cleared else 0

    def f(c):
        c.execute(
            """UPDATE bank_reconciliation_items
               SET cleared=?
               WHERE reconciliation_id=? AND journal_entry_id=?""",
            (flag, rid, jid)
        )
        audit_row(
            c, "bank_reconciliation_item_changed", "bank_reconciliation", rid,
            {"journal_entry_id": jid, "cleared": flag}
        )

    write(f)


def reconciliation_summary(rid):
    rid = int(rid)
    rec = read("SELECT * FROM bank_reconciliations WHERE id=?", (rid,))
    if rec.empty:
        raise ValueError("Bank reconciliation not found.")

    r = rec.iloc[0]
    items = reconciliation_detail(rid)

    opening = round(float(r.opening_balance), 2)
    ending = round(float(r.ending_balance), 2)

    cleared_net = 0.0
    uncleared_deposits = 0.0
    uncleared_payments = 0.0

    if not items.empty:
        cleared = items[items.cleared.astype(int) == 1]
        uncleared = items[items.cleared.astype(int) == 0]

        cleared_net = round(float((cleared.debit - cleared.credit).sum()), 2)
        uncleared_deposits = round(float(uncleared.loc[uncleared.bank_effect > 0, "bank_effect"].sum()), 2)
        uncleared_payments = round(float((-uncleared.loc[uncleared.bank_effect < 0, "bank_effect"]).sum()), 2)

    calculated_statement_balance = round(opening + cleared_net, 2)
    difference = round(ending - calculated_statement_balance, 2)

    # Book balance from all bank GL activity through statement date.
    bank = read(
        """SELECT COALESCE(SUM(debit-credit),0) AS balance
           FROM journal_entries
           WHERE account='1000 Bank' AND date<=?""",
        (str(r.statement_date),)
    )
    book_balance = round(float(bank.iloc[0]["balance"]), 2) if not bank.empty else 0.0

    adjusted_bank_balance = round(ending + uncleared_deposits - uncleared_payments, 2)
    book_difference = round(book_balance - adjusted_bank_balance, 2)

    return {
        "opening_balance": opening,
        "ending_balance": ending,
        "cleared_net_change": cleared_net,
        "calculated_statement_balance": calculated_statement_balance,
        "statement_difference": difference,
        "book_balance": book_balance,
        "deposits_in_transit": uncleared_deposits,
        "outstanding_payments": uncleared_payments,
        "adjusted_bank_balance": adjusted_bank_balance,
        "book_difference": book_difference,
        "status": r.status
    }


def complete_bank_reconciliation(rid):
    rid = int(rid)
    summary = reconciliation_summary(rid)

    if abs(summary["statement_difference"]) >= 0.01:
        raise ValueError(
            f"Statement reconciliation difference is ${summary['statement_difference']:,.2f}. "
            "Clear the correct bank items before completing."
        )

    if abs(summary["book_difference"]) >= 0.01:
        raise ValueError(
            f"Adjusted bank balance differs from the book balance by ${summary['book_difference']:,.2f}."
        )

    stamp = datetime.now().isoformat(timespec="seconds")

    def f(c):
        c.execute(
            """UPDATE bank_reconciliations
               SET status='Reconciled',completed_at=?
               WHERE id=?""",
            (stamp, rid)
        )
        audit_row(
            c, "bank_reconciliation_completed", "bank_reconciliation", rid,
            summary
        )

    write(f)


def statements(start=None,end=None):
    """
    Build Trial Balance, Income Statement, and Balance Sheet from posted GL data.

    V10.5 change:
    - Trial Balance remains activity-based within the selected range.
    - Income Statement remains activity-based within the selected range.
    - Balance Sheet is built AS OF the report end date and includes current-period
      earnings in equity so the accounting equation can be tested explicitly.
    """
    j_all = gl()

    if j_all.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            0.0,
            {
                "total_assets": 0.0,
                "total_liabilities": 0.0,
                "total_equity_before_income": 0.0,
                "current_period_net_income": 0.0,
                "total_equity": 0.0,
                "liabilities_plus_equity": 0.0,
                "difference": 0.0,
            }
        )

    j_all = j_all.copy()
    j_all["_date"] = pd.to_datetime(j_all["date"], errors="coerce")

    # Activity report period.
    j_period = j_all.copy()
    if start is not None:
        j_period = j_period[j_period["_date"] >= pd.to_datetime(start)]
    if end is not None:
        j_period = j_period[j_period["_date"] <= pd.to_datetime(end)]

    def build_balances(df):
        rows = []
        if df.empty:
            return pd.DataFrame(columns=["Account","Type","Group","Debits","Credits","Balance"])

        for acct, g in df.groupby("account"):
            m = account_meta(acct)
            dr = round(float(g.debit.sum()), 2)
            cr = round(float(g.credit.sum()), 2)
            bal = round(dr-cr, 2) if m["natural_balance"]=="Debit" else round(cr-dr, 2)
            rows.append([acct,m["type"],m["group_name"],dr,cr,bal])

        return pd.DataFrame(
            rows,
            columns=["Account","Type","Group","Debits","Credits","Balance"]
        ).sort_values("Account").reset_index(drop=True)

    # Trial Balance and P&L use selected period activity.
    tb = build_balances(j_period)
    pnl = tb[tb.Type.isin(["Revenue","Expense"])].copy() if not tb.empty else tb.copy()

    revenue = float(pnl.loc[pnl.Type=="Revenue","Balance"].sum()) if not pnl.empty else 0.0
    expenses = float(pnl.loc[pnl.Type=="Expense","Balance"].sum()) if not pnl.empty else 0.0
    net = round(revenue - expenses, 2)

    # Balance Sheet is point-in-time as of END DATE, so include all posted GL
    # activity up to the selected end date, not only activity since start date.
    j_asof = j_all.copy()
    if end is not None:
        j_asof = j_asof[j_asof["_date"] <= pd.to_datetime(end)]

    bs_raw = build_balances(j_asof)
    bs = bs_raw[bs_raw.Type.isin(["Asset","Liability","Equity"])].copy() if not bs_raw.empty else bs_raw.copy()

    # Current-period earnings that have not been closed to retained earnings
    # must appear in equity for the balance sheet to balance.
    pnl_asof_period = j_period.copy()
    pnl_asof_tb = build_balances(pnl_asof_period)
    pnl_asof = pnl_asof_tb[pnl_asof_tb.Type.isin(["Revenue","Expense"])].copy() if not pnl_asof_tb.empty else pnl_asof_tb.copy()

    current_revenue = float(pnl_asof.loc[pnl_asof.Type=="Revenue","Balance"].sum()) if not pnl_asof.empty else 0.0
    current_expenses = float(pnl_asof.loc[pnl_asof.Type=="Expense","Balance"].sum()) if not pnl_asof.empty else 0.0
    current_net_income = round(current_revenue - current_expenses, 2)

    if abs(current_net_income) > 0.0001:
        current_income_row = pd.DataFrame([{
            "Account": "Current Period Earnings",
            "Type": "Equity",
            "Group": "Current Earnings",
            "Debits": 0.0,
            "Credits": 0.0,
            "Balance": current_net_income,
        }])
        bs = pd.concat([bs, current_income_row], ignore_index=True)

    total_assets = round(float(bs.loc[bs.Type=="Asset","Balance"].sum()), 2) if not bs.empty else 0.0
    total_liabilities = round(float(bs.loc[bs.Type=="Liability","Balance"].sum()), 2) if not bs.empty else 0.0
    total_equity = round(float(bs.loc[bs.Type=="Equity","Balance"].sum()), 2) if not bs.empty else 0.0
    total_equity_before_income = round(total_equity - current_net_income, 2)
    liabilities_plus_equity = round(total_liabilities + total_equity, 2)
    difference = round(total_assets - liabilities_plus_equity, 2)

    bs_summary = {
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity_before_income": total_equity_before_income,
        "current_period_net_income": current_net_income,
        "total_equity": total_equity,
        "liabilities_plus_equity": liabilities_plus_equity,
        "difference": difference,
    }

    return tb, pnl, bs, net, bs_summary

def create_invoice_record(invoice_no,customer_name,invoice_date,due_date,revenue_account,subtotal,gst=0.0,qst=0.0,memo=""):
    subtotal=round(float(subtotal),2);gst=round(float(gst),2);qst=round(float(qst),2)
    total=round(subtotal+gst+qst,2)
    if subtotal<=0: raise ValueError("Invoice subtotal must be greater than zero.")
    if pd.to_datetime(due_date)<pd.to_datetime(invoice_date): raise ValueError("Due date cannot be before invoice date.")
    if locked(invoice_date): raise ValueError(closed_period_message(invoice_date,"create an invoice"))
    def f(c):
        if c.execute("SELECT 1 FROM invoices WHERE invoice_no=?",(invoice_no,)).fetchone():
            raise ValueError("Invoice number already exists.")
        c.execute("""INSERT INTO invoices(invoice_no,customer_name,invoice_date,due_date,revenue_account,subtotal,gst,qst,total,status,memo,posted)
                     VALUES(?,?,?,?,?,?,?,?,?,'Open',?,0)""",
                  (invoice_no,customer_name,str(invoice_date),str(due_date),revenue_account,subtotal,gst,qst,total,memo))
        iid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        audit_row(c,"invoice_created","invoice",iid,{"invoice_no":invoice_no,"total":total})
        return iid
    return write(f)

def post_invoice_record(iid):
    iid=int(iid)
    def f(c):
        r=c.execute("""SELECT invoice_no,customer_name,invoice_date,revenue_account,subtotal,gst,qst,total,posted
                       FROM invoices WHERE id=?""",(iid,)).fetchone()
        if not r: raise ValueError("Invoice not found.")
        if r[8]: return 0
        if locked(r[2]):
            raise ValueError(closed_period_message(r[2],"post this invoice"))
        rows=[("1100 Accounts Receivable",float(r[7]),0.0),(r[3],0.0,float(r[4]))]
        if r[5]: rows.append(("2300 GST/HST Payable",0.0,float(r[5])))
        if r[6]: rows.append(("2310 QST Payable",0.0,float(r[6])))
        if round(sum(x[1] for x in rows)-sum(x[2] for x in rows),2)!=0:
            raise RuntimeError("Invoice posting is out of balance.")
        stamp=datetime.now().isoformat(timespec="seconds")
        for acct,dr,cr in rows:
            c.execute("""INSERT INTO journal_entries(transaction_id,date,memo,account,debit,credit,source_type,source_id,created_at)
                         VALUES(NULL,?,?,?,?,?,'Invoice',?,?)""",
                      (str(r[2]),f"Invoice {r[0]} - {r[1]}",acct,dr,cr,str(iid),stamp))
        c.execute("UPDATE invoices SET posted=1 WHERE id=?",(iid,))
        audit_row(c,"invoice_posted","invoice",iid,{"rows":len(rows),"total":float(r[7])})
        return len(rows)
    return write(f)

def invoice_payment_summary(iid):
    iid=int(iid)
    d=read(
        """SELECT i.id,i.invoice_no,i.customer_name,i.total,i.status,i.posted,
                  COALESCE((SELECT SUM(amount) FROM invoice_payments p WHERE p.invoice_id=i.id),0) AS paid,
                  COALESCE((SELECT SUM(amount) FROM credit_notes c WHERE c.invoice_id=i.id),0) AS credits
           FROM invoices i WHERE i.id=?""",(iid,)
    )
    if d.empty:
        raise ValueError("Invoice not found.")
    r=d.iloc[0]
    total=round(float(r.total),2)
    paid=round(float(r.paid),2)
    credits=round(float(r.credits),2)
    remaining=round(total-paid-credits,2)
    return {
        "id":iid,"invoice_no":r.invoice_no,"customer_name":r.customer_name,
        "total":total,"paid":paid,"credits":credits,"remaining":remaining,
        "status":r.status,"posted":int(r.posted)
    }


def pay_invoice_record(iid,payment_date,amount):
    iid=int(iid)
    amount=round(float(amount),2)

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if locked(payment_date):
        raise ValueError(closed_period_message(payment_date,"record this customer payment"))

    def f(c):
        r=c.execute(
            """SELECT i.invoice_no,i.customer_name,i.total,i.status,i.posted,
                      COALESCE((SELECT SUM(amount) FROM invoice_payments p WHERE p.invoice_id=i.id),0),
                      COALESCE((SELECT SUM(amount) FROM credit_notes cn WHERE cn.invoice_id=i.id),0)
               FROM invoices i WHERE i.id=?""",(iid,)
        ).fetchone()

        if not r:
            raise ValueError("Invoice not found.")
        if not r[4]:
            raise ValueError("Post the invoice before recording payment.")

        total=round(float(r[2]),2)
        already_paid=round(float(r[5]),2)
        credits=round(float(r[6]),2)
        remaining=round(total-already_paid-credits,2)

        if remaining <= 0.005:
            raise ValueError("Invoice is already fully paid.")
        if amount > remaining + 0.001:
            raise ValueError(
                f"Payment exceeds the remaining invoice balance of ${remaining:,.2f}."
            )

        stamp=datetime.now().isoformat(timespec="seconds")

        c.execute(
            """INSERT INTO invoice_payments(invoice_id,payment_date,amount,created_at)
               VALUES(?,?,?,?)""",
            (iid,str(payment_date),amount,stamp)
        )
        pid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

        for acct,dr,cr in [
            ("1000 Bank",amount,0.0),
            ("1100 Accounts Receivable",0.0,amount)
        ]:
            c.execute(
                """INSERT INTO journal_entries(transaction_id,date,memo,account,debit,credit,
                   source_type,source_id,created_at)
                   VALUES(NULL,?,?,?,?,?,'Invoice Payment',?,?)""",
                (
                    str(payment_date),
                    f"Payment invoice {r[0]} - {r[1]}",
                    acct,dr,cr,str(pid),stamp
                )
            )

        new_paid=round(already_paid+amount,2)
        new_remaining=round(total-new_paid-credits,2)
        status="Paid" if new_remaining <= 0.005 else "Partially Paid"

        c.execute(
            "UPDATE invoices SET status=?,paid_date=? WHERE id=?",
            (status,str(payment_date) if status=="Paid" else None,iid)
        )

        audit_row(
            c,"invoice_payment","invoice",iid,
            {
                "payment_id":pid,
                "payment_date":str(payment_date),
                "amount":amount,
                "paid_to_date":new_paid,
                "remaining":new_remaining,
                "status":status
            }
        )

        return {
            "payment_id":pid,
            "amount":amount,
            "paid_to_date":new_paid,
            "remaining":new_remaining,
            "status":status
        }

    return write(f)

def create_bill_record(bill_no,vendor_name,bill_date,due_date,expense_account,subtotal,gst=0.0,qst=0.0,memo=""):
    subtotal=round(float(subtotal),2);gst=round(float(gst),2);qst=round(float(qst),2)
    total=round(subtotal+gst+qst,2)
    if subtotal<=0: raise ValueError("Bill subtotal must be greater than zero.")
    if pd.to_datetime(due_date)<pd.to_datetime(bill_date): raise ValueError("Due date cannot be before bill date.")
    if locked(bill_date): raise ValueError(closed_period_message(bill_date,"create a bill"))
    def f(c):
        if c.execute("SELECT 1 FROM bills WHERE bill_no=?",(bill_no,)).fetchone():
            raise ValueError("Bill number already exists.")
        c.execute("""INSERT INTO bills(bill_no,vendor_name,bill_date,due_date,expense_account,subtotal,gst,qst,total,status,memo,posted)
                     VALUES(?,?,?,?,?,?,?,?,?,'Open',?,0)""",
                  (bill_no,vendor_name,str(bill_date),str(due_date),expense_account,subtotal,gst,qst,total,memo))
        bid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        audit_row(c,"bill_created","bill",bid,{"bill_no":bill_no,"total":total})
        return bid
    return write(f)

def post_bill_record(bid):
    bid=int(bid)
    def f(c):
        r=c.execute("""SELECT bill_no,vendor_name,bill_date,expense_account,subtotal,gst,qst,total,posted
                       FROM bills WHERE id=?""",(bid,)).fetchone()
        if not r: raise ValueError("Bill not found.")
        if r[8]: return 0
        if locked(r[2]):
            raise ValueError(closed_period_message(r[2],"post this bill"))
        rows=[(r[3],float(r[4]),0.0)]
        if r[5]: rows.append(("1200 GST/HST Receivable",float(r[5]),0.0))
        if r[6]: rows.append(("1210 QST Receivable",float(r[6]),0.0))
        rows.append(("2000 Accounts Payable",0.0,float(r[7])))
        if round(sum(x[1] for x in rows)-sum(x[2] for x in rows),2)!=0:
            raise RuntimeError("Bill posting is out of balance.")
        stamp=datetime.now().isoformat(timespec="seconds")
        for acct,dr,cr in rows:
            c.execute("""INSERT INTO journal_entries(transaction_id,date,memo,account,debit,credit,source_type,source_id,created_at)
                         VALUES(NULL,?,?,?,?,?,'Bill',?,?)""",
                      (str(r[2]),f"Bill {r[0]} - {r[1]}",acct,dr,cr,str(bid),stamp))
        c.execute("UPDATE bills SET posted=1 WHERE id=?",(bid,))
        audit_row(c,"bill_posted","bill",bid,{"rows":len(rows),"total":float(r[7])})
        return len(rows)
    return write(f)

def bill_payment_summary(bid):
    bid=int(bid)
    d=read(
        """SELECT b.id,b.bill_no,b.vendor_name,b.total,b.status,b.posted,
                  COALESCE((SELECT SUM(amount) FROM bill_payments p WHERE p.bill_id=b.id),0) AS paid
           FROM bills b WHERE b.id=?""",(bid,)
    )
    if d.empty:
        raise ValueError("Bill not found.")
    r=d.iloc[0]
    total=round(float(r.total),2)
    paid=round(float(r.paid),2)
    remaining=round(total-paid,2)
    return {
        "id":bid,"bill_no":r.bill_no,"vendor_name":r.vendor_name,
        "total":total,"paid":paid,"remaining":remaining,
        "status":r.status,"posted":int(r.posted)
    }


def pay_bill_record(bid,payment_date,amount):
    bid=int(bid)
    amount=round(float(amount),2)

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if locked(payment_date):
        raise ValueError(closed_period_message(payment_date,"record this vendor payment"))

    def f(c):
        r=c.execute(
            """SELECT b.bill_no,b.vendor_name,b.total,b.status,b.posted,
                      COALESCE((SELECT SUM(amount) FROM bill_payments p WHERE p.bill_id=b.id),0)
               FROM bills b WHERE b.id=?""",(bid,)
        ).fetchone()

        if not r:
            raise ValueError("Bill not found.")
        if not r[4]:
            raise ValueError("Post the bill before recording payment.")

        total=round(float(r[2]),2)
        already_paid=round(float(r[5]),2)
        remaining=round(total-already_paid,2)

        if remaining <= 0.005:
            raise ValueError("Bill is already fully paid.")
        if amount > remaining + 0.001:
            raise ValueError(
                f"Payment exceeds the remaining bill balance of ${remaining:,.2f}."
            )

        stamp=datetime.now().isoformat(timespec="seconds")

        c.execute(
            """INSERT INTO bill_payments(bill_id,payment_date,amount,created_at)
               VALUES(?,?,?,?)""",
            (bid,str(payment_date),amount,stamp)
        )
        pid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

        for acct,dr,cr in [
            ("2000 Accounts Payable",amount,0.0),
            ("1000 Bank",0.0,amount)
        ]:
            c.execute(
                """INSERT INTO journal_entries(transaction_id,date,memo,account,debit,credit,
                   source_type,source_id,created_at)
                   VALUES(NULL,?,?,?,?,?,'Bill Payment',?,?)""",
                (
                    str(payment_date),
                    f"Payment bill {r[0]} - {r[1]}",
                    acct,dr,cr,str(pid),stamp
                )
            )

        new_paid=round(already_paid+amount,2)
        new_remaining=round(total-new_paid,2)
        status="Paid" if new_remaining <= 0.005 else "Partially Paid"

        c.execute(
            "UPDATE bills SET status=?,paid_date=? WHERE id=?",
            (status,str(payment_date) if status=="Paid" else None,bid)
        )

        audit_row(
            c,"bill_payment","bill",bid,
            {
                "payment_id":pid,
                "payment_date":str(payment_date),
                "amount":amount,
                "paid_to_date":new_paid,
                "remaining":new_remaining,
                "status":status
            }
        )

        return {
            "payment_id":pid,
            "amount":amount,
            "paid_to_date":new_paid,
            "remaining":new_remaining,
            "status":status
        }

    return write(f)

def aging(kind,asof):
    if kind=="AR":
        df=read(
            """SELECT i.id,i.invoice_no,i.customer_name,i.due_date,i.total,i.status,
                      COALESCE((SELECT SUM(amount) FROM invoice_payments p WHERE p.invoice_id=i.id),0) AS paid,
                      COALESCE((SELECT SUM(amount) FROM credit_notes c WHERE c.invoice_id=i.id),0) AS credits
               FROM invoices i
               WHERE i.posted=1"""
        )
        namecol="customer_name"
        numcol="invoice_no"
    else:
        df=read(
            """SELECT b.id,b.bill_no,b.vendor_name,b.due_date,b.total,b.status,
                      COALESCE((SELECT SUM(amount) FROM bill_payments p WHERE p.bill_id=b.id),0) AS paid
               FROM bills b
               WHERE b.posted=1"""
        )
        namecol="vendor_name"
        numcol="bill_no"

    if df.empty:
        return df

    df["paid"]=df["paid"].astype(float).round(2)
    if kind=="AR":
        df["credits"]=df["credits"].astype(float).round(2)
        df["outstanding"]=(df["total"].astype(float)-df["paid"]-df["credits"]).round(2)
    else:
        df["outstanding"]=(df["total"].astype(float)-df["paid"]).round(2)
    df=df[df["outstanding"]>0.005].copy()

    if df.empty:
        return df

    df["days_past_due"]=(pd.to_datetime(asof)-pd.to_datetime(df["due_date"])).dt.days

    def bucket(x):
        if x<=0:return "Current"
        if x<=30:return "1-30"
        if x<=60:return "31-60"
        if x<=90:return "61-90"
        return "90+"

    df["aging_bucket"]=df.days_past_due.apply(bucket)

    return df[
        ([numcol,namecol,"due_date","total","paid","credits","outstanding","days_past_due","aging_bucket","status"]
         if kind=="AR" else
         [numcol,namecol,"due_date","total","paid","outstanding","days_past_due","aging_bucket","status"])
    ]

def tax_summary(start,end):
    """
    Bookkeeping tax summary from POSTED GL tax accounts only.
    """
    j = gl()
    if j.empty:
        return {
            "GST/HST collected":0.0,
            "GST/HST ITCs":0.0,
            "GST/HST net payable":0.0,
            "QST collected":0.0,
            "QST ITRs":0.0,
            "QST net payable":0.0
        }

    d = j[
        (pd.to_datetime(j.date)>=pd.to_datetime(start)) &
        (pd.to_datetime(j.date)<=pd.to_datetime(end))
    ]

    def sums(acct):
        x = d[d.account==acct]
        return float(x.debit.sum()), float(x.credit.sum())

    gst_rec = sums("1200 GST/HST Receivable")
    qst_rec = sums("1210 QST Receivable")
    gst_pay = sums("2300 GST/HST Payable")
    qst_pay = sums("2310 QST Payable")

    gst_itc = round(gst_rec[0]-gst_rec[1], 2)
    qst_itr = round(qst_rec[0]-qst_rec[1], 2)
    gst_collected = round(gst_pay[1]-gst_pay[0], 2)
    qst_collected = round(qst_pay[1]-qst_pay[0], 2)

    return {
        "GST/HST collected":gst_collected,
        "GST/HST ITCs":gst_itc,
        "GST/HST net payable":round(gst_collected-gst_itc,2),
        "QST collected":qst_collected,
        "QST ITRs":qst_itr,
        "QST net payable":round(qst_collected-qst_itr,2)
    }

def integrity():
    issues=[]
    j=gl()
    if not j.empty:
        diff=round(float(j.debit.sum()-j.credit.sum()),2)
        if diff:issues.append(f"General Ledger is out of balance by ${diff:,.2f}.")
        bad=j[(j.debit<0)|(j.credit<0)]
        if not bad.empty:issues.append(f"{len(bad)} journal row(s) contain negative debit/credit values.")
        both=j[(j.debit>0)&(j.credit>0)]
        if not both.empty:issues.append(f"{len(both)} journal row(s) contain both a debit and a credit.")
    t=trans()
    if not t.empty:
        dup=t[t.duplicated(subset=["fingerprint"],keep=False)]
        if not dup.empty:issues.append(f"{len(dup)} saved transaction row(s) share duplicate fingerprints.")
        unc=t[(t.account=="6999 Uncategorized Expense")|(t.status!="Ready for books")]
        if not unc.empty:issues.append(f"{len(unc)} transaction(s) remain unresolved/uncategorized.")
    return issues

def close_checks(period_end):
    t=trans();j=gl();pe=pd.to_datetime(period_end)
    pt=t[pd.to_datetime(t.date,errors="coerce")<=pe] if not t.empty else t
    pj=j[pd.to_datetime(j.date,errors="coerce")<=pe] if not j.empty else j
    posted=set(pj.transaction_id.dropna().astype(int)) if not pj.empty else set()
    unresolved=pt[(pt.status!="Ready for books")|(pt.review==1)] if not pt.empty else pt
    unc=pt[(pt.account=="6999 Uncategorized Expense")|pt.category.isin(["Needs Review","Uncategorized"])] if not pt.empty else pt
    unrec=pt[pt.reconciled==0] if not pt.empty else pt
    ready=set(pt.loc[pt.status=="Ready for books","id"].astype(int)) if not pt.empty else set()
    diff=round(float(pj.debit.sum()-pj.credit.sum()),2) if not pj.empty else 0
    return {"No unresolved transactions":len(unresolved)==0,"All bank transactions reconciled":len(unrec)==0,
    "No uncategorized transactions":len(unc)==0,"All ready transactions posted":len(ready-posted)==0,
    "General Ledger balanced":abs(diff)<.01}, {"unresolved":len(unresolved),"unreconciled":len(unrec),"uncategorized":len(unc),
    "unposted_ready":len(ready-posted),"gl_difference":diff}

def lock_period(period_end,checks):
    if not all(checks.values()):raise ValueError("All automatic close checks must pass.")
    def f(c):
        c.execute("""INSERT INTO close_periods(period_end,status,checklist_json,locked,closed_at) VALUES(?,'Closed',?,1,?)
        ON CONFLICT(period_end) DO UPDATE SET status='Closed',checklist_json=excluded.checklist_json,locked=1,closed_at=excluded.closed_at""",
        (str(period_end),json.dumps(checks),datetime.now().isoformat(timespec="seconds")))
        c.execute("UPDATE transactions SET period_locked=1 WHERE date<=?",(str(period_end),))
        audit_row(c,"period_locked","period",period_end,checks)
    write(f)

def export_package():
    tb,pnl,bs,net,bs_summary=statements()
    datasets={"transactions.csv":trans(),"general_ledger.csv":gl(),"audit_log.csv":audits(),"counterparties.csv":cps(),
    "invoices.csv":invs(),"bills.csv":bills(),"documents.csv":docs(),"trial_balance.csv":tb,"income_statement.csv":pnl,"balance_sheet.csv":bs}
    for n,d in datasets.items():d.to_csv(EXPORT_DIR/n,index=False)
    (EXPORT_DIR/"summary.json").write_text(json.dumps({"generated_at":datetime.now().isoformat(),"net_income":float(net),"balance_sheet_summary":bs_summary,"integrity_issues":integrity()},indent=2))
    zpath=APP_DIR/"sullivan_v15_4_accountant_package.zip"
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED) as z:
        for p in EXPORT_DIR.iterdir():z.write(p,p.name)
        for p in DOC_DIR.iterdir():
            if p.is_file():z.write(p,f"documents/{p.name}")
    log("accountant_export","export","package",{"path":str(zpath)});return zpath

def v13_home_metrics():
    j=gl()
    today=pd.Timestamp(date.today())
    month_start=today.replace(day=1)
    bank=revenue=expenses=0.0

    if not j.empty:
        bank_rows=j[j.account=="1000 Bank"]
        bank=round(float((bank_rows.debit-bank_rows.credit).sum()),2)
        jd=pd.to_datetime(j.date,errors="coerce")
        month=j[(jd>=month_start)&(jd<=today)]
        if not month.empty:
            for acct,g in month.groupby("account"):
                meta=account_meta(acct)
                dr=float(g.debit.sum()); cr=float(g.credit.sum())
                bal=(cr-dr) if meta["natural_balance"]=="Credit" else (dr-cr)
                if meta["type"]=="Revenue": revenue+=bal
                elif meta["type"]=="Expense": expenses+=bal

    ar=aging("AR",date.today())
    ap=aging("AP",date.today())
    ar_total=round(float(ar.outstanding.sum()),2) if not ar.empty else 0.0
    ap_total=round(float(ap.outstanding.sum()),2) if not ap.empty else 0.0

    tax=tax_summary(date(date.today().year,1,1),date.today())
    tax_due=round(float(tax.get("GST/HST net payable",0))+float(tax.get("QST net payable",0)),2)

    overdue_count=0; overdue_total=0.0
    if not ar.empty:
        overdue=ar[ar.days_past_due>0]
        overdue_count=len(overdue)
        overdue_total=round(float(overdue.outstanding.sum()),2) if not overdue.empty else 0.0

    due_ap_count=0; due_ap_total=0.0
    if not ap.empty:
        due=ap[ap.days_past_due>=-7]
        due_ap_count=len(due)
        due_ap_total=round(float(due.outstanding.sum()),2) if not due.empty else 0.0

    t=trans()
    unreconciled=0
    if not t.empty and "reconciled" in t.columns:
        unreconciled=int((t.reconciled.fillna(0).astype(int)==0).sum())

    return dict(
        bank=bank,revenue=round(revenue,2),expenses=round(expenses,2),
        profit=round(revenue-expenses,2),ar=ar_total,ap=ap_total,tax_due=tax_due,
        overdue_count=overdue_count,overdue_total=overdue_total,
        due_ap_count=due_ap_count,due_ap_total=due_ap_total,
        unreconciled=unreconciled
    )


def v14_business_name():
    try:
        name=get_setting("business_name","")
    except Exception:
        name=""
    return name.strip() or "Your Business"

def v14_recent_activity(limit=5):
    rows=[]
    try:
        inv=read("""SELECT invoice_no,customer_name,invoice_date,total,status FROM invoices ORDER BY id DESC LIMIT 5""")
        for _,r in inv.iterrows():
            rows.append({
                "date":str(r.invoice_date),
                "kind":"Invoice",
                "title":f"Invoice {r.invoice_no}",
                "detail":f"{r.customer_name} • {r.status}",
                "amount":float(r.total)
            })
    except Exception:
        pass
    try:
        bills=read("""SELECT bill_no,vendor_name,bill_date,total,status FROM bills ORDER BY id DESC LIMIT 5""")
        for _,r in bills.iterrows():
            rows.append({
                "date":str(r.bill_date),
                "kind":"Bill",
                "title":f"Bill {r.bill_no}",
                "detail":f"{r.vendor_name} • {r.status}",
                "amount":-float(r.total)
            })
    except Exception:
        pass
    rows=sorted(rows,key=lambda x:x["date"],reverse=True)
    return rows[:limit]

def v14_cash_forecast(days=30):
    start=pd.Timestamp(date.today())
    dates=pd.date_range(start,start+pd.Timedelta(days=days),freq="D")
    m=v13_home_metrics()
    start_cash=float(m["bank"])
    incoming={d.date():0.0 for d in dates}
    outgoing={d.date():0.0 for d in dates}

    ar=aging("AR",date.today())
    if not ar.empty:
        for _,r in ar.iterrows():
            try:
                d=pd.to_datetime(r.due_date).date()
                if d in incoming:
                    incoming[d]+=float(r.outstanding)
            except Exception:
                pass

    ap=aging("AP",date.today())
    if not ap.empty:
        for _,r in ap.iterrows():
            try:
                d=pd.to_datetime(r.due_date).date()
                if d in outgoing:
                    outgoing[d]+=float(r.outstanding)
            except Exception:
                pass

    cash=[]
    inc=[]
    out=[]
    bal=start_cash
    for d in dates:
        i=incoming[d.date()]
        o=outgoing[d.date()]
        bal+=i-o
        cash.append(bal)
        inc.append(i)
        out.append(o)

    return pd.DataFrame({
        "date":dates,
        "Cash balance":cash,
        "Money in":inc,
        "Money out":out,
    })


# ==========================
# Sullivan V17 accounts
# ==========================

def _pwd_hash(password, salt=None):
    if not password:
        raise ValueError("Password cannot be blank.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()

def _pwd_verify(password, salt_b64, hash_b64):
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def _new_company_id():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "SUL-COMP-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if read("SELECT id FROM companies WHERE company_code=?", (code,)).empty:
            return code

def _new_user_id():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "SUL-USER-" + "".join(secrets.choice(alphabet) for _ in range(10))
        if read("SELECT id FROM app_users WHERE user_code=?", (code,)).empty:
            return code

def _new_invite_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "JOIN-" + "".join(secrets.choice(alphabet) for _ in range(10))
        if read("SELECT id FROM company_invites WHERE invite_code=?", (code,)).empty:
            return code

def v17_init_auth_tables():
    def f(c):
        c.execute("""CREATE TABLE IF NOT EXISTS companies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT UNIQUE NOT NULL,
            company_name TEXT NOT NULL,
            owner_user_id INTEGER,
            subscription_plan TEXT DEFAULT 'Trial',
            status TEXT DEFAULT 'Active',
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS app_users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_code TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            personal_account INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS company_members(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'Employee',
            status TEXT DEFAULT 'Active',
            joined_at TEXT NOT NULL,
            UNIQUE(company_id,user_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS company_invites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invite_code TEXT UNIQUE NOT NULL,
            company_id INTEGER NOT NULL,
            invited_email TEXT,
            role TEXT NOT NULL DEFAULT 'Employee',
            status TEXT DEFAULT 'Active',
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            used_by_user_id INTEGER,
            used_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS workspace_migrations(
            migration_key TEXT PRIMARY KEY,
            workspace_key TEXT,
            created_at TEXT NOT NULL,
            details TEXT
        )""")
        # V18.3 migrations for Google/OIDC identities.
        user_cols = [r[1] for r in c.execute("PRAGMA table_info(app_users)").fetchall()]
        if "auth_provider" not in user_cols:
            c.execute("ALTER TABLE app_users ADD COLUMN auth_provider TEXT DEFAULT 'email'")
        if "provider_subject" not in user_cols:
            c.execute("ALTER TABLE app_users ADD COLUMN provider_subject TEXT")
        if "last_workspace_company_id" not in user_cols:
            c.execute("ALTER TABLE app_users ADD COLUMN last_workspace_company_id INTEGER")
        if "last_workspace_mode" not in user_cols:
            c.execute("ALTER TABLE app_users ADD COLUMN last_workspace_mode TEXT DEFAULT 'Personal'")
        if "ui_theme" not in user_cols:
            c.execute("ALTER TABLE app_users ADD COLUMN ui_theme TEXT DEFAULT 'Light'")

        # V18 membership / AI-credit migrations.
        company_cols = [r[1] for r in c.execute("PRAGMA table_info(companies)").fetchall()]
        company_wanted = {
            "subscription_status": "TEXT DEFAULT 'Trial'",
            "ai_credit_limit": "INTEGER DEFAULT 0",
            "ai_credits_used": "INTEGER DEFAULT 0",
            "ai_period_start": "TEXT",
            "seat_limit": "INTEGER DEFAULT 1",
            "demo_used": "INTEGER DEFAULT 0",
            "stripe_customer_id": "TEXT",
            "stripe_subscription_id": "TEXT",
        }
        for col, ctype in company_wanted.items():
            if col not in company_cols:
                c.execute(f"ALTER TABLE companies ADD COLUMN {col} {ctype}")

        # Backfill sensible values for companies created before V18.
        c.execute("""UPDATE companies
                     SET subscription_status=COALESCE(subscription_status,'Trial'),
                         ai_credit_limit=COALESCE(ai_credit_limit,0),
                         ai_credits_used=COALESCE(ai_credits_used,0),
                         seat_limit=CASE WHEN seat_limit IS NULL OR seat_limit<1 THEN 1 ELSE seat_limit END,
                         demo_used=COALESCE(demo_used,0)""")

        c.execute("""CREATE TABLE IF NOT EXISTS ai_usage(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            user_id INTEGER,
            action TEXT NOT NULL,
            credits INTEGER NOT NULL DEFAULT 0,
            source TEXT DEFAULT 'subscription',
            detail TEXT,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ai_demo_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT,
            account TEXT,
            confidence REAL DEFAULT 0,
            explanation TEXT,
            question TEXT,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS enterprise_quotes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            requested_by_user_id INTEGER,
            requested_seats INTEGER NOT NULL,
            expected_ai_usage TEXT,
            estimated_monthly_price REAL,
            quote_summary TEXT,
            status TEXT DEFAULT 'Estimate',
            created_at TEXT NOT NULL
        )""")
    write(f)

def create_app_user(full_name,email,password,personal_account=True):
    full_name=(full_name or "").strip()
    email=(email or "").strip().lower()
    if not full_name: raise ValueError("Enter your name.")
    if "@" not in email: raise ValueError("Enter a valid email address.")
    if len(password or "") < 8: raise ValueError("Password must be at least 8 characters.")
    if not read("SELECT id FROM app_users WHERE email=?",(email,)).empty:
        raise ValueError("An account with this email already exists.")
    salt,h=_pwd_hash(password)
    user_code=_new_user_id()
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO app_users(user_code,email,full_name,password_salt,password_hash,personal_account,active,created_at)
                     VALUES(?,?,?,?,?,?,1,?)""",
                  (user_code,email,full_name,salt,h,1 if personal_account else 0,stamp))
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    uid=write(f)
    return uid,user_code


def _streamlit_oidc_logged_in():
    """Return True only when Streamlit native OIDC is configured and authenticated."""
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False

def _streamlit_user_value(key, default=""):
    try:
        value = st.user.get(key, default)
    except Exception:
        try:
            value = getattr(st.user, key, default)
        except Exception:
            value = default
    return value or default

def sync_google_user():
    """
    Link a successfully authenticated Google/OIDC identity to Sullivan's local
    user record. The Google identity is trusted from Streamlit's OIDC cookie,
    not from user-entered form fields.
    """
    if not _streamlit_oidc_logged_in():
        return None

    email = str(_streamlit_user_value("email", "")).strip().lower()
    full_name = str(_streamlit_user_value("name", "")).strip()
    subject = str(_streamlit_user_value("sub", "")).strip()

    if not email or "@" not in email:
        raise ValueError("Google did not provide an email address Sullivan can use.")

    if not full_name:
        full_name = email.split("@", 1)[0]

    existing = read(
        """SELECT id,user_code,email,full_name,personal_account,active,
                  COALESCE(auth_provider,'email') AS auth_provider,
                  provider_subject
           FROM app_users WHERE email=?""",
        (email,)
    )

    stamp = datetime.now().isoformat(timespec="seconds")

    if existing.empty:
        # app_users was originally built for email/password, so Google accounts
        # receive an unusable random password hash while authentication remains
        # controlled entirely by Google OIDC.
        salt, password_hash = _pwd_hash(secrets.token_urlsafe(48))
        user_code = _new_user_id()

        def create_google_user(c):
            c.execute(
                """INSERT INTO app_users(
                       user_code,email,full_name,password_salt,password_hash,
                       personal_account,active,created_at,last_login_at,
                       auth_provider,provider_subject
                   ) VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    user_code, email, full_name, salt, password_hash,
                    1, stamp, stamp, "google", subject
                )
            )
            return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

        uid = write(create_google_user)
        return {
            "id": uid,
            "user_code": user_code,
            "email": email,
            "full_name": full_name,
            "personal_account": True,
            "auth_provider": "google",
        }

    r = existing.iloc[0]
    if not int(r.active):
        raise ValueError("This Sullivan account is disabled.")

    uid = int(r.id)

    def update_google_user(c):
        c.execute(
            """UPDATE app_users
               SET full_name=?,last_login_at=?,auth_provider='google',
                   provider_subject=COALESCE(NULLIF(?,''),provider_subject)
               WHERE id=?""",
            (full_name, stamp, subject, uid)
        )

    write(update_google_user)

    return {
        "id": uid,
        "user_code": r.user_code,
        "email": email,
        "full_name": full_name,
        "personal_account": bool(int(r.personal_account)),
        "auth_provider": "google",
    }

def _workspace_company_dict(row):
    return {
        "company_id": int(row.company_id),
        "company_code": row.company_code,
        "company_name": row.company_name,
        "subscription_plan": row.subscription_plan,
    }


def save_workspace_preference(user_id, company_id=None):
    """
    Persist the user's last confirmed workspace.
    Personal is stored explicitly so a refresh/login does not silently jump
    back into a company unless that is what the user last confirmed.
    """
    uid = int(user_id)
    if company_id is None:
        write(lambda c: c.execute(
            """UPDATE app_users
               SET last_workspace_mode='Personal',
                   last_workspace_company_id=NULL
               WHERE id=?""",
            (uid,)
        ))
        return

    write(lambda c: c.execute(
        """UPDATE app_users
           SET last_workspace_mode='Company',
               last_workspace_company_id=?
           WHERE id=?""",
        (int(company_id), uid)
    ))


def activate_workspace(user, company_row=None, persist=True):
    """Apply one confirmed workspace selection to the entire Sullivan session."""
    if not user:
        return

    if company_row is None:
        st.session_state["auth_company"] = None
        st.session_state["auth_role"] = "Personal"
        if persist:
            save_workspace_preference(user["id"], None)
        ensure_current_workspace_books()
        return

    st.session_state["auth_company"] = _workspace_company_dict(company_row)
    st.session_state["auth_role"] = str(company_row.role)
    if persist:
        save_workspace_preference(user["id"], int(company_row.company_id))
    ensure_current_workspace_books()


def load_default_workspace_for_user(user):
    """
    Restore the user's last CONFIRMED workspace first.

    If an older account has no saved preference yet:
      • exactly one active company membership -> use that company
      • otherwise -> Personal

    This avoids silently changing workspaces on ordinary Streamlit reruns.
    """
    if not user:
        return

    # If the current Streamlit session already has a valid workspace, keep it.
    # This prevents Google/OIDC sync on every rerun from changing the workspace.
    existing_company = st.session_state.get("auth_company")
    existing_role = st.session_state.get("auth_role")
    if existing_company is not None and existing_role not in (None, "", "Guest"):
        return
    if existing_company is None and existing_role == "Personal":
        return

    memberships = company_memberships(user["id"])

    pref = read(
        """SELECT COALESCE(last_workspace_mode,'') AS last_workspace_mode,
                  last_workspace_company_id
           FROM app_users
           WHERE id=?""",
        (int(user["id"]),)
    )

    pref_mode = ""
    pref_company_id = None
    if not pref.empty:
        pref_mode = str(pref.iloc[0].last_workspace_mode or "")
        raw_id = pref.iloc[0].last_workspace_company_id
        if pd.notna(raw_id):
            pref_company_id = int(raw_id)

    if pref_mode == "Company" and pref_company_id is not None and not memberships.empty:
        match = memberships[memberships.company_id == pref_company_id]
        if not match.empty:
            activate_workspace(user, match.iloc[0], persist=False)
            return

    if pref_mode == "Personal":
        activate_workspace(user, None, persist=False)
        return

    # Backward-compatible first-time default for users created before V19.2.
    if len(memberships) == 1:
        activate_workspace(user, memberships.iloc[0], persist=True)
    else:
        activate_workspace(user, None, persist=True)


def get_user_ui_theme(user_id):
    if not user_id:
        return "Light"
    c = raw_connect()
    try:
        row = c.execute(
            "SELECT COALESCE(ui_theme,'Light') FROM app_users WHERE id=?",
            (int(user_id),)
        ).fetchone()
        theme = str(row[0] or "Light") if row else "Light"
        return theme if theme in ("Light", "Dark") else "Light"
    finally:
        c.close()


def save_user_ui_theme(user_id, theme):
    theme = str(theme or "Light")
    if theme not in ("Light", "Dark"):
        raise ValueError("Unknown display theme.")
    c = raw_connect()
    try:
        c.execute(
            "UPDATE app_users SET ui_theme=? WHERE id=?",
            (theme, int(user_id))
        )
    finally:
        c.close()


def rename_company_workspace(user_id, company_id, new_name):
    uid = int(user_id)
    cid = int(company_id)
    new_name = str(new_name or "").strip()

    if not new_name:
        raise ValueError("Enter a company name.")
    if len(new_name) > 120:
        raise ValueError("Company name is too long.")

    c = raw_connect()
    try:
        owner = c.execute(
            """SELECT 1 FROM company_members
               WHERE company_id=? AND user_id=? AND role='Owner' AND status='Active'
               LIMIT 1""",
            (cid, uid)
        ).fetchone()
        if not owner:
            raise ValueError("Only a company Owner can rename this workspace.")

        company = c.execute(
            "SELECT company_name FROM companies WHERE id=?",
            (cid,)
        ).fetchone()
        if not company:
            raise ValueError("Company workspace not found.")

        old_name = str(company[0])
        c.execute(
            "UPDATE companies SET company_name=? WHERE id=?",
            (new_name, cid)
        )
        return old_name, new_name
    finally:
        c.close()


def owned_company_workspaces(user_id):
    c = raw_connect()
    try:
        rows = c.execute(
            """SELECT c.id,c.company_code,c.company_name,c.subscription_plan,
                      c.subscription_status,c.stripe_customer_id,c.stripe_subscription_id,
                      m.role,c.created_at
               FROM company_members m
               JOIN companies c ON c.id=m.company_id
               WHERE m.user_id=? AND m.status='Active' AND c.status='Active'
                     AND m.role='Owner'
               ORDER BY c.company_name,c.id""",
            (int(user_id),)
        ).fetchall()
    finally:
        c.close()

    cols = [
        "company_id","company_code","company_name","subscription_plan",
        "subscription_status","stripe_customer_id","stripe_subscription_id",
        "role","created_at"
    ]
    return pd.DataFrame(rows, columns=cols)


def authenticate_user(email,password):
    email=(email or "").strip().lower()
    d=read("""SELECT id,user_code,email,full_name,password_salt,password_hash,personal_account,active
              FROM app_users WHERE email=?""",(email,))
    if d.empty: return None
    r=d.iloc[0]
    if not int(r.active): return None
    if not _pwd_verify(password,str(r.password_salt),str(r.password_hash)): return None
    write(lambda c:c.execute("UPDATE app_users SET last_login_at=? WHERE id=?",(datetime.now().isoformat(timespec="seconds"),int(r.id))))
    return {
        "id":int(r.id),"user_code":r.user_code,"email":r.email,"full_name":r.full_name,
        "personal_account":bool(int(r.personal_account)),
        "auth_provider":"email"
    }

def create_company_for_user(user_id,company_name):
    company_name=(company_name or "").strip()
    if not company_name: raise ValueError("Enter a company name.")
    company_code=_new_company_id()
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO companies(
                         company_code,company_name,owner_user_id,subscription_plan,status,created_at,
                         subscription_status,ai_credit_limit,ai_credits_used,ai_period_start,seat_limit,demo_used
                     ) VALUES(?,?,?,'Trial','Active',?,'Trial',0,0,?,1,0)""",
                  (company_code,company_name,int(user_id),stamp,date.today().replace(day=1).isoformat()))
        cid=int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        c.execute("""INSERT INTO company_members(company_id,user_id,role,status,joined_at)
                     VALUES(?,?,'Owner','Active',?)""",(cid,int(user_id),stamp))
        return cid
    cid=write(f)
    return cid,company_code

def company_memberships(user_id):
    return read("""SELECT c.id AS company_id,c.company_code,c.company_name,c.subscription_plan,c.status,
                         c.subscription_status,c.ai_credit_limit,c.ai_credits_used,c.ai_period_start,
                         c.seat_limit,c.demo_used,
                         m.role,m.joined_at
                  FROM company_members m
                  JOIN companies c ON c.id=m.company_id
                  WHERE m.user_id=? AND m.status='Active' AND c.status='Active'
                  ORDER BY c.company_name""",(int(user_id),))


def _raw_table_exists(c, table_name):
    return bool(
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(table_name),),
        ).fetchone()
    )


def _legacy_books_have_data(c):
    """
    Only migrate legacy books when they contain meaningful user bookkeeping data.
    Default chart-of-account seed rows alone do not count.
    """
    probes = (
        "transactions",
        "journal_entries",
        "invoices",
        "bills",
        "customers",
        "vendors",
        "manual_journals",
        "estimates",
        "purchase_orders",
        "documents",
    )
    for table in probes:
        if _raw_table_exists(c, table):
            try:
                n = int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if n > 0:
                    return True
            except Exception:
                pass

    # A customized business profile also counts as legacy business data.
    if _raw_table_exists(c, "business_profile"):
        try:
            row = c.execute(
                """SELECT business_name,industry,gst_registered,qst_registered
                   FROM business_profile WHERE id=1"""
            ).fetchone()
            if row and (
                str(row[0] or "").strip()
                or str(row[1] or "").strip()
                or int(row[2] or 0)
                or int(row[3] or 0)
            ):
                return True
        except Exception:
            pass

    return False


def ensure_current_workspace_books():
    """
    Initialize the active workspace's accounting schema and perform the one-time
    V19.3 migration of pre-workspace Sullivan books.

    Legacy accounting data is migrated only into a COMPANY workspace, never into
    Personal. That makes Personal start as a genuinely separate clean account.
    """
    workspace_key = _workspace_storage_key()
    if not workspace_key:
        return

    # Running init_db() while a workspace is active causes every accounting-table
    # CREATE/ALTER/seed statement to be transparently rewritten to that workspace.
    init_db()

    # Legacy migration applies once per Sullivan deployment.
    c = raw_connect()
    try:
        c.execute(
            """CREATE TABLE IF NOT EXISTS workspace_migrations(
                migration_key TEXT PRIMARY KEY,
                workspace_key TEXT,
                created_at TEXT NOT NULL,
                details TEXT
            )"""
        )

        done = c.execute(
            "SELECT workspace_key FROM workspace_migrations WHERE migration_key='v19_3_legacy_books'"
        ).fetchone()

        # Do not move old shared bookkeeping into Personal. Wait until the user is
        # inside a company workspace, which is where pre-V19 company books belonged.
        is_company = workspace_key.startswith("c")

        if not done and is_company and _legacy_books_have_data(c):
            for table in WORKSPACE_ACCOUNTING_TABLES:
                source = table
                target = _workspace_table_name(table, workspace_key)

                if not _raw_table_exists(c, source) or not _raw_table_exists(c, target):
                    continue

                # The scoped schema has already been created by init_db().
                # Replace its seed/default rows with the exact legacy data.
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.execute(f'DELETE FROM "{target}"')
                    c.execute(
                        f'INSERT INTO "{target}" SELECT * FROM "{source}"'
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise

            c.execute(
                """INSERT OR REPLACE INTO workspace_migrations(
                       migration_key,workspace_key,created_at,details
                   ) VALUES('v19_3_legacy_books',?,?,?)""",
                (
                    workspace_key,
                    datetime.now().isoformat(timespec="seconds"),
                    "Pre-V19 accounting data moved into the first confirmed company workspace.",
                ),
            )
    finally:
        c.close()


def delete_company_workspace(user_id, company_id, confirmation_name):
    """
    Permanently delete an accidental/unneeded company workspace.

    Safety:
      • Owner only.
      • Exact company-name confirmation required.
      • Active paid Stripe-backed companies cannot be deleted here.
    """
    uid = int(user_id)
    cid = int(company_id)
    confirmation_name = str(confirmation_name or "").strip()

    c = raw_connect()
    try:
        company = c.execute(
            """SELECT company_name,subscription_plan,subscription_status,
                      stripe_customer_id,stripe_subscription_id
               FROM companies WHERE id=?""",
            (cid,),
        ).fetchone()

        if not company:
            raise ValueError("Company workspace not found.")

        company_name, plan, sub_status, stripe_customer, stripe_subscription = company

        owner = c.execute(
            """SELECT 1 FROM company_members
               WHERE company_id=? AND user_id=? AND role='Owner' AND status='Active'
               LIMIT 1""",
            (cid, uid),
        ).fetchone()

        if not owner:
            raise ValueError("Only a company Owner can delete this workspace.")

        if confirmation_name != str(company_name):
            raise ValueError("Type the company name exactly to confirm deletion.")

        paid_or_stripe_backed = (
            str(plan or "Trial") != "Trial"
            or bool(str(stripe_customer or "").strip())
            or bool(str(stripe_subscription or "").strip())
            or str(sub_status or "").lower() in ("active", "trialing", "past_due")
        )

        if paid_or_stripe_backed:
            raise ValueError(
                "This company has billing/subscription history. Cancel the Stripe "
                "subscription first, then delete the workspace after it is no longer active."
            )

        workspace_key = f"c{cid}"

        c.execute("BEGIN IMMEDIATE")
        try:
            # Drop only this company's physical bookkeeping tables.
            for table in WORKSPACE_ACCOUNTING_TABLES:
                physical = _workspace_table_name(table, workspace_key)
                c.execute(f'DROP TABLE IF EXISTS "{physical}"')

            c.execute("DELETE FROM company_invites WHERE company_id=?", (cid,))
            c.execute("DELETE FROM ai_usage WHERE company_id=?", (cid,))
            c.execute("DELETE FROM ai_demo_results WHERE company_id=?", (cid,))
            c.execute("DELETE FROM enterprise_quotes WHERE company_id=?", (cid,))
            c.execute("DELETE FROM company_members WHERE company_id=?", (cid,))
            c.execute("DELETE FROM companies WHERE id=?", (cid,))
            c.execute(
                """UPDATE app_users
                   SET last_workspace_mode='Personal',
                       last_workspace_company_id=NULL
                   WHERE last_workspace_company_id=?""",
                (cid,),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    finally:
        c.close()

    return str(company_name)

def create_company_invite(company_id,creator_user_id,role="Employee",email=""):
    role=role if role in ("Owner","Accountant","Manager","Employee") else "Employee"
    email=(email or "").strip().lower()
    cid=int(company_id)

    company=read("""SELECT seat_limit,subscription_plan,subscription_status
                    FROM companies WHERE id=?""",(cid,))
    if company.empty:
        raise ValueError("Company not found.")
    seat_limit=max(1,int(company.iloc[0].seat_limit or 1))

    member_count=read("""SELECT COUNT(*) AS n FROM company_members
                         WHERE company_id=? AND status='Active'""",(cid,))
    active_members=int(member_count.iloc[0].n)

    open_invites=read("""SELECT COUNT(*) AS n FROM company_invites
                         WHERE company_id=? AND status='Active'""",(cid,))
    reserved=int(open_invites.iloc[0].n)

    if active_members + reserved >= seat_limit:
        raise ValueError(
            f"This company has reached its {seat_limit}-seat plan limit. "
            "Upgrade the plan or cancel an unused invite before adding another employee."
        )

    code=_new_invite_code()
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO company_invites(invite_code,company_id,invited_email,role,status,created_by_user_id,created_at)
                     VALUES(?,?,?,?, 'Active', ?, ?)""",
                  (code,cid,email,role,int(creator_user_id),stamp))
    write(f)
    return code

def join_company_with_invite(user_id,user_email,invite_code):
    invite_code=(invite_code or "").strip().upper()
    d=read("""SELECT * FROM company_invites WHERE invite_code=? AND status='Active'""",(invite_code,))
    if d.empty: raise ValueError("That company invite code is invalid or has already been used.")
    r=d.iloc[0]
    invited_email=(str(r.invited_email) if pd.notna(r.invited_email) else "").strip().lower()
    if invited_email and invited_email != (user_email or "").strip().lower():
        raise ValueError("This invite was issued to a different email address.")
    stamp=datetime.now().isoformat(timespec="seconds")

    # Re-check seat availability when the invite is actually redeemed.
    company=read("SELECT seat_limit FROM companies WHERE id=?",(int(r.company_id),))
    seat_limit=max(1,int(company.iloc[0].seat_limit or 1)) if not company.empty else 1
    existing_member=read("""SELECT id FROM company_members
                            WHERE company_id=? AND user_id=?""",(int(r.company_id),int(user_id)))
    if existing_member.empty:
        member_count=read("""SELECT COUNT(*) AS n FROM company_members
                             WHERE company_id=? AND status='Active'""",(int(r.company_id),))
        if int(member_count.iloc[0].n) >= seat_limit:
            raise ValueError("This company's Sullivan plan has no employee seats remaining.")

    def f(c):
        exists=c.execute("SELECT id FROM company_members WHERE company_id=? AND user_id=?",
                         (int(r.company_id),int(user_id))).fetchone()
        if exists:
            c.execute("UPDATE company_members SET role=?,status='Active' WHERE id=?",(r.role,int(exists[0])))
        else:
            c.execute("""INSERT INTO company_members(company_id,user_id,role,status,joined_at)
                         VALUES(?,?,?,'Active',?)""",(int(r.company_id),int(user_id),r.role,stamp))
        c.execute("""UPDATE company_invites SET status='Used',used_by_user_id=?,used_at=? WHERE id=?""",
                  (int(user_id),stamp,int(r.id)))
    write(f)
    c=read("SELECT company_code,company_name FROM companies WHERE id=?",(int(r.company_id),))
    return c.iloc[0].to_dict()


def v18_company_billing(company_id):
    """
    V19 billing reader.
    SQLite remains the local workspace store, but Supabase is authoritative
    for Stripe-backed subscription state whenever a valid remote row exists.
    """
    cid = int(company_id)

    d = read(
        """SELECT id,company_code,company_name,subscription_plan,subscription_status,
                  ai_credit_limit,ai_credits_used,ai_period_start,seat_limit,demo_used,
                  stripe_customer_id,stripe_subscription_id
           FROM companies WHERE id=?""",
        (cid,),
    )

    if d.empty:
        return None

    result = d.iloc[0].to_dict()
    remote = v19_supabase_subscription(cid)

    remote_ok = bool(
        remote
        and remote.get("_diagnostic") == "connected"
        and remote.get("plan")
    )

    if remote_ok:
        result["subscription_plan"] = str(remote.get("plan") or "Trial")
        result["subscription_status"] = str(remote.get("subscription_status") or "Trial")
        result["ai_credit_limit"] = int(remote.get("ai_credits") or 0)
        result["seat_limit"] = max(1, int(remote.get("seat_limit") or 1))
        result["stripe_customer_id"] = str(remote.get("stripe_customer_id") or "")
        result["stripe_subscription_id"] = str(remote.get("stripe_subscription_id") or "")
        result["cancel_at_period_end"] = bool(remote.get("cancel_at_period_end", False))
        result["current_period_end"] = remote.get("current_period_end")

        # Keep legacy/local UI in sync with the authoritative shared billing row.
        try:
            write(lambda c: c.execute(
                """UPDATE companies
                   SET subscription_plan=?,
                       subscription_status=?,
                       ai_credit_limit=?,
                       seat_limit=?,
                       stripe_customer_id=?,
                       stripe_subscription_id=?
                   WHERE id=?""",
                (
                    result["subscription_plan"],
                    result["subscription_status"],
                    result["ai_credit_limit"],
                    result["seat_limit"],
                    result["stripe_customer_id"],
                    result["stripe_subscription_id"],
                    cid,
                )
            ))
        except Exception as e:
            print(f"V19 local billing sync failed for company {cid}: {e}")

    return result

def v18_refresh_credit_period(company_id):
    cid=int(company_id)
    data=v18_company_billing(cid)
    if not data:
        return None

    plan=str(data.get("subscription_plan") or "Trial")
    spec=SULLIVAN_PLANS.get(plan,SULLIVAN_PLANS["Trial"])
    start=pd.to_datetime(data.get("ai_period_start"),errors="coerce")
    this_month=date.today().replace(day=1)

    if pd.isna(start) or start.date()!=this_month:
        def f(c):
            c.execute("""UPDATE companies
                         SET ai_period_start=?,ai_credits_used=0,
                             ai_credit_limit=?,seat_limit=?
                         WHERE id=?""",
                      (this_month.isoformat(),int(spec["ai_credits"]),int(spec["seat_limit"]),cid))
        write(f)
    return v18_company_billing(cid)

def v18_active_company_id():
    c=current_company()
    if not c:
        return None
    cid=int(c.get("company_id",0) or 0)
    return cid if cid>0 else None

def v18_credit_status(company_id=None):
    cid=int(company_id or v18_active_company_id() or 0)
    if cid<=0:
        return {
            "company_id":None,"plan":"Personal","status":"No company",
            "limit":0,"used":0,"remaining":0,"seat_limit":1,"demo_used":1
        }
    d=v18_refresh_credit_period(cid)
    if not d:
        return None
    limit=int(d.get("ai_credit_limit") or 0)
    used=int(d.get("ai_credits_used") or 0)
    return {
        "company_id":cid,
        "plan":str(d.get("subscription_plan") or "Trial"),
        "status":str(d.get("subscription_status") or "Trial"),
        "limit":limit,
        "used":used,
        "remaining":max(0,limit-used),
        "seat_limit":max(1,int(d.get("seat_limit") or 1)),
        "demo_used":int(d.get("demo_used") or 0),
        "company_name":d.get("company_name"),
        "company_code":d.get("company_code"),
    }

def v18_log_ai_usage(company_id,action,credits,source="subscription",detail=""):
    user=current_user() or {}
    uid=user.get("id")
    stamp=datetime.now().isoformat(timespec="seconds")
    write(lambda c:c.execute(
        """INSERT INTO ai_usage(company_id,user_id,action,credits,source,detail,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (int(company_id),int(uid) if uid else None,str(action),int(credits),str(source),str(detail),stamp)
    ))

def v18_require_ai_credits(action,credits):
    cid=v18_active_company_id()
    if not cid:
        raise ValueError("Choose or create a company workspace before using Sullivan AI.")
    status=v18_credit_status(cid)
    if status["status"]!="Active" or status["plan"]=="Trial":
        raise ValueError(
            "This company is on the free preview. Use the one-time Sullivan AI demo "
            "or choose a paid plan when billing is connected."
        )
    if int(status["remaining"]) < int(credits):
        raise ValueError(
            f"Not enough Sullivan AI credits. This action needs {credits}; "
            f"{status['remaining']} remain this billing period."
        )
    return cid

def v18_consume_ai_credits(company_id,action,credits,detail=""):
    cid=int(company_id)
    credits=int(credits)
    def f(c):
        row=c.execute("""SELECT ai_credit_limit,ai_credits_used FROM companies WHERE id=?""",(cid,)).fetchone()
        if not row:
            raise ValueError("Company not found.")
        limit=int(row[0] or 0); used=int(row[1] or 0)
        if used+credits>limit:
            raise ValueError("Sullivan AI credit limit reached.")
        c.execute("UPDATE companies SET ai_credits_used=ai_credits_used+? WHERE id=?",(credits,cid))
    write(f)
    v18_log_ai_usage(cid,action,credits,"subscription",detail)

def v18_demo_available(company_id=None):
    status=v18_credit_status(company_id)
    return bool(status and status.get("company_id") and not status.get("demo_used"))

def v18_run_demo(description,amount,p):
    cid=v18_active_company_id()
    if not cid:
        raise ValueError("Create or open a company workspace to use the free AI demo.")
    if not v18_demo_available(cid):
        raise ValueError("This company has already used its free Sullivan AI demo.")
    if not key():
        raise ValueError("Sullivan AI is not configured on this server.")

    client=OpenAI(api_key=key())
    r=client.responses.parse(
        model=MODEL,
        input=[
            {"role":"system","content":
             "You are Sullivan AI. Demonstrate conservative bookkeeping classification. "
             "This is a preview only; do not claim the transaction was posted."},
            {"role":"user","content":
             f"Business {p['entity_type']} in {p['region']}, {p['country']}; "
             f"industry {p['industry']}. Demo transaction: {description}; amount {amount}. "
             f"Allowed categories: {', '.join(CATEGORIES)}."}
        ],
        text_format=Classification
    ).output_parsed

    cat=r.category if r.category in CATEGORIES else "Needs Review"
    result={
        "description":str(description),
        "amount":float(amount),
        "category":cat,
        "confidence":float(r.confidence),
        "explanation":r.explanation,
        "question":r.question_for_owner or "",
        "account":CATEGORY_TO_ACCOUNT.get(cat,"6999 Uncategorized Expense"),
    }

    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        row=c.execute("SELECT demo_used FROM companies WHERE id=?",(cid,)).fetchone()
        if not row or int(row[0] or 0):
            raise ValueError("This company has already used its free Sullivan AI demo.")
        c.execute("""INSERT INTO ai_demo_results(
                         company_id,description,amount,category,account,confidence,explanation,question,created_at
                     ) VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(company_id) DO UPDATE SET
                         description=excluded.description,
                         amount=excluded.amount,
                         category=excluded.category,
                         account=excluded.account,
                         confidence=excluded.confidence,
                         explanation=excluded.explanation,
                         question=excluded.question,
                         created_at=excluded.created_at""",
                  (cid,str(description),float(amount),cat,result["account"],float(r.confidence),
                   r.explanation,r.question_for_owner or "",stamp))
        c.execute("UPDATE companies SET demo_used=1 WHERE id=?",(cid,))
    write(f)
    v18_log_ai_usage(cid,"free_transaction_demo",0,"free_demo",f"{description} | {amount}")
    return result

def v18_demo_result(company_id=None):
    cid=int(company_id or v18_active_company_id() or 0)
    if cid<=0:
        return None
    d=read("""SELECT description,amount,category,account,confidence,explanation,question,created_at
              FROM ai_demo_results WHERE company_id=?""",(cid,))
    return None if d.empty else d.iloc[0].to_dict()


def v18_enterprise_quote(seats, expected_ai_usage="Standard"):
    """
    Produce a non-binding enterprise estimate for 51+ people.
    This intentionally does not activate a plan or create a legal commitment.
    """
    seats=int(seats)
    if seats < 51:
        raise ValueError("Enterprise quotes start at 51 people.")

    usage=str(expected_ai_usage or "Standard")
    # Internal estimate model for testing before Stripe/contracts are connected.
    base=250.0
    extra_seats=seats-50
    per_seat=4.50

    usage_multiplier={
        "Light": 0.90,
        "Standard": 1.00,
        "Heavy": 1.20,
        "Very heavy": 1.45,
    }.get(usage,1.00)

    estimate=round((base + extra_seats*per_seat)*usage_multiplier,2)

    if seats >= 500:
        estimate=round(estimate*0.90,2)
    elif seats >= 250:
        estimate=round(estimate*0.94,2)
    elif seats >= 100:
        estimate=round(estimate*0.97,2)

    return {
        "seats":seats,
        "usage":usage,
        "estimate":estimate,
        "summary":(
            f"Estimated Sullivan Enterprise price for {seats} people with "
            f"{usage.lower()} AI usage: ${estimate:,.2f}/month. "
            "This is a preliminary estimate, not a binding contract."
        )
    }

def v18_save_enterprise_quote(company_id,seats,expected_ai_usage,estimate,summary):
    user=current_user() or {}
    uid=user.get("id")
    stamp=datetime.now().isoformat(timespec="seconds")
    def f(c):
        c.execute("""INSERT INTO enterprise_quotes(
                         company_id,requested_by_user_id,requested_seats,expected_ai_usage,
                         estimated_monthly_price,quote_summary,status,created_at
                     ) VALUES(?,?,?,?,?,?,'Estimate',?)""",
                  (int(company_id),int(uid) if uid else None,int(seats),
                   str(expected_ai_usage),float(estimate),str(summary),stamp))
    write(f)


# ==========================
# Sullivan V19 Stripe Sandbox
# ==========================

SULLIVAN_PUBLIC_URL = "https://sullivan-accounting.streamlit.app"

def _secret_value(name, default=""):
    """
    Read deployment secrets without exposing them in the UI.
    Supports Streamlit Secrets first, then normal environment variables.
    """
    try:
        value = st.secrets.get(name, default)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return str(os.getenv(name, default) or "").strip()


def supabase_url():
    return _secret_value("SUPABASE_URL")


def supabase_secret_key():
    return _secret_value("SUPABASE_SECRET_KEY")


def supabase_ready():
    return bool(supabase_url() and supabase_secret_key())


def supabase_headers():
    secret = supabase_secret_key()
    return {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }


def v19_supabase_subscription(company_id):
    """
    Read the authoritative Sullivan billing record from Supabase.

    IMPORTANT:
    On failure this returns a safe diagnostic dictionary instead of None.
    That lets Plan & AI show the exact reason without exposing either secret.
    """
    cid = int(company_id)

    if not supabase_ready():
        missing = []
        if not supabase_url():
            missing.append("SUPABASE_URL")
        if not supabase_secret_key():
            missing.append("SUPABASE_SECRET_KEY")

        message = "Missing Streamlit secret(s): " + ", ".join(missing)
        print("Supabase billing disabled | " + message)
        return {
            "_diagnostic": "missing_secrets",
            "_message": message,
        }

    url = (
        f"{supabase_url().rstrip('/')}"
        "/rest/v1/sullivan_subscriptions"
        f"?company_id=eq.{cid}&select=*&limit=1"
    )

    try:
        response = requests.get(
            url,
            headers=supabase_headers(),
            timeout=10,
        )

        if response.status_code != 200:
            # Never print or display the request headers/key.
            body = (response.text or "").strip()
            if len(body) > 300:
                body = body[:300] + "..."

            message = f"Supabase returned HTTP {response.status_code}"
            if body:
                message += f": {body}"

            print(f"Supabase lookup failed | company={cid} | {message}")
            return {
                "_diagnostic": "http_error",
                "_message": message,
            }

        rows = response.json()

        if not rows:
            message = (
                f"Connected to Supabase, but no subscription row was found "
                f"for company_id={cid}."
            )
            print(message)
            return {
                "_diagnostic": "no_row",
                "_message": message,
            }

        row = dict(rows[0])
        row["_diagnostic"] = "connected"
        row["_message"] = "Subscription row found."
        print(
            f"Supabase subscription loaded | company={cid} | "
            f"plan={row.get('plan')} | status={row.get('subscription_status')}"
        )
        return row

    except requests.exceptions.Timeout:
        message = "Supabase connection timed out."
        print(f"Supabase lookup failed | company={cid} | {message}")
        return {
            "_diagnostic": "timeout",
            "_message": message,
        }

    except requests.exceptions.RequestException as e:
        message = f"Supabase network error: {type(e).__name__}"
        print(f"Supabase lookup failed | company={cid} | {message}: {e}")
        return {
            "_diagnostic": "network_error",
            "_message": message,
        }

    except Exception as e:
        message = f"Supabase lookup error: {type(e).__name__}: {e}"
        print(f"Supabase lookup failed | company={cid} | {message}")
        return {
            "_diagnostic": "exception",
            "_message": message,
        }


def v19_billing_diagnostic(company_id):
    """Return safe billing diagnostics without exposing secrets."""
    remote = v19_supabase_subscription(company_id) or {}
    state = str(remote.get("_diagnostic") or "unknown")

    return {
        "company_id": int(company_id),
        "supabase_configured": supabase_ready(),
        "state": state,
        "message": str(remote.get("_message") or ""),
        "remote_plan": remote.get("plan") if state == "connected" else None,
        "remote_status": remote.get("subscription_status") if state == "connected" else None,
        "remote_ai_credits": remote.get("ai_credits") if state == "connected" else None,
        "remote_seat_limit": remote.get("seat_limit") if state == "connected" else None,
    }

def stripe_secret_key():
    return _secret_value("STRIPE_SECRET_KEY")

def stripe_price_id(plan_name):
    mapping = {
        "Starter": "STRIPE_PRICE_STARTER",
        "Business": "STRIPE_PRICE_BUSINESS",
        "Pro": "STRIPE_PRICE_PRO",
        "Accounting Firm": "STRIPE_PRICE_ACCOUNTING_FIRM",
    }
    secret_name = mapping.get(plan_name)
    return _secret_value(secret_name) if secret_name else ""

def stripe_checkout_ready(plan_name=None):
    if not stripe_secret_key():
        return False
    if plan_name:
        return bool(stripe_price_id(plan_name))
    return all(stripe_price_id(x) for x in ("Starter","Business","Pro","Accounting Firm"))

def v183_create_checkout_session(company_id, plan_name):
    """
    Create a Stripe Checkout subscription session for the signed-in company.
    The company/plan identity is placed in Stripe metadata and verified again
    after Stripe redirects back to Sullivan.
    """
    if plan_name not in ("Starter","Business","Pro","Accounting Firm"):
        raise ValueError("That Sullivan plan is not available through standard checkout.")

    user = current_user()
    company = current_company()
    if not user:
        raise ValueError("Sign in before starting checkout.")
    if not company or int(company.get("company_id",0) or 0) != int(company_id):
        raise ValueError("Open the company workspace you want to subscribe before checkout.")

    secret = stripe_secret_key()
    price_id = stripe_price_id(plan_name)
    if not secret:
        raise ValueError("Stripe is not configured on this Sullivan deployment.")
    if not price_id:
        raise ValueError(f"Stripe Price ID for {plan_name} is missing.")

    stripe.api_key = secret

    success_url = (
        SULLIVAN_PUBLIC_URL
        + "/?checkout=success&session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = SULLIVAN_PUBLIC_URL + "/?checkout=cancelled"

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=str(user.get("email") or ""),
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        billing_address_collection="auto",
        client_reference_id=str(company_id),
        metadata={
            "sullivan_company_id": str(company_id),
            "sullivan_company_code": str(company.get("company_code") or ""),
            "sullivan_plan": plan_name,
            "sullivan_user_id": str(user.get("id") or ""),
        },
        subscription_data={
            "metadata": {
                "sullivan_company_id": str(company_id),
                "sullivan_plan": plan_name,
            }
        },
    )

    if not getattr(session, "url", None):
        raise RuntimeError("Stripe created the session but did not return a Checkout URL.")
    return session.url

def v183_activate_paid_plan(company_id, plan_name, stripe_customer_id="", stripe_subscription_id=""):
    if plan_name not in SULLIVAN_PLANS or plan_name in ("Trial","Enterprise"):
        raise ValueError("Invalid paid Sullivan plan.")

    spec = SULLIVAN_PLANS[plan_name]
    period_start = date.today().replace(day=1).isoformat()

    def f(c):
        c.execute(
            """UPDATE companies
               SET subscription_plan=?,
                   subscription_status='Active',
                   ai_credit_limit=?,
                   ai_credits_used=0,
                   ai_period_start=?,
                   seat_limit=?,
                   stripe_customer_id=?,
                   stripe_subscription_id=?
               WHERE id=?""",
            (
                plan_name,
                int(spec["ai_credits"]),
                period_start,
                int(spec["seat_limit"]),
                str(stripe_customer_id or ""),
                str(stripe_subscription_id or ""),
                int(company_id),
            )
        )
    write(f)

def v183_verify_checkout_return(session_id):
    """
    Retrieve the Checkout Session directly from Stripe.
    Never trust plan/company values supplied only by the browser query string.
    """
    if not session_id:
        raise ValueError("Stripe did not provide a Checkout Session ID.")

    secret = stripe_secret_key()
    if not secret:
        raise ValueError("Stripe is not configured.")

    user = current_user()
    company = current_company()
    if not user or not company:
        raise ValueError("Sign in and open the company workspace used for checkout.")

    stripe.api_key = secret
    session = stripe.checkout.Session.retrieve(
        session_id,
        expand=["subscription"],
    )

    metadata = dict(getattr(session, "metadata", {}) or {})
    expected_company_id = int(company.get("company_id",0) or 0)
    returned_company_id = int(metadata.get("sullivan_company_id") or 0)
    plan_name = str(metadata.get("sullivan_plan") or "")

    if returned_company_id != expected_company_id:
        raise ValueError("This Stripe checkout belongs to a different Sullivan company.")

    if plan_name not in ("Starter","Business","Pro","Accounting Firm"):
        raise ValueError("Stripe returned an unknown Sullivan plan.")

    # Stripe Checkout for subscriptions normally returns complete + paid/no_payment_required.
    checkout_status = str(getattr(session, "status", "") or "")
    payment_status = str(getattr(session, "payment_status", "") or "")
    if checkout_status != "complete":
        raise ValueError("Stripe Checkout is not complete yet.")
    if payment_status not in ("paid","no_payment_required"):
        raise ValueError(f"Stripe has not confirmed payment yet ({payment_status or 'unknown'}).")

    customer_id = str(getattr(session, "customer", "") or "")
    subscription_obj = getattr(session, "subscription", None)
    subscription_id = ""
    subscription_status = ""

    if isinstance(subscription_obj, str):
        subscription_id = subscription_obj
        sub = stripe.Subscription.retrieve(subscription_id)
        subscription_status = str(getattr(sub, "status", "") or "")
    elif subscription_obj is not None:
        subscription_id = str(getattr(subscription_obj, "id", "") or "")
        subscription_status = str(getattr(subscription_obj, "status", "") or "")

    if not subscription_id:
        raise ValueError("Stripe did not return a subscription for this checkout.")

    if subscription_status and subscription_status not in ("active","trialing"):
        raise ValueError(f"Stripe subscription status is {subscription_status}, so Sullivan did not activate it.")

    v183_activate_paid_plan(
        expected_company_id,
        plan_name,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
    )

    return {
        "plan": plan_name,
        "company_id": expected_company_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "payment_status": payment_status,
        "subscription_status": subscription_status or "active",
    }

def current_user():
    return st.session_state.get("auth_user")

def current_company():
    return st.session_state.get("auth_company")

def logout_v17():
    for k in [
        "auth_user","auth_company","auth_role","show_join_company","show_create_company",
        "v1722_workspace_select","v19_workspace_pending_label",
        "v19_workspace_pending_company_id","v19_workspace_pending_role",
        "v19_workspace_select_reset","v19_workspace_switched_to"
    ]:
        st.session_state.pop(k,None)

def require_company_role(*roles):
    role=st.session_state.get("auth_role")
    return role in roles


init_db()
v17_init_auth_tables()
st.markdown("<div class=\"v15-topbrand\">Sullivan <span>Business Command Center · V19.6</span></div>",unsafe_allow_html=True)



# ==========================
# V19 guest-first access
# ==========================
if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None
if "auth_company" not in st.session_state:
    st.session_state["auth_company"] = None
if "auth_role" not in st.session_state:
    st.session_state["auth_role"] = "Guest"
if "v171_auth_open" not in st.session_state:
    st.session_state["v171_auth_open"] = False
if "v171_auth_reason" not in st.session_state:
    st.session_state["v171_auth_reason"] = ""
if "v1722_workspace_open" not in st.session_state:
    st.session_state["v1722_workspace_open"] = False
if "v19_workspace_pending_label" not in st.session_state:
    st.session_state["v19_workspace_pending_label"] = None
if "v19_workspace_pending_company_id" not in st.session_state:
    st.session_state["v19_workspace_pending_company_id"] = None
if "v19_workspace_pending_role" not in st.session_state:
    st.session_state["v19_workspace_pending_role"] = None
if "v19_workspace_select_reset" not in st.session_state:
    st.session_state["v19_workspace_select_reset"] = None


# V18.3: if Streamlit has a valid Google OIDC identity, turn it into a
# Sullivan account automatically after Google redirects back to the app.
if _streamlit_oidc_logged_in():
    try:
        google_user = sync_google_user()
        if google_user:
            st.session_state["auth_user"] = google_user
            load_default_workspace_for_user(google_user)
            st.session_state["v171_auth_open"] = False
            st.session_state["v171_auth_reason"] = ""
    except Exception as e:
        st.error(f"Google sign-in reached Sullivan, but the account could not be loaded: {e}")

# V19.3: after authentication/workspace restoration, make sure this workspace
# owns a separate set of accounting tables before any business screens render.
if st.session_state.get("auth_user") and st.session_state.get("auth_role") != "Guest":
    try:
        ensure_current_workspace_books()
    except Exception as e:
        st.error(f"Sullivan could not initialize this workspace's separate books: {e}")

def v171_is_signed_in():
    return bool(st.session_state.get("auth_user"))

def v171_open_auth(reason="Sign in to continue."):
    st.session_state["v171_auth_open"] = True
    st.session_state["v171_auth_reason"] = reason

def v171_close_auth():
    st.session_state["v171_auth_open"] = False
    st.session_state["v171_auth_reason"] = ""

# Protect the common write/action buttons centrally so the rest of Sullivan
# can remain browseable in Guest mode.
_v171_real_button = st.button
_v171_protected_words = (
    "save","create","record","post","approve","import","upload","add ","enter a bill",
    "send invoice","match bank","reconcile","finish","complete","lock","reopen",
    "reverse","convert","generate","apply credit","pay ","payment","delete","archive"
)
_v171_allow_guest_buttons = (
    "sign in","create account","continue with google","continue with apple",
    "continue with email","browse as guest","close","switch workspace","sign out",
    "see reports","view","open"
)

def _v171_button(label, *args, **kwargs):
    lbl = str(label or "").strip().lower()
    protected = any(w in lbl for w in _v171_protected_words)
    allowed = any(w in lbl for w in _v171_allow_guest_buttons)
    if (not v171_is_signed_in()) and protected and not allowed:
        clicked = _v171_real_button(label, *args, **kwargs)
        if clicked:
            v171_open_auth(f"Please sign in before you {str(label).lower()}.")
            st.rerun()
        return False
    return _v171_real_button(label, *args, **kwargs)

st.button = _v171_button

# Make upload controls clearly read-only for guests.
_v171_real_file_uploader = st.file_uploader
def _v171_file_uploader(label, *args, **kwargs):
    if not v171_is_signed_in():
        kwargs["disabled"] = True
        result = _v171_real_file_uploader(label, *args, **kwargs)
        st.caption("🔒 Sign in to upload/import files.")
        return result
    return _v171_real_file_uploader(label, *args, **kwargs)
st.file_uploader = _v171_file_uploader

p0=profile()

# V19: Handle Stripe Checkout redirect safely.
try:
    checkout_state = st.query_params.get("checkout", "")
except Exception:
    checkout_state = ""

if checkout_state == "success":
    session_id = st.query_params.get("session_id", "")
    if v171_is_signed_in() and current_company():
        if st.session_state.get("v183_verified_session") != session_id:
            try:
                verified = v183_verify_checkout_return(session_id)
                st.session_state["v183_verified_session"] = session_id
                st.session_state["v183_checkout_result"] = verified
                # Refresh the session's company plan display.
                refreshed = v18_company_billing(verified["company_id"])
                if refreshed and st.session_state.get("auth_company"):
                    st.session_state["auth_company"]["subscription_plan"] = refreshed.get("subscription_plan")
                st.query_params.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Stripe returned to Sullivan, but the subscription could not be activated: {e}")
    else:
        v171_open_auth("Your Stripe checkout succeeded. Sign in to the same Sullivan account to finish activating the plan.")

elif checkout_state == "cancelled":
    st.warning("Stripe checkout was cancelled. Nothing was charged and your Sullivan plan was not changed.")
    try:
        st.query_params.clear()
    except Exception:
        pass

if st.session_state.get("v183_checkout_result"):
    result = st.session_state.pop("v183_checkout_result")
    st.success(
        f"✅ Stripe confirmed your **{result['plan']}** subscription. "
        "Sullivan activated the plan and reset its monthly AI allowance."
    )


# Guest sign-in panel opens only when needed.
if st.session_state.get("v171_auth_open"):
    st.markdown('<div class="auth-overlay-card">',unsafe_allow_html=True)
    ctitle,cclose=st.columns([8,1])
    ctitle.markdown("## Sign in to Sullivan")
    if cclose.button("Close",key="v171_close_auth_btn",use_container_width=True):
        v171_close_auth()
        st.rerun()

    reason=st.session_state.get("v171_auth_reason") or "Sign in to continue."
    st.info(reason)

    st.markdown("### Choose how you want to sign in")
    gcol,acol,ecol=st.columns(3)
    if gcol.button("🔵 Continue with Google",use_container_width=True,key="v171_google"):
        try:
            st.login()
        except Exception as e:
            st.error(
                "Google sign-in is not fully configured yet. "
                "Check Streamlit Secrets and the Google redirect URI. "
                f"Details: {e}"
            )
    if acol.button(" Continue with Apple",use_container_width=True,key="v171_apple"):
        st.info("Apple sign-in is ready to connect when Apple OAuth credentials are added to Sullivan's deployment.")
    email_mode=ecol.button("✉ Continue with Email",use_container_width=True,key="v171_email_mode")
    if email_mode:
        st.session_state["v171_email_auth"]=True

    if st.session_state.get("v171_email_auth"):
        signin_tab,create_tab=st.tabs(["Email sign in","Create account"])
        with signin_tab:
            lemail=st.text_input("Email",key="v171_login_email")
            lpw=st.text_input("Password",type="password",key="v171_login_password")
            if st.button("Sign in",type="primary",use_container_width=True,key="v171_email_signin"):
                u=authenticate_user(lemail,lpw)
                if not u:
                    st.error("Email or password is incorrect.")
                else:
                    st.session_state["auth_user"]=u
                    load_default_workspace_for_user(u)
                    v171_close_auth()
                    st.success("Signed in.")
                    st.rerun()

        with create_tab:
            sname=st.text_input("Full name",key="v171_signup_name")
            semail=st.text_input("Email",key="v171_signup_email")
            spw=st.text_input("Password",type="password",key="v171_signup_password")
            spw2=st.text_input("Confirm password",type="password",key="v171_signup_password2")
            if st.button("Create personal account",type="primary",use_container_width=True,key="v171_create_personal"):
                try:
                    if spw!=spw2: raise ValueError("Passwords do not match.")
                    uid,user_code=create_app_user(sname,semail,spw,True)
                    u=authenticate_user(semail,spw)
                    st.session_state["auth_user"]=u
                    st.session_state["auth_role"]="Personal"
                    v171_close_auth()
                    st.success(f"Account created. Sullivan User ID: {user_code}")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    st.markdown("---")
    st.markdown("### Are you a company employee?")
    st.caption("Sign in first, then connect to your employer. Your employer gives you a one-time invite code; you cannot join a company just by typing its name.")
    if v171_is_signed_in():
        emp_code=st.text_input("Employer invite code",placeholder="JOIN-XXXXXXXXXX",key="v171_employee_code")
        if st.button("Join employer",type="primary",use_container_width=True,key="v171_join_employer"):
            try:
                u=current_user()
                joined=join_company_with_invite(u["id"],u["email"],emp_code)
                memberships=company_memberships(u["id"])
                match=memberships[memberships.company_name==joined["company_name"]]
                if not match.empty:
                    r=match.iloc[0]
                    activate_workspace(u, r, persist=True)
                    st.session_state["v19_workspace_select_reset"] = f'{r.company_name} · {r.role} · {r.company_code}'
                v171_close_auth()
                st.success(f"Connected to {joined['company_name']}.")
                st.rerun()
            except Exception as e:
                st.error(str(e))
    else:
        st.caption("Use Google, Apple, or Email above first. Then the employee join box will appear here.")

    st.markdown('</div>',unsafe_allow_html=True)
with st.sidebar:
    st.markdown(
        """<div class="s15-brand">
            <div class="s15-logo">◆</div>
            <div>
                <div class="s15-brand-name">Sullivan</div>
                <div class="s15-brand-sub">Your business, simplified.</div>
            </div>
        </div>""",
        unsafe_allow_html=True
    )

    st.markdown('<div class="s15-divider"></div>',unsafe_allow_html=True)
    auth_u=current_user()
    auth_c=current_company()
    if auth_u:
        provider_label = "Google" if auth_u.get("auth_provider")=="google" else "Email"
        st.markdown(
            f'<div class="account-strip"><div><b>{auth_u["full_name"]}</b>'
            f'<span>{st.session_state.get("auth_role","")} · {provider_label}</span></div>'
            f'<div><span>{auth_c["company_name"] if auth_c else "Personal"}</span><br>'
            f'<span>{auth_c["company_code"] if auth_c else auth_u["user_code"]}</span></div></div>',
            unsafe_allow_html=True
        )
        if st.button("Manage workspace",use_container_width=True,key="v171_switch_workspace"):
            st.session_state["v1722_workspace_open"] = not st.session_state.get("v1722_workspace_open", False)
            st.rerun()

        if st.session_state.get("v1722_workspace_open"):
            st.markdown('<div class="workspace-card">', unsafe_allow_html=True)
            st.markdown("#### Workspace")

            memberships = company_memberships(auth_u["id"])
            options = ["Personal"]
            company_by_label = {}
            if not memberships.empty:
                for _, mr in memberships.iterrows():
                    label = f'{mr.company_name} · {mr.role} · {mr.company_code}'
                    options.append(label)
                    company_by_label[label] = mr

            current_label = "Personal"
            if auth_c:
                for label, mr in company_by_label.items():
                    if int(mr.company_id) == int(auth_c.get("company_id", -1)):
                        current_label = label
                        break

            # The selector chooses a DESTINATION only. It does not switch workspaces
            # until the user explicitly confirms below.
            #
            # Streamlit does not allow changing a widget's session-state key after
            # that widget has already been instantiated in the same run. Therefore,
            # confirmation buttons only stage a reset target; it is applied HERE,
            # before the selectbox is created on the next rerun.
            reset_target = st.session_state.pop("v19_workspace_select_reset", None)
            if reset_target in options:
                st.session_state["v1722_workspace_select"] = reset_target
            elif "v1722_workspace_select" not in st.session_state:
                st.session_state["v1722_workspace_select"] = current_label
            elif st.session_state["v1722_workspace_select"] not in options:
                st.session_state["v1722_workspace_select"] = current_label

            selected = st.selectbox(
                "Work in",
                options,
                key="v1722_workspace_select"
            )

            # If the destination changed, stage it for confirmation.
            if selected != current_label:
                if st.session_state.get("v19_workspace_pending_label") != selected:
                    st.session_state["v19_workspace_pending_label"] = selected
                    if selected == "Personal":
                        st.session_state["v19_workspace_pending_company_id"] = None
                        st.session_state["v19_workspace_pending_role"] = "Personal"
                    else:
                        pending_row = company_by_label[selected]
                        st.session_state["v19_workspace_pending_company_id"] = int(pending_row.company_id)
                        st.session_state["v19_workspace_pending_role"] = str(pending_row.role)

            pending_label = st.session_state.get("v19_workspace_pending_label")
            if pending_label and pending_label != current_label:
                current_name = current_label
                destination_name = pending_label

                st.warning(
                    f"**Switch workspace?**\n\n"
                    f"You are currently working in **{current_name}**. "
                    f"You are about to switch to **{destination_name}**.\n\n"
                    "Transactions, reports, AI credits, billing, team access, and other "
                    "workspace data will change to the selected workspace."
                )

                stay_col, switch_col = st.columns(2)

                if stay_col.button(
                    f"Stay in {current_name}",
                    use_container_width=True,
                    key="v19_workspace_stay"
                ):
                    st.session_state["v19_workspace_pending_label"] = None
                    st.session_state["v19_workspace_pending_company_id"] = None
                    st.session_state["v19_workspace_pending_role"] = None
                    st.session_state["v19_workspace_select_reset"] = current_label
                    st.rerun()

                if switch_col.button(
                    f"Switch to {destination_name}",
                    type="primary",
                    use_container_width=True,
                    key="v19_workspace_confirm"
                ):
                    if pending_label == "Personal":
                        activate_workspace(auth_u, None, persist=True)
                    else:
                        target_id = int(st.session_state["v19_workspace_pending_company_id"])
                        target_match = memberships[memberships.company_id == target_id]
                        if target_match.empty:
                            st.session_state["v19_workspace_pending_label"] = None
                            st.session_state["v19_workspace_select_reset"] = current_label
                            st.error("That company workspace is no longer available to this account.")
                            st.rerun()
                        activate_workspace(auth_u, target_match.iloc[0], persist=True)

                    st.session_state["v19_workspace_pending_label"] = None
                    st.session_state["v19_workspace_pending_company_id"] = None
                    st.session_state["v19_workspace_pending_role"] = None
                    st.session_state["v19_workspace_select_reset"] = destination_name
                    st.session_state["v19_workspace_switched_to"] = destination_name
                    st.rerun()

            if st.session_state.get("v19_workspace_switched_to"):
                switched_to = st.session_state.pop("v19_workspace_switched_to")
                st.success(f"Switched to **{switched_to}**.")

            with st.expander("Join an employer"):
                join_code = st.text_input(
                    "Employer invite code",
                    placeholder="JOIN-XXXXXXXXXX",
                    key="v1722_sidebar_join_code"
                )
                if st.button("Join company", use_container_width=True, key="v1722_sidebar_join_btn"):
                    try:
                        joined = join_company_with_invite(auth_u["id"], auth_u["email"], join_code)
                        memberships = company_memberships(auth_u["id"])
                        match = memberships[memberships.company_name == joined["company_name"]]
                        if not match.empty:
                            mr = match.iloc[0]
                            activate_workspace(auth_u, mr, persist=True)
                            st.session_state["v19_workspace_select_reset"] = f'{mr.company_name} · {mr.role} · {mr.company_code}'
                        st.success(f"Joined {joined['company_name']}.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

            with st.expander("Create a company"):
                new_company_name = st.text_input(
                    "Company name",
                    key="v1722_company_name"
                )
                if st.button("Create company workspace", use_container_width=True, key="v1722_create_company_btn"):
                    try:
                        cid, code = create_company_for_user(auth_u["id"], new_company_name)
                        memberships = company_memberships(auth_u["id"])
                        match = memberships[memberships.company_id == cid]
                        if not match.empty:
                            mr = match.iloc[0]
                            activate_workspace(auth_u, mr, persist=True)
                            st.session_state["v19_workspace_select_reset"] = f'{mr.company_name} · {mr.role} · {mr.company_code}'
                        st.success(f"Company created. Company ID: {code}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

            # V19.3 company deletion. Only the active company's Owner sees it.
            active_company = current_company()
            active_role = st.session_state.get("auth_role")
            if active_company and active_role == "Owner":
                with st.expander("Delete company"):
                    delete_name = str(active_company.get("company_name") or "")
                    st.warning(
                        "Deleting a company permanently removes that company's Sullivan "
                        "workspace and bookkeeping data. This cannot be undone."
                    )
                    st.caption(
                        "Paid/Stripe-backed companies must have billing cancelled before deletion."
                    )
                    typed_delete_name = st.text_input(
                        f'Type "{delete_name}" to confirm',
                        key="v19_delete_company_confirm_name"
                    )
                    if st.button(
                        "Delete this company permanently",
                        use_container_width=True,
                        key="v19_delete_company_btn"
                    ):
                        try:
                            deleted_name = delete_company_workspace(
                                auth_u["id"],
                                int(active_company["company_id"]),
                                typed_delete_name,
                            )
                            activate_workspace(auth_u, None, persist=True)
                            st.session_state["v19_workspace_select_reset"] = "Personal"
                            st.session_state["v19_workspace_pending_label"] = None
                            st.session_state["v19_workspace_pending_company_id"] = None
                            st.session_state["v19_workspace_pending_role"] = None
                            st.session_state["v19_company_deleted"] = deleted_name
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

            if st.session_state.get("v19_company_deleted"):
                deleted_name = st.session_state.pop("v19_company_deleted")
                st.success(f'Deleted company workspace **{deleted_name}**.')

            st.caption(
                "Personal and company workspaces have completely separate books, transactions, "
                "reports, customers, vendors, taxes, documents, and accounting history."
            )
            st.markdown('</div>', unsafe_allow_html=True)
        if st.button("Sign out",use_container_width=True,key="v171_sidebar_signout"):
            provider = (auth_u or {}).get("auth_provider","email")
            logout_v17()
            st.session_state["auth_user"]=None
            st.session_state["auth_company"]=None
            st.session_state["auth_role"]="Guest"
            if provider == "google" and _streamlit_oidc_logged_in():
                st.logout()
            st.rerun()
    else:
        st.markdown(
            '<div class="guest-card"><b>Browsing as Guest</b><span>Explore Sullivan freely. Sign in only when you want to save or change something.</span></div>',
            unsafe_allow_html=True
        )
        if st.button("Sign in / Create account",use_container_width=True,key="v171_sidebar_signin"):
            v171_open_auth("Sign in to save your work and use Sullivan.")
            st.rerun()


    with st.expander("⚙️  Business settings", expanded=True):
        bn=st.text_input("Business name",p0["business_name"])
        country=st.selectbox("Country",["Canada","United States","Other"],index=0)
        region=st.text_input("Province / State",p0["region"])
        entity=st.selectbox("Entity type",["Corporation","Sole proprietorship","Partnership","LLC","Other"])
        industry=st.text_input("Industry",p0["industry"])
        fy=st.text_input("Fiscal year end",p0["fiscal_year_end"])
        gstreg=st.checkbox("GST/HST registered",p0["gst_registered"])
        qstreg=st.checkbox("QST registered",p0["qst_registered"])

        if st.button("💾  Save business settings",use_container_width=True,key="save_business_v153"):
            save_profile({
                "business_name":bn,
                "country":country,
                "region":region,
                "entity_type":entity,
                "industry":industry,
                "fiscal_year_end":fy,
                "gst_registered":gstreg,
                "qst_registered":qstreg
            })
            st.success("✓ Business settings saved.")

    st.markdown('<div class="s15-small-gap"></div>',unsafe_allow_html=True)

    with st.expander("✨  Sullivan AI", expanded=True):
        st.caption("Sullivan AI is managed securely by Sullivan. Employees never need an OpenAI API key.")
        if key():
            active_c=current_company()
            if active_c and int(active_c.get("company_id",0) or 0)>0:
                cs=v18_credit_status(int(active_c["company_id"]))
                if cs["plan"]=="Trial" and not cs["demo_used"]:
                    msg="● Sullivan AI is ready · 1 free demo available"
                elif cs["plan"]=="Trial":
                    msg="● Sullivan AI ready · choose a plan for more AI"
                else:
                    msg=f'● Sullivan AI · {cs["remaining"]:,} credits remaining'
                st.markdown(f'<div class="s15-ai-ok">{msg}</div>',unsafe_allow_html=True)
            else:
                st.markdown('<div class="s15-ai-ok">● Sullivan AI is available.</div>',unsafe_allow_html=True)
        else:
            st.markdown('<div class="s15-ai-off">● Sullivan AI is not configured on this server yet.</div>',unsafe_allow_html=True)

    st.markdown(
        """<div class="s15-system-card">
            <div class="s15-system-title">● Sullivan is running normally</div>
            <div class="s15-system-sub">All accounting systems operational</div>
        </div>""",
        unsafe_allow_html=True
    )

p={"business_name":bn,"country":country,"region":region,"entity_type":entity,"industry":industry,"fiscal_year_end":fy,"gst_registered":gstreg,"qst_registered":qstreg}


st.markdown("""
<style>
:root {
  --navy:#061A30; --navy2:#082744; --blue:#1769E0; --ink:#102943;
  --muted:#647A90; --line:#DFE7F0; --bg:#F6F9FC;
}
html,body,[class*="css"]{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}
.stApp{background:var(--bg)!important;color:var(--ink)!important;}
.block-container{max-width:1500px!important;padding:1.05rem 1.65rem 1.6rem!important;}
.v15-topbrand{font-size:1.65rem;font-weight:900;color:#102943;margin:0 0 .65rem;}
.v15-topbrand span{font-size:.78rem;font-weight:600;color:#7A8C9E;margin-left:10px;}

/* Sidebar */
[data-testid="stSidebar"]{background:linear-gradient(180deg,#061A30 0%,#082744 100%)!important;border-right:none!important;}
[data-testid="stSidebar"]>div:first-child{background:transparent!important;}
[data-testid="stSidebar"] *{color:#EEF6FF!important;}
[data-testid="stSidebar"] input,[data-testid="stSidebar"] textarea{background:white!important;color:#17314D!important;}
[data-testid="stSidebar"] div[data-baseweb="select"]>div{background:white!important;color:#17314D!important;}
.side-brand{display:flex;gap:12px;align-items:center;padding:10px 4px 18px;}
.side-logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#12D5C5,#1769E0);display:flex;align-items:center;justify-content:center;color:white!important;font-size:20px;}
.side-brand b{display:block;font-size:1.45rem;color:white!important;}
.side-brand span{display:block;font-size:.83rem;color:#AFC3D8!important;margin-top:2px;}
.side-help{background:rgba(255,255,255,.065);border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:14px;margin:5px 0 18px;}
.side-help b{display:block;color:white!important;margin-bottom:4px;}
.side-help span{font-size:.82rem;color:#B9CCDE!important;line-height:1.4;}
.side-status{margin-top:20px;padding:12px;border-top:1px solid rgba(255,255,255,.09);font-size:.82rem;color:#8EE4C2!important;}
.side-status span{color:#9FB6CA!important;font-size:.75rem;}

/* Top navigation */
div[data-baseweb="tab-list"]{gap:7px!important;border-bottom:1px solid #DFE7F0!important;padding-bottom:7px!important;}
button[data-baseweb="tab"]{background:white!important;border:1px solid #E1E8F0!important;border-radius:11px!important;padding:10px 15px!important;font-weight:760!important;color:#4D647A!important;box-shadow:0 2px 7px rgba(17,43,67,.035)!important;}
button[data-baseweb="tab"][aria-selected="true"]{background:#1769E0!important;border-color:#1769E0!important;color:white!important;}
button[data-baseweb="tab"] p{color:inherit!important;}

/* Hero */
.hero-v15{min-height:184px;border-radius:20px;border:1px solid #DCE6EF;box-shadow:0 7px 24px rgba(17,44,70,.07);background:linear-gradient(90deg,rgba(248,251,254,.98) 0%,rgba(248,251,254,.88) 37%,rgba(248,251,254,.15) 72%),url("data:image/jpeg;base64,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") center/cover no-repeat;display:flex;justify-content:space-between;align-items:flex-start;gap:30px;padding:30px 30px;margin:14px 0 14px;overflow:hidden;}
.hero-copy{max-width:54%;}
.hero-kicker{font-size:.72rem;font-weight:850;letter-spacing:.12em;color:#1769E0;margin-bottom:8px;}
.hero-v15 h1{margin:0!important;font-size:2.1rem!important;color:#0C2541!important;line-height:1.08!important;}
.hero-v15 p{margin:10px 0 0!important;color:#36516B!important;font-size:1rem!important;}
.hero-meta{display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end;max-width:45%;}
.hero-chip{background:rgba(255,255,255,.94);border:1px solid #DCE5EE;border-radius:11px;padding:10px 13px;color:#17314E;font-size:.86rem;font-weight:760;box-shadow:0 4px 12px rgba(16,39,62,.06);white-space:nowrap;}

/* KPI cards */
.kpi-grid-v15{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:18px;}
.kpi-v15{background:white;border:1px solid #E0E8F0;border-radius:17px;padding:17px 16px 15px;min-height:137px;box-shadow:0 5px 17px rgba(18,43,67,.045);}
.kpi-head{display:flex;align-items:center;gap:8px;font-size:.82rem;font-weight:780;color:#18324E;min-height:32px;}
.kpi-icon{width:31px;height:31px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:900;flex:0 0 auto;}
.kpi-value{font-size:1.62rem;font-weight:900;line-height:1;margin-top:13px;letter-spacing:-.025em;}
.kpi-sub{font-size:.78rem;color:#6B7F92;margin-top:13px;}

/* Panels */
.section-heading{font-size:1.08rem;font-weight:880;color:#102B48;margin:4px 0 9px;}
.panel-v15{background:white;border:1px solid #E0E8F0;border-radius:17px;padding:8px 16px;box-shadow:0 5px 17px rgba(18,43,67,.04);min-height:118px;margin-bottom:13px;}
.list-row,.activity-v15{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #EDF2F6;}
.list-row:last-child,.activity-v15:last-child{border-bottom:none;}
.round-v15{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;flex:0 0 auto;}
.list-copy{flex:1;min-width:0;}
.list-copy b{display:block;color:#162F49;font-size:.89rem;}
.list-copy span{display:block;color:#6A7F92;font-size:.78rem;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.activity-amount{text-align:right;flex:0 0 auto;font-size:.84rem;}
.activity-amount span{display:block;color:#74889A!important;font-size:.73rem;margin-top:2px;}
.empty-state{display:flex;flex-direction:column;justify-content:center;min-height:130px;color:#18314B;}
.empty-state span{color:#73879A;font-size:.82rem;margin-top:4px;}

/* Actions and metrics */
div.stButton>button{border-radius:12px!important;min-height:48px!important;border:1px solid #DCE5EE!important;background:white!important;color:#18324E!important;font-weight:800!important;box-shadow:0 2px 7px rgba(17,42,66,.04)!important;}
div.stButton>button:hover{border-color:#1769E0!important;color:#1769E0!important;background:#F6FAFF!important;}
.quick-labels{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:-3px 0 9px;}
.quick-labels span{font-size:.69rem;color:#7A8B9C;text-align:center;}
[data-testid="stMetric"]{background:white!important;border:1px solid #E0E8F0!important;border-radius:14px!important;padding:11px 13px!important;box-shadow:none!important;}
[data-testid="stMetricValue"]{color:#18314B!important;font-size:1.35rem!important;}
[data-testid="stMetricLabel"]{color:#687E91!important;font-size:.76rem!important;}
[data-testid="stAlert"]{border-radius:12px!important;}

/* Footer */
.footer-v15{background:#061A30;color:#CAD9E8;border-radius:14px;margin-top:16px;padding:13px 16px;display:flex;align-items:center;justify-content:space-between;gap:20px;font-size:.78rem;}
.footer-v15 b{color:white;font-size:1.02rem;white-space:nowrap;}
.footer-v15 small{font-weight:600;color:#8FAAC1;margin-left:7px;}

@media(max-width:1250px){
 .kpi-grid-v15{grid-template-columns:repeat(3,1fr);}
 .hero-copy{max-width:60%;}
}
@media(max-width:850px){
 .kpi-grid-v15{grid-template-columns:repeat(2,1fr);}
 .hero-v15{flex-direction:column;}
 .hero-copy,.hero-meta{max-width:100%;}
 .hero-meta{justify-content:flex-start;}
 .footer-v15{flex-direction:column;align-items:flex-start;}
}


/* V18.3 — readable forms everywhere */
.main input,
.main textarea,
section.main input,
section.main textarea,
[data-testid="stAppViewContainer"] input,
[data-testid="stAppViewContainer"] textarea {
    background:#FFFFFF !important;
    color:#102943 !important;
    border-color:#D6E1EC !important;
    caret-color:#1769E0 !important;
}

[data-testid="stAppViewContainer"] input::placeholder,
[data-testid="stAppViewContainer"] textarea::placeholder {
    color:#96A6B6 !important;
    opacity:1 !important;
}

/* BaseWeb wrappers used by Streamlit inputs */
[data-testid="stAppViewContainer"] div[data-baseweb="input"] > div,
[data-testid="stAppViewContainer"] div[data-baseweb="textarea"] > div,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] > div {
    background:#FFFFFF !important;
    color:#102943 !important;
    border-color:#D6E1EC !important;
}

/* Select/dropdown visible text */
[data-testid="stAppViewContainer"] div[data-baseweb="select"] span,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] input,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] svg {
    color:#102943 !important;
    fill:#667A8E !important;
}

/* Number-input wrapper and buttons */
[data-testid="stAppViewContainer"] [data-testid="stNumberInput"] > div > div,
[data-testid="stAppViewContainer"] [data-testid="stNumberInput"] input {
    background:#FFFFFF !important;
    color:#102943 !important;
}
[data-testid="stAppViewContainer"] [data-testid="stNumberInput"] button {
    background:#F4F7FA !important;
    color:#36516B !important;
    border-color:#D6E1EC !important;
}

/* Date inputs */
[data-testid="stAppViewContainer"] [data-testid="stDateInput"] input {
    background:#FFFFFF !important;
    color:#102943 !important;
}

/* Focus state */
[data-testid="stAppViewContainer"] div[data-baseweb="input"] > div:focus-within,
[data-testid="stAppViewContainer"] div[data-baseweb="textarea"] > div:focus-within,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] > div:focus-within {
    border-color:#1769E0 !important;
    box-shadow:0 0 0 2px rgba(23,105,224,.12) !important;
}

/* Disabled/read-only fields should still be legible */
[data-testid="stAppViewContainer"] input:disabled,
[data-testid="stAppViewContainer"] textarea:disabled {
    background:#F0F4F8 !important;
    color:#5E7286 !important;
    opacity:1 !important;
}

/* Keep labels crisp */
[data-testid="stAppViewContainer"] label,
[data-testid="stAppViewContainer"] [data-testid="stWidgetLabel"] p {
    color:#17314D !important;
    font-weight:650 !important;
}

/* Checkbox text */
[data-testid="stAppViewContainer"] [data-testid="stCheckbox"] p {
    color:#17314D !important;
}



/* V18.3 — sidebar readability fix */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] label p,
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] .stMarkdown span {
    color: #FFFFFF !important;
}

/* Keep actual white input boxes readable with dark text */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea,
section[data-testid="stSidebar"] div[data-baseweb="select"] span {
    color: #17314D !important;
}



/* V18.3 polished Sullivan sidebar */
section[data-testid="stSidebar"] {
    background:
        linear-gradient(180deg,#061A30 0%,#07223D 55%,#082B4B 100%) !important;
}

section[data-testid="stSidebar"] > div:first-child {
    padding-top: 1rem !important;
}

.s15-brand {
    display:flex;
    align-items:center;
    gap:12px;
    padding:8px 2px 10px;
}
.s15-logo {
    width:42px;
    height:42px;
    border-radius:12px;
    display:flex;
    align-items:center;
    justify-content:center;
    background:linear-gradient(145deg,#17D0C1,#1877E8);
    color:white !important;
    font-size:20px;
    font-weight:900;
    box-shadow:0 6px 20px rgba(20,112,213,.28);
}
.s15-brand-name {
    color:#FFFFFF !important;
    font-size:1.45rem;
    line-height:1;
    font-weight:900;
}
.s15-brand-sub {
    color:#B8CADB !important;
    font-size:.82rem;
    margin-top:6px;
}
.s15-divider {
    height:1px;
    background:rgba(255,255,255,.10);
    margin:12px 0 16px;
}
.s15-small-gap {
    height:8px;
}

/* Expander shells */
section[data-testid="stSidebar"] details {
    background:rgba(255,255,255,.055) !important;
    border:1px solid rgba(255,255,255,.10) !important;
    border-radius:12px !important;
    overflow:hidden !important;
    margin-bottom:10px !important;
}
section[data-testid="stSidebar"] details > summary {
    background:rgba(255,255,255,.075) !important;
    color:#FFFFFF !important;
    min-height:44px !important;
}
section[data-testid="stSidebar"] details > summary p,
section[data-testid="stSidebar"] details > summary span,
section[data-testid="stSidebar"] details > summary svg {
    color:#FFFFFF !important;
    fill:#FFFFFF !important;
    font-weight:800 !important;
}
section[data-testid="stSidebar"] details[open] > summary {
    background:#8799AB !important;
}

/* Labels */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] label p,
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
    color:#FFFFFF !important;
    font-weight:700 !important;
}

/* Fields */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea,
section[data-testid="stSidebar"] div[data-baseweb="select"] > div {
    background:#FFFFFF !important;
    color:#17314D !important;
    border:1px solid #D7E2EC !important;
    border-radius:9px !important;
}
section[data-testid="stSidebar"] input::placeholder {
    color:#8DA0B2 !important;
}
section[data-testid="stSidebar"] div[data-baseweb="select"] span,
section[data-testid="stSidebar"] div[data-baseweb="select"] input {
    color:#17314D !important;
}
section[data-testid="stSidebar"] div[data-baseweb="select"] svg {
    fill:#61768A !important;
}

/* Sidebar primary-style buttons */
section[data-testid="stSidebar"] div.stButton > button {
    background:linear-gradient(180deg,#1773ED 0%,#0D62D8 100%) !important;
    color:#FFFFFF !important;
    border:1px solid #2B80F0 !important;
    border-radius:9px !important;
    font-weight:800 !important;
    min-height:44px !important;
    box-shadow:0 5px 15px rgba(10,87,190,.22) !important;
}
section[data-testid="stSidebar"] div.stButton > button p {
    color:#FFFFFF !important;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    background:linear-gradient(180deg,#2480F4 0%,#176BDD 100%) !important;
    color:#FFFFFF !important;
}
section[data-testid="stSidebar"] div.stButton > button:disabled {
    background:#35516C !important;
    border-color:#45627E !important;
    color:#AFC0D0 !important;
    opacity:1 !important;
}
section[data-testid="stSidebar"] div.stButton > button:disabled p {
    color:#AFC0D0 !important;
}

/* Checkboxes */
section[data-testid="stSidebar"] [data-testid="stCheckbox"] p {
    color:#FFFFFF !important;
    font-weight:700 !important;
}

/* Status cards */
.s15-ai-ok,
.s15-ai-off {
    border-radius:10px;
    padding:12px 13px;
    margin:6px 0 10px;
    font-size:.84rem;
    font-weight:750;
}
.s15-ai-ok {
    background:rgba(25,164,92,.20);
    border:1px solid rgba(49,202,118,.28);
    color:#8AF0B7 !important;
}
.s15-ai-off {
    background:rgba(23,105,224,.16);
    border:1px solid rgba(68,137,232,.22);
    color:#D4E7FF !important;
}
.s15-system-card {
    margin-top:22px;
    padding:15px 12px;
    border-top:1px solid rgba(255,255,255,.10);
}
.s15-system-title {
    color:#5EE6AD !important;
    font-size:.82rem;
    font-weight:800;
}
.s15-system-sub {
    color:#A8BDCF !important;
    font-size:.74rem;
    margin-top:5px;
}

/* Success messages inside sidebar */
section[data-testid="stSidebar"] [data-testid="stAlert"] {
    background:rgba(24,143,94,.28) !important;
    border:1px solid rgba(70,190,132,.28) !important;
}
section[data-testid="stSidebar"] [data-testid="stAlert"] * {
    color:#FFFFFF !important;
}



/* V18.3 full contrast safety pass */

/* MAIN WORKSPACE */
[data-testid="stAppViewContainer"] label,
[data-testid="stAppViewContainer"] label p,
[data-testid="stAppViewContainer"] [data-testid="stWidgetLabel"] p {
    color:#17314D !important;
}

[data-testid="stAppViewContainer"] input,
[data-testid="stAppViewContainer"] textarea {
    background:#FFFFFF !important;
    color:#102943 !important;
    -webkit-text-fill-color:#102943 !important;
    caret-color:#1769E0 !important;
    border-color:#D4DFEA !important;
}

[data-testid="stAppViewContainer"] input::placeholder,
[data-testid="stAppViewContainer"] textarea::placeholder {
    color:#8193A5 !important;
    -webkit-text-fill-color:#8193A5 !important;
    opacity:1 !important;
}

[data-testid="stAppViewContainer"] input:disabled,
[data-testid="stAppViewContainer"] textarea:disabled {
    background:#EAF0F6 !important;
    color:#52677B !important;
    -webkit-text-fill-color:#52677B !important;
    opacity:1 !important;
}

[data-testid="stAppViewContainer"] div[data-baseweb="input"] > div,
[data-testid="stAppViewContainer"] div[data-baseweb="textarea"] > div,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] > div {
    background:#FFFFFF !important;
    color:#102943 !important;
    border-color:#D4DFEA !important;
}

[data-testid="stAppViewContainer"] div[data-baseweb="select"] span,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] input,
[data-testid="stAppViewContainer"] div[data-baseweb="select"] div {
    color:#102943 !important;
    -webkit-text-fill-color:#102943 !important;
}

[data-testid="stAppViewContainer"] div[data-baseweb="select"] svg {
    color:#52677B !important;
    fill:#52677B !important;
}

/* Dropdown menus */
div[role="listbox"], ul[role="listbox"] {
    background:#FFFFFF !important;
    color:#102943 !important;
}
div[role="option"], li[role="option"] {
    background:#FFFFFF !important;
    color:#102943 !important;
}
div[role="option"]:hover,
li[role="option"]:hover,
div[role="option"][aria-selected="true"],
li[role="option"][aria-selected="true"] {
    background:#EAF3FF !important;
    color:#0E56B7 !important;
}

/* Help / question-mark icons: never inherit filled button styling */
[data-testid="stTooltipIcon"] button,
[data-testid="stTooltipIcon"] [data-baseweb="button"],
button[aria-label="Help"] {
    background:transparent !important;
    border:none !important;
    box-shadow:none !important;
    padding:0 !important;
    color:#52677B !important;
}
[data-testid="stTooltipIcon"] button svg,
[data-testid="stTooltipIcon"] [data-baseweb="button"] svg,
button[aria-label="Help"] svg {
    color:#52677B !important;
    fill:none !important;
    stroke:#52677B !important;
}

/* V18.3: Streamlit renders help icons differently by widget type.
   Force every widget-label help trigger to use the same clean question-mark treatment. */
[data-testid="stWidgetLabel"] button,
[data-testid="stWidgetLabel"] [role="button"],
[data-testid="stTooltipHoverTarget"],
[data-testid="stTooltipHoverTarget"] button {
    background:transparent !important;
    background-color:transparent !important;
    border:0 !important;
    border-radius:0 !important;
    box-shadow:none !important;
    color:#52677B !important;
    padding:0 !important;
    min-width:auto !important;
}
[data-testid="stWidgetLabel"] button svg,
[data-testid="stWidgetLabel"] [role="button"] svg,
[data-testid="stTooltipHoverTarget"] svg,
[data-testid="stTooltipHoverTarget"] button svg {
    color:#52677B !important;
    fill:none !important;
    stroke:#52677B !important;
    background:transparent !important;
}

/* Number controls */
[data-testid="stNumberInput"] button {
    background:#EEF3F8 !important;
    color:#17314D !important;
    border-color:#D4DFEA !important;
}
[data-testid="stNumberInput"] button svg {
    color:#17314D !important;
    fill:#17314D !important;
}

/* Password eye */
[data-testid="stTextInput"] button,
[data-testid="stTextInput"] button[kind="minimal"],
[data-testid="stTextInput"] [data-baseweb="button"] {
    background:#EAF0F6 !important;
    color:#17314D !important;
    border-left:1px solid #D4DFEA !important;
}
[data-testid="stTextInput"] button svg,
[data-testid="stTextInput"] [data-baseweb="button"] svg {
    color:#17314D !important;
    fill:#17314D !important;
    stroke:#17314D !important;
}
[data-testid="stTextInput"] button:hover {
    background:#DDE8F3 !important;
}

/* MAIN buttons */
[data-testid="stAppViewContainer"] div.stButton > button {
    background:#FFFFFF !important;
    color:#17314D !important;
    border:1px solid #D4DFEA !important;
}
[data-testid="stAppViewContainer"] div.stButton > button p,
[data-testid="stAppViewContainer"] div.stButton > button span {
    color:#17314D !important;
}
[data-testid="stAppViewContainer"] div.stButton > button:hover {
    background:#EAF3FF !important;
    color:#0E56B7 !important;
    border-color:#1769E0 !important;
}
[data-testid="stAppViewContainer"] div.stButton > button:disabled {
    background:#E9EEF4 !important;
    color:#718396 !important;
    opacity:1 !important;
}
[data-testid="stAppViewContainer"] div.stButton > button:disabled p,
[data-testid="stAppViewContainer"] div.stButton > button:disabled span {
    color:#718396 !important;
}

/* SIDEBAR labels */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] label p,
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stMarkdown p {
    color:#FFFFFF !important;
}

/* SIDEBAR fields */
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea {
    background:#FFFFFF !important;
    color:#102943 !important;
    -webkit-text-fill-color:#102943 !important;
    opacity:1 !important;
}

section[data-testid="stSidebar"] input::placeholder,
section[data-testid="stSidebar"] textarea::placeholder {
    color:#8193A5 !important;
    -webkit-text-fill-color:#8193A5 !important;
    opacity:1 !important;
}

section[data-testid="stSidebar"] div[data-baseweb="select"] > div {
    background:#FFFFFF !important;
    color:#102943 !important;
    border-color:#D4DFEA !important;
}

/* Country / Entity type / all selects */
section[data-testid="stSidebar"] div[data-baseweb="select"] span,
section[data-testid="stSidebar"] div[data-baseweb="select"] input,
section[data-testid="stSidebar"] div[data-baseweb="select"] div {
    color:#102943 !important;
    -webkit-text-fill-color:#102943 !important;
}
section[data-testid="stSidebar"] div[data-baseweb="select"] svg {
    color:#52677B !important;
    fill:#52677B !important;
}

/* Sidebar password eye */
section[data-testid="stSidebar"] [data-testid="stTextInput"] button,
section[data-testid="stSidebar"] [data-testid="stTextInput"] [data-baseweb="button"] {
    background:#E7EDF4 !important;
    color:#17314D !important;
    border-left:1px solid #D4DFEA !important;
    opacity:1 !important;
}
section[data-testid="stSidebar"] [data-testid="stTextInput"] button svg,
section[data-testid="stSidebar"] [data-testid="stTextInput"] [data-baseweb="button"] svg {
    color:#17314D !important;
    fill:#17314D !important;
    stroke:#17314D !important;
    opacity:1 !important;
}

/* Sidebar save buttons */
section[data-testid="stSidebar"] div.stButton > button {
    background:#1769E0 !important;
    color:#FFFFFF !important;
    border:1px solid #2B7BE5 !important;
}
section[data-testid="stSidebar"] div.stButton > button p,
section[data-testid="stSidebar"] div.stButton > button span {
    color:#FFFFFF !important;
    -webkit-text-fill-color:#FFFFFF !important;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    background:#0E58C3 !important;
    color:#FFFFFF !important;
}
section[data-testid="stSidebar"] div.stButton > button:disabled {
    background:#38546F !important;
    color:#C5D3E0 !important;
    border-color:#496680 !important;
    opacity:1 !important;
}
section[data-testid="stSidebar"] div.stButton > button:disabled p,
section[data-testid="stSidebar"] div.stButton > button:disabled span {
    color:#C5D3E0 !important;
    -webkit-text-fill-color:#C5D3E0 !important;
}

/* Checkbox text */
section[data-testid="stSidebar"] [data-testid="stCheckbox"] p {
    color:#FFFFFF !important;
}
[data-testid="stAppViewContainer"] [data-testid="stCheckbox"] p {
    color:#17314D !important;
}

/* Expander summaries */
section[data-testid="stSidebar"] details > summary,
section[data-testid="stSidebar"] details > summary p,
section[data-testid="stSidebar"] details > summary span {
    color:#FFFFFF !important;
}

/* Alerts */
[data-testid="stAppViewContainer"] [data-testid="stAlert"] * {
    color:#17314D !important;
}
section[data-testid="stSidebar"] [data-testid="stAlert"] * {
    color:#FFFFFF !important;
}

/* V18.3 — FINAL HELP ICON FIX
   TextInput/NumberInput have broad button rules above for password/stepper controls.
   Streamlit places label help triggers inside those widget containers too, so those rules
   were repainting the help SVG as a solid dot. Keep this override LAST so help icons
   always match the correctly rendered DateInput question mark. */
[data-testid="stWidgetLabel"] [data-testid="stTooltipHoverTarget"],
[data-testid="stWidgetLabel"] [data-testid="stTooltipHoverTarget"] button,
[data-testid="stTextInput"] [data-testid="stWidgetLabel"] button,
[data-testid="stNumberInput"] [data-testid="stWidgetLabel"] button {
    background: transparent !important;
    background-color: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
    color: #52677B !important;
}
[data-testid="stWidgetLabel"] [data-testid="stTooltipHoverTarget"] svg,
[data-testid="stWidgetLabel"] [data-testid="stTooltipHoverTarget"] button svg,
[data-testid="stTextInput"] [data-testid="stWidgetLabel"] button svg,
[data-testid="stNumberInput"] [data-testid="stWidgetLabel"] button svg {
    color: #52677B !important;
    fill: none !important;
    stroke: #52677B !important;
    stroke-width: 1.8 !important;
    opacity: 1 !important;
}

/* Tabs */
button[data-baseweb="tab"][aria-selected="true"],
button[data-baseweb="tab"][aria-selected="true"] p {
    background:#1769E0 !important;
    color:#FFFFFF !important;
}
button[data-baseweb="tab"]:not([aria-selected="true"]),
button[data-baseweb="tab"]:not([aria-selected="true"]) p {
    background:#FFFFFF !important;
    color:#415B72 !important;
}



/* V17 authentication */
.auth-shell{max-width:980px;margin:3.2rem auto 0;}
.auth-brand{text-align:center;margin-bottom:22px;}
.auth-brand .logo{display:inline-flex;width:56px;height:56px;border-radius:16px;align-items:center;justify-content:center;background:linear-gradient(145deg,#17D0C1,#1769E0);color:#fff;font-size:26px;font-weight:900;box-shadow:0 10px 28px rgba(23,105,224,.20);}
.auth-brand h1{margin:12px 0 4px!important;}
.auth-brand p{color:#657B90;margin:0;}
.auth-card{background:#fff;border:1px solid #E0E8F0;border-radius:20px;padding:22px;box-shadow:0 12px 34px rgba(18,43,67,.07);}
.auth-note{background:#EAF3FF;border:1px solid #D1E2FA;color:#17314D;border-radius:12px;padding:12px 14px;font-size:.86rem;margin:8px 0 16px;}
.company-badge{display:inline-flex;align-items:center;gap:8px;background:#EAF3FF;color:#0E56B7;border:1px solid #CFE0FA;border-radius:999px;padding:7px 11px;font-weight:800;font-size:.78rem;}
.account-strip{
    display:flex!important;
    flex-direction:column!important;
    align-items:flex-start!important;
    gap:5px!important;
    background:#163551!important;
    border:1px solid rgba(255,255,255,.14)!important;
    border-radius:13px!important;
    padding:13px 14px!important;
    margin:0 0 10px!important;
    box-shadow:0 8px 22px rgba(0,0,0,.08)!important;
}
.account-strip div{width:100%!important;color:#FFFFFF!important;}
.account-strip b{
    display:block!important;
    color:#FFFFFF!important;
    -webkit-text-fill-color:#FFFFFF!important;
    font-size:.98rem!important;
    font-weight:800!important;
    margin-bottom:3px!important;
}
.account-strip span{
    color:#D5E4F2!important;
    -webkit-text-fill-color:#D5E4F2!important;
    font-size:.78rem!important;
    line-height:1.35!important;
}
.workspace-card{
    background:rgba(255,255,255,.07)!important;
    border:1px solid rgba(255,255,255,.12)!important;
    border-radius:13px!important;
    padding:12px!important;
    margin:0 0 12px!important;
}
.workspace-card h4,.workspace-card label,.workspace-card p{
    color:#FFFFFF!important;
    -webkit-text-fill-color:#FFFFFF!important;
}
.workspace-card [data-baseweb="select"] > div,
.workspace-card input{
    background:#FFFFFF!important;
    color:#102943!important;
    -webkit-text-fill-color:#102943!important;
}



/* V18.3 guest-first authentication */
.guest-card{
    background:rgba(255,255,255,.06);
    border:1px solid rgba(255,255,255,.10);
    border-radius:12px;
    padding:13px;
    margin:8px 0 12px;
}
.guest-card b{display:block;color:#FFFFFF!important;margin-bottom:4px;}
.guest-card span{display:block;color:#B9CCDE!important;font-size:.78rem;line-height:1.4;}

.auth-overlay-card{
    background:#FFFFFF;
    border:1px solid #DCE6EF;
    border-radius:18px;
    padding:18px 20px;
    box-shadow:0 12px 30px rgba(19,43,69,.08);
    margin:6px 0 18px;
}

/* Authentication inputs must stay light/readable regardless of sidebar theme */
.auth-overlay-card input,
.auth-overlay-card textarea,
.auth-overlay-card div[data-baseweb="input"] > div,
.auth-overlay-card div[data-baseweb="select"] > div{
    background:#FFFFFF!important;
    color:#102943!important;
    -webkit-text-fill-color:#102943!important;
    border-color:#D4DFEA!important;
}
.auth-overlay-card label,
.auth-overlay-card label p{
    color:#17314D!important;
}
.auth-overlay-card [data-testid="stTextInput"] button{
    background:#EAF0F6!important;
    color:#17314D!important;
}
.auth-overlay-card [data-testid="stTextInput"] button svg{
    fill:#17314D!important;
    stroke:#17314D!important;
}

/* Guest/read-only callout */
.guest-readonly{
    background:#F5F9FF;border:1px solid #D6E5F7;border-radius:12px;
    color:#31516F;padding:10px 12px;font-size:.82rem;
}

</style>
""",unsafe_allow_html=True)

# V19.4 per-user appearance preference.
_theme_user = current_user()
_theme_name = get_user_ui_theme(_theme_user["id"]) if _theme_user else "Light"
st.session_state["v19_ui_theme"] = _theme_name

if _theme_name == "Dark":
    st.markdown("""
    <style>
    /* =========================================================
       Sullivan V19.4.1 — complete dark theme
       ========================================================= */

    :root{
        --s-bg:#08121F;
        --s-bg-2:#0B1726;
        --s-panel:#111F30;
        --s-panel-2:#15263A;
        --s-panel-3:#1A2D43;
        --s-border:#29415A;
        --s-border-soft:#20364C;
        --s-text:#F2F6FA;
        --s-text-2:#D6E0EA;
        --s-muted:#9FB0C2;
        --s-blue:#4B9BFF;
        --s-blue-2:#1E73E8;
        --s-green:#47D18C;
        --s-amber:#FFBE55;
        --s-red:#FF6B75;
    }

    html, body,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    [data-testid="stMainBlockContainer"],
    .stApp{
        background:var(--s-bg)!important;
        color:var(--s-text)!important;
    }

    [data-testid="stHeader"]{
        background:var(--s-bg)!important;
        border-bottom:1px solid var(--s-border-soft)!important;
    }

    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"]{
        background:transparent!important;
        color:var(--s-text)!important;
    }

    [data-testid="stToolbar"] button,
    [data-testid="stToolbar"] svg{
        color:var(--s-text-2)!important;
        fill:currentColor!important;
        stroke:currentColor!important;
    }

    [data-testid="stSidebar"]{
        background:#06101D!important;
        border-right:1px solid var(--s-border-soft)!important;
    }

    [data-testid="stSidebar"] > div{
        background:#06101D!important;
    }

    [data-testid="stSidebar"] *{
        color:var(--s-text-2);
    }

    /* Global typography */
    h1,h2,h3,h4,h5,h6,
    .stMarkdown p,
    .stMarkdown li,
    label,
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] p{
        color:var(--s-text)!important;
    }

    .stCaption,
    small{
        color:var(--s-muted)!important;
    }

    a{
        color:#75B4FF!important;
    }

    hr{
        border-color:var(--s-border-soft)!important;
    }

    /* Navigation */
    div[data-baseweb="tab-list"]{
        border-bottom:1px solid var(--s-border-soft)!important;
    }

    button[data-baseweb="tab"]{
        background:transparent!important;
        border:1px solid transparent!important;
        box-shadow:none!important;
        color:#C3D0DD!important;
    }

    button[data-baseweb="tab"]:hover{
        background:var(--s-panel)!important;
        border-color:var(--s-border-soft)!important;
        color:#FFFFFF!important;
    }

    button[data-baseweb="tab"][aria-selected="true"]{
        background:var(--s-panel-2)!important;
        border-color:var(--s-border)!important;
        color:#FFFFFF!important;
    }

    button[data-baseweb="tab"] p{
        color:inherit!important;
    }

    /* Inputs / text areas / select boxes */
    div[data-baseweb="input"] > div,
    div[data-baseweb="select"] > div,
    div[data-baseweb="base-input"] > div,
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextArea"] textarea,
    textarea,
    input{
        background:#0C1A29!important;
        color:#F5F8FB!important;
        -webkit-text-fill-color:#F5F8FB!important;
        border-color:#35516D!important;
        caret-color:#FFFFFF!important;
    }

    input::placeholder,
    textarea::placeholder{
        color:#7F94A8!important;
        -webkit-text-fill-color:#7F94A8!important;
        opacity:1!important;
    }

    div[data-baseweb="select"] span,
    div[data-baseweb="select"] input,
    [role="combobox"]{
        color:#F5F8FB!important;
        -webkit-text-fill-color:#F5F8FB!important;
    }

    div[data-baseweb="popover"],
    div[data-baseweb="menu"],
    ul[role="listbox"]{
        background:var(--s-panel-2)!important;
        border-color:var(--s-border)!important;
        color:var(--s-text)!important;
    }

    li[role="option"]{
        background:var(--s-panel-2)!important;
        color:var(--s-text)!important;
    }

    li[role="option"]:hover{
        background:#203852!important;
    }

    /* Buttons */
    div.stButton > button,
    button[kind="secondary"],
    button[kind="tertiary"]{
        background:var(--s-panel-2)!important;
        color:#F3F7FA!important;
        border:1px solid #35516D!important;
        box-shadow:none!important;
    }

    div.stButton > button:hover,
    button[kind="secondary"]:hover,
    button[kind="tertiary"]:hover{
        background:#203852!important;
        color:#FFFFFF!important;
        border-color:#4B9BFF!important;
    }

    button[kind="primary"]{
        background:#1E73E8!important;
        color:#FFFFFF!important;
        border-color:#1E73E8!important;
    }

    button[kind="primary"]:hover{
        background:#2E83F6!important;
        border-color:#2E83F6!important;
    }

    /* Checkbox, toggle, radio */
    [data-testid="stCheckbox"] label,
    [data-testid="stRadio"] label,
    [data-testid="stToggle"] label{
        color:var(--s-text-2)!important;
    }

    /* Expanders / forms / common app cards */
    .account-strip,
    .workspace-card,
    .auth-note,
    .guest-card,
    .auth-overlay-card,
    .auth-card,
    .s15-system-card,
    div[data-testid="stExpander"],
    details[data-testid="stExpander"],
    div[data-testid="stForm"]{
        background:var(--s-panel)!important;
        border-color:var(--s-border)!important;
        color:var(--s-text)!important;
        box-shadow:none!important;
    }

    div[data-testid="stExpander"] summary,
    details[data-testid="stExpander"] summary{
        color:var(--s-text)!important;
    }

    /* Home hero — darken the image instead of leaving a white wash */
    .hero-v15{
        position:relative!important;
        border-color:var(--s-border)!important;
        box-shadow:0 10px 28px rgba(0,0,0,.28)!important;
        isolation:isolate!important;
    }

    .hero-v15::before{
        content:"";
        position:absolute;
        inset:0;
        z-index:-1;
        border-radius:inherit;
        background:linear-gradient(
            90deg,
            rgba(5,14,24,.96) 0%,
            rgba(5,14,24,.86) 42%,
            rgba(5,14,24,.48) 76%,
            rgba(5,14,24,.30) 100%
        );
        pointer-events:none;
    }

    .hero-v15 h1{
        color:#F6FAFD!important;
        text-shadow:0 1px 4px rgba(0,0,0,.25);
    }

    .hero-v15 p{
        color:#C9D7E4!important;
    }

    .hero-kicker{
        color:#71B5FF!important;
    }

    .hero-chip{
        background:rgba(13,29,45,.90)!important;
        border-color:#35516D!important;
        color:#E8F0F7!important;
        box-shadow:none!important;
    }

    .hero-chip span{
        color:#E8F0F7!important;
    }

    /* Home KPI cards */
    .kpi-v15{
        background:var(--s-panel)!important;
        border-color:var(--s-border)!important;
        box-shadow:0 7px 20px rgba(0,0,0,.16)!important;
    }

    .kpi-head{
        color:#DDE7F0!important;
    }

    .kpi-sub{
        color:var(--s-muted)!important;
    }

    /* Keep KPI accent values colorful but readable */
    .kpi-value{
        filter:saturate(.92) brightness(1.16);
    }

    /* Home panels and activity */
    .section-heading{
        color:#EAF1F7!important;
    }

    .panel-v15{
        background:var(--s-panel)!important;
        border-color:var(--s-border)!important;
        box-shadow:0 7px 20px rgba(0,0,0,.16)!important;
    }

    .list-row,
    .activity-v15{
        border-bottom-color:var(--s-border-soft)!important;
    }

    .list-copy b,
    .empty-state{
        color:#E6EEF5!important;
    }

    .list-copy span,
    .empty-state span,
    .activity-amount span,
    .quick-labels span{
        color:var(--s-muted)!important;
    }

    /* Native Streamlit metrics */
    [data-testid="stMetric"]{
        background:var(--s-panel)!important;
        border:1px solid var(--s-border)!important;
        box-shadow:none!important;
    }

    [data-testid="stMetricLabel"],
    [data-testid="stMetricLabel"] *{
        color:#AFC0D0!important;
    }

    [data-testid="stMetricValue"],
    [data-testid="stMetricValue"] *{
        color:#F4F8FB!important;
    }

    [data-testid="stMetricDelta"],
    [data-testid="stMetricDelta"] *{
        color:#BFD0DF!important;
    }

    /* Alerts */
    [data-testid="stAlert"]{
        border:1px solid var(--s-border)!important;
        color:var(--s-text)!important;
    }

    [data-testid="stAlert"] p,
    [data-testid="stAlert"] span{
        color:inherit!important;
    }

    /* Dataframes / tables */
    [data-testid="stDataFrame"],
    [data-testid="stTable"],
    [data-testid="stDataFrameResizable"]{
        background:var(--s-panel)!important;
        border-color:var(--s-border)!important;
        color:var(--s-text)!important;
    }

    /* File uploader / widgets */
    [data-testid="stFileUploaderDropzone"]{
        background:var(--s-panel)!important;
        border-color:#35516D!important;
    }

    [data-testid="stFileUploaderDropzone"] *{
        color:var(--s-text-2)!important;
    }

    /* Code and JSON */
    pre,
    code,
    [data-testid="stCodeBlock"]{
        background:#07111D!important;
        color:#DDE9F3!important;
        border-color:var(--s-border)!important;
    }

    /* Sidebar cards and workspace controls */
    .account-strip,
    .workspace-card{
        background:#102237!important;
        border-color:#2D4862!important;
    }

    .account-strip b,
    .workspace-card h4,
    .workspace-card label,
    .workspace-card p{
        color:#F4F8FB!important;
    }

    .account-strip span,
    .workspace-card .stCaption{
        color:#B5C5D4!important;
    }

    /* Settings page */
    [data-testid="stVerticalBlockBorderWrapper"]{
        border-color:var(--s-border)!important;
    }

    /* Footer */
    .footer-v15{
        background:#050D17!important;
        border:1px solid var(--s-border-soft)!important;
        color:#BFD0DF!important;
    }

    .footer-v15 b{
        color:#FFFFFF!important;
    }

    .footer-v15 small{
        color:#8FA6BA!important;
    }

    /* Scrollbars */
    ::-webkit-scrollbar{
        width:10px;
        height:10px;
    }

    ::-webkit-scrollbar-track{
        background:#08121F;
    }

    ::-webkit-scrollbar-thumb{
        background:#2D455C;
        border-radius:999px;
        border:2px solid #08121F;
    }

    ::-webkit-scrollbar-thumb:hover{
        background:#3B5873;
    }

    /* =========================================================
       V19.4.2 visibility pass
       - all editable fields are WHITE with BLACK text
       - all tab labels are bright/readable
       - all widget labels remain visible in dark mode
       ========================================================= */

    /* MAIN NAV TABS + INNER TABS */
    div[data-baseweb="tab-list"] button[data-baseweb="tab"],
    div[data-baseweb="tab-list"] button[data-baseweb="tab"] p,
    div[data-baseweb="tab-list"] button[data-baseweb="tab"] span {
        color:#EAF2F8!important;
        -webkit-text-fill-color:#EAF2F8!important;
        opacity:1!important;
        font-weight:600!important;
    }

    div[data-baseweb="tab-list"] button[data-baseweb="tab"][aria-selected="true"],
    div[data-baseweb="tab-list"] button[data-baseweb="tab"][aria-selected="true"] p,
    div[data-baseweb="tab-list"] button[data-baseweb="tab"][aria-selected="true"] span {
        color:#FFFFFF!important;
        -webkit-text-fill-color:#FFFFFF!important;
    }

    /* STREAMLIT WIDGET LABELS */
    [data-testid="stWidgetLabel"],
    [data-testid="stWidgetLabel"] *,
    [data-testid="stTextInput"] label,
    [data-testid="stTextInput"] label *,
    [data-testid="stTextArea"] label,
    [data-testid="stTextArea"] label *,
    [data-testid="stNumberInput"] label,
    [data-testid="stNumberInput"] label *,
    [data-testid="stSelectbox"] label,
    [data-testid="stSelectbox"] label *,
    [data-testid="stDateInput"] label,
    [data-testid="stDateInput"] label *,
    [data-testid="stCheckbox"] label,
    [data-testid="stCheckbox"] label *,
    [data-testid="stRadio"] label,
    [data-testid="stRadio"] label *,
    [data-testid="stToggle"] label,
    [data-testid="stToggle"] label * {
        color:#F2F6FA!important;
        -webkit-text-fill-color:#F2F6FA!important;
        opacity:1!important;
    }

    /* EVERY EDITABLE WRITING FIELD: white background / black text */
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stDateInput"] input,
    [data-testid="stTimeInput"] input,
    div[data-baseweb="input"] input,
    div[data-baseweb="base-input"] input,
    textarea {
        background:#FFFFFF!important;
        color:#111111!important;
        -webkit-text-fill-color:#111111!important;
        caret-color:#111111!important;
        border-color:#CAD4DE!important;
    }

    [data-testid="stTextInput"] > div > div,
    [data-testid="stNumberInput"] > div > div,
    [data-testid="stTextArea"] > div > div,
    [data-testid="stDateInput"] > div > div,
    [data-testid="stTimeInput"] > div > div,
    div[data-baseweb="input"] > div,
    div[data-baseweb="base-input"] > div {
        background:#FFFFFF!important;
        color:#111111!important;
        border-color:#CAD4DE!important;
    }

    /* Input placeholders */
    [data-testid="stTextInput"] input::placeholder,
    [data-testid="stTextArea"] textarea::placeholder,
    [data-testid="stNumberInput"] input::placeholder,
    input::placeholder,
    textarea::placeholder {
        color:#6B7280!important;
        -webkit-text-fill-color:#6B7280!important;
        opacity:1!important;
    }

    /* Number input +/- controls */
    [data-testid="stNumberInput"] button {
        background:#F2F5F8!important;
        color:#111111!important;
        border-color:#CAD4DE!important;
    }
    [data-testid="stNumberInput"] button svg {
        color:#111111!important;
        fill:#111111!important;
        stroke:#111111!important;
    }

    /* SELECTBOXES: white with black text */
    [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
    [data-testid="stMultiSelect"] div[data-baseweb="select"] > div,
    div[data-baseweb="select"] > div {
        background:#FFFFFF!important;
        color:#111111!important;
        border-color:#CAD4DE!important;
    }

    [data-testid="stSelectbox"] div[data-baseweb="select"] *,
    [data-testid="stMultiSelect"] div[data-baseweb="select"] *,
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] input {
        color:#111111!important;
        -webkit-text-fill-color:#111111!important;
    }

    [data-testid="stSelectbox"] svg,
    [data-testid="stMultiSelect"] svg,
    div[data-baseweb="select"] svg {
        fill:#111111!important;
        color:#111111!important;
    }

    /* Select dropdown menu remains dark, but readable */
    ul[role="listbox"],
    div[data-baseweb="menu"] {
        background:#132338!important;
        border:1px solid #35516D!important;
    }
    li[role="option"],
    li[role="option"] * {
        color:#FFFFFF!important;
        -webkit-text-fill-color:#FFFFFF!important;
    }
    li[role="option"]:hover,
    li[role="option"][aria-selected="true"] {
        background:#23415F!important;
    }

    /* AUTOCOMPLETE / SEARCH FIELDS */
    [data-testid="stTextInput"] svg,
    [data-baseweb="input"] svg {
        color:#111111!important;
        fill:#111111!important;
    }

    /* DATA EDITOR CELLS / INLINE EDITING */
    [data-testid="stDataFrame"] input,
    [data-testid="stDataEditor"] input,
    [data-testid="stDataFrame"] textarea,
    [data-testid="stDataEditor"] textarea {
        background:#FFFFFF!important;
        color:#111111!important;
        -webkit-text-fill-color:#111111!important;
    }

    /* File uploader browse button */
    [data-testid="stFileUploader"] button {
        background:#FFFFFF!important;
        color:#111111!important;
        border-color:#CAD4DE!important;
    }

    /* Sidebar business settings fields */
    [data-testid="stSidebar"] input,
    [data-testid="stSidebar"] textarea,
    [data-testid="stSidebar"] div[data-baseweb="select"] > div {
        background:#FFFFFF!important;
        color:#111111!important;
        -webkit-text-fill-color:#111111!important;
        border-color:#CAD4DE!important;
    }

    [data-testid="stSidebar"] div[data-baseweb="select"] span,
    [data-testid="stSidebar"] div[data-baseweb="select"] input {
        color:#111111!important;
        -webkit-text-fill-color:#111111!important;
    }

    [data-testid="stSidebar"] div[data-baseweb="select"] svg {
        fill:#111111!important;
        color:#111111!important;
    }

    /* Generic headings and subheadings inside tabs */
    [data-testid="stMain"] h1,
    [data-testid="stMain"] h2,
    [data-testid="stMain"] h3,
    [data-testid="stMain"] h4,
    [data-testid="stMain"] h5,
    [data-testid="stMain"] h6 {
        color:#FFFFFF!important;
        -webkit-text-fill-color:#FFFFFF!important;
    }

    /* Captions/descriptions should never become near-black */
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] *,
    .stCaption,
    .stCaption * {
        color:#AFC0D0!important;
        -webkit-text-fill-color:#AFC0D0!important;
    }

    /* Markdown/body text */
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li,
    [data-testid="stMarkdownContainer"] strong,
    [data-testid="stMarkdownContainer"] em {
        color:#E7EEF5!important;
    }

    /* Quick-action white buttons intentionally remain white with dark text */
    .quick-action button,
    .quick-actions button {
        background:#FFFFFF!important;
        color:#122033!important;
        -webkit-text-fill-color:#122033!important;
    }


    /* V19.4.3 — dark-mode hint/help text */
    [data-testid="stAlert"] {
        background:#13263A!important;
        border:1px solid #35516D!important;
    }

    [data-testid="stAlert"] *,
    [data-testid="stAlert"] p,
    [data-testid="stAlert"] strong,
    [data-testid="stAlert"] li,
    [data-testid="stAlert"] h1,
    [data-testid="stAlert"] h2,
    [data-testid="stAlert"] h3,
    [data-testid="stAlert"] h4 {
        color:#F2F6FA!important;
        -webkit-text-fill-color:#F2F6FA!important;
        opacity:1!important;
    }

    [data-testid="stAlert"] svg {
        color:#75B4FF!important;
        fill:currentColor!important;
        stroke:currentColor!important;
    }

    /* Help text beneath/beside widgets */
    [data-testid="stTooltipIcon"],
    [data-testid="stTooltipIcon"] svg {
        color:#AFC0D0!important;
        fill:#AFC0D0!important;
        stroke:#AFC0D0!important;
    }

    .st-emotion-cache-1pbsqtx,
    [data-testid="InputInstructions"],
    [data-testid="InputInstructions"] *,
    [data-testid="stFileUploader"] small,
    [data-testid="stFileUploader"] small * {
        color:#B8C7D5!important;
        -webkit-text-fill-color:#B8C7D5!important;
        opacity:1!important;
    }

    /* Dashboard metric labels, especially under the cash outlook chart */
    [data-testid="stMetric"] {
        background:#132338!important;
        border:1px solid #35516D!important;
        border-radius:14px!important;
        padding:14px 16px!important;
    }

    [data-testid="stMetricLabel"],
    [data-testid="stMetricLabel"] *,
    [data-testid="stMetricLabel"] p {
        color:#BFD0DF!important;
        -webkit-text-fill-color:#BFD0DF!important;
        opacity:1!important;
        font-weight:600!important;
    }

    [data-testid="stMetricValue"],
    [data-testid="stMetricValue"] *,
    [data-testid="stMetricValue"] div {
        color:#FFFFFF!important;
        -webkit-text-fill-color:#FFFFFF!important;
        opacity:1!important;
    }

    /* General explanatory text in custom dark cards */
    .panel-v15 p,
    .panel-v15 span,
    .auth-note p,
    .auth-note span,
    .workspace-card p,
    .workspace-card span {
        color:#C7D5E2!important;
        -webkit-text-fill-color:#C7D5E2!important;
    }
    </style>
    """, unsafe_allow_html=True)



# ============================================================
# V19.6 — FREE SULLIVAN AI HELP
# ============================================================
def v196_support_ai(question):
    """
    Built-in Sullivan product support.
    IMPORTANT: this intentionally does NOT call v18_require_ai_credits(),
    v18_consume_ai_credits(), or v18_log_ai_usage(), so support questions
    never consume the customer's normal Sullivan AI points.
    """
    q = str(question or "").strip()
    if not q:
        return "Tell me what you're trying to do in Sullivan and I'll walk you through it."

    if not key():
        return "Sullivan AI Help is not configured on this server yet."

    support_instructions = """
You are Sullivan AI Help, the built-in customer support assistant for Sullivan Accounting.

Help the user operate Sullivan clearly and quickly. Focus on Sullivan product support:
dashboard navigation, importing transactions, transaction categorization, Question Queue,
General Ledger, Manual Journals, customers, vendors, estimates, invoices, credit notes,
bills, payments, AR/AP, reconciliation, Smart Close, Tax Center, Accounting Periods,
reports, Personal vs Company workspaces, switching workspaces, workspace settings,
subscriptions, AI usage, and interface settings.

Rules:
1. Give short, practical, step-by-step instructions.
2. Never claim an action happened unless the user says it happened.
3. If you are not sure of an exact Sullivan button or location, say so instead of inventing it.
4. Never ask for passwords, API keys, secret keys, full card numbers, or bank credentials.
5. You may explain basic bookkeeping concepts needed to use Sullivan.
6. Do not present product support as professional legal, tax, or accounting advice.
7. For consequential tax/legal/accounting decisions, explain the Sullivan workflow and tell
   the user to confirm the underlying professional decision with a qualified professional.
8. Sullivan AI Help is free support. Never tell the user that this support chat uses or
   deducts their normal Sullivan AI points.
"""

    try:
        client = OpenAI(api_key=key())
        response = client.responses.create(
            model=MODEL,
            instructions=support_instructions,
            input=q,
            max_output_tokens=700,
        )
        answer = getattr(response, "output_text", None)
        if answer:
            return answer.strip()

        # Defensive fallback for SDK response variants.
        try:
            pieces = []
            for item in response.output:
                for content in getattr(item, "content", []):
                    txt = getattr(content, "text", None)
                    if txt:
                        pieces.append(str(txt))
            if pieces:
                return "\n".join(pieces).strip()
        except Exception:
            pass

        return "I couldn't generate a support answer. Please try rephrasing the question."
    except Exception as e:
        return f"Sullivan AI Help couldn't connect right now. Please try again. ({type(e).__name__})"


def v196_render_support_ai():
    st.markdown("#### ✨ Ask Sullivan AI")
    st.write(
        "Get free help using Sullivan — transactions, invoices, reconciliation, "
        "workspaces, reports, settings, and more."
    )
    st.success("Free support: questions here do **not** use your normal Sullivan AI points.")

    if "v196_support_messages" not in st.session_state:
        st.session_state["v196_support_messages"] = []

    st.caption("Quick help")
    q1, q2, q3 = st.columns(3)
    quick = None

    with q1:
        if st.button(
            "Import transactions",
            key="v196_quick_import",
            width="stretch"
        ):
            quick = "How do I import transactions into Sullivan?"

    with q2:
        if st.button(
            "Reconcile an account",
            key="v196_quick_reconcile",
            width="stretch"
        ):
            quick = "How do I reconcile an account in Sullivan?"

    with q3:
        if st.button(
            "Personal vs Company",
            key="v196_quick_workspace",
            width="stretch"
        ):
            quick = (
                "Explain the difference between Personal and Company workspaces "
                "in Sullivan and how I safely switch between them."
            )

    for message in st.session_state["v196_support_messages"][-10:]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    typed = st.chat_input(
        "Ask Sullivan AI for help…",
        key="v196_support_chat_input"
    )
    question = quick or typed

    if question:
        st.session_state["v196_support_messages"].append(
            {"role": "user", "content": question}
        )

        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Sullivan AI is helping…"):
                answer = v196_support_ai(question)
            st.markdown(answer)

        st.session_state["v196_support_messages"].append(
            {"role": "assistant", "content": answer}
        )

    if st.session_state["v196_support_messages"]:
        if st.button(
            "Clear support chat",
            key="v196_clear_support_chat",
            width="content"
        ):
            st.session_state["v196_support_messages"] = []
            st.rerun()

    st.caption(
        "Human email support can be added later. For now, Sullivan AI handles simple product-help questions for free."
    )




with st.sidebar:
    st.markdown("---")
    st.markdown("### ✨ Free AI Help")
    st.caption(
        "Need help using Sullivan? Open **Settings → Help & Support**. "
        "Support questions do not use your normal AI points."
    )

main_sections=st.tabs(["Home","Money In","Money Out","Bank","Taxes","Reports","Team","Plan & AI","Advanced","Settings"])


with main_sections[0]:
    home_tabs=[st.container()]
with main_sections[1]:
    sales_tabs=st.tabs(["Customers","Estimates","Invoices","Credit Notes","Recurring","Statements"])
with main_sections[2]:
    expense_tabs=st.tabs(["Vendors","Purchase Orders","Bills","Documents"])
with main_sections[3]:
    banking_tabs=st.tabs(["Bank Activity","Reconciliation"])
with main_sections[4]:
    tax_tabs=st.tabs(["Tax Center"])
with main_sections[5]:
    report_tabs=st.tabs(["Financial Reports","Money Owed / Bills Owed"])
with main_sections[6]:
    st.subheader("Team")

    if not v171_is_signed_in():
        st.markdown(
            '<div class="auth-note"><b>Company employee?</b><br>'
            'You can browse Sullivan without signing in. To join your employer and access the company workspace, sign in first, then enter the invite code your employer gave you.</div>',
            unsafe_allow_html=True
        )
        if st.button("Sign in to join a company",type="primary",key="v171_team_guest_signin"):
            v171_open_auth("Sign in first, then join your employer with their invite code.")
            st.rerun()
    else:
        auth_u=current_user()
        auth_c=current_company()
        role=st.session_state.get("auth_role","Employee")

        if not auth_c:
            st.info("Choose a company workspace or join your employer.")
        elif int(auth_c.get("company_id",0))==0:
            st.info("Personal workspaces do not have employee seats.")
        else:
            st.markdown(
                f'<div class="auth-note"><b>{auth_c["company_name"]}</b><br>'
                f'Company ID: <b>{auth_c["company_code"]}</b><br>'
                'Employees cannot join using the Company ID alone. They need an invite code created by the employer.</div>',
                unsafe_allow_html=True
            )

            members=read("""SELECT u.user_code,u.full_name,u.email,m.role,m.status,m.joined_at
                            FROM company_members m
                            JOIN app_users u ON u.id=m.user_id
                            WHERE m.company_id=?
                            ORDER BY CASE m.role WHEN 'Owner' THEN 1 WHEN 'Accountant' THEN 2 WHEN 'Manager' THEN 3 ELSE 4 END,u.full_name""",
                         (int(auth_c["company_id"]),))
            if not members.empty:
                st.dataframe(members,use_container_width=True,hide_index=True)

            if role in ("Owner","Manager"):
                billing=v18_credit_status(int(auth_c["company_id"]))
                seats_used=len(members) if not members.empty else 0
                st.markdown(f"**Team seats:** {seats_used} / {billing['seat_limit']}")
                if seats_used >= billing["seat_limit"]:
                    st.warning("Your current Sullivan plan has no open employee seats.")
                st.markdown("### Invite employee")
                inv_email=st.text_input("Employee email",key="v171_invite_email")
                inv_role=st.selectbox("Role",["Employee","Manager","Accountant"],key="v171_invite_role")
                if st.button("Create one-time invite code",type="primary",key="v171_invite_btn"):
                    code=create_company_invite(auth_c["company_id"],auth_u["id"],inv_role,inv_email)
                    st.success(f"Invite created: **{code}**")
                    st.info("Give this code to the employee. If an email was entered, only that email can use it.")

                invites=read("""SELECT invite_code,invited_email,role,status,created_at,used_at
                                FROM company_invites WHERE company_id=? ORDER BY id DESC""",
                             (int(auth_c["company_id"]),))
                if not invites.empty:
                    st.markdown("### Invite history")
                    st.dataframe(invites,use_container_width=True,hide_index=True)
            else:
                st.info("Only an Owner or Manager can invite employees.")

with main_sections[7]:
    st.subheader("Plan & AI")
    st.caption("Manage your Sullivan membership, team seats, billing status, and AI usage.")

    if not v171_is_signed_in():
        st.info("Browse freely. Sign in when you're ready to create a company and try Sullivan AI.")
        if st.button("Sign in to see plans",type="primary",key="v18_plan_signin"):
            v171_open_auth("Sign in to create a company, try Sullivan AI, and choose a membership.")
            st.rerun()
    else:
        billing_company=current_company()
        if not billing_company:
            st.info("You're in your personal workspace. Create or open a company workspace to use company memberships.")
            st.caption("Use **Manage workspace** in the left sidebar.")
        else:
            cid=int(billing_company.get("company_id",0) or 0)
            if cid<=0:
                st.info("Personal workspaces don't use company memberships. Create a company from Manage workspace.")
            else:
                status=v18_credit_status(cid)
                members=read("""SELECT COUNT(*) AS n FROM company_members
                                WHERE company_id=? AND status='Active'""",(cid,))
                seats_used=int(members.iloc[0].n) if not members.empty else 0

                st.markdown(
                    f'<div class="auth-note"><b>{status["company_name"]}</b><br>'
                    f'Company ID: <b>{status["company_code"]}</b> &nbsp; • &nbsp; '
                    f'Current plan: <b>{status["plan"]}</b></div>',
                    unsafe_allow_html=True
                )

                b1,b2,b3,b4=st.columns(4)
                b1.metric("Plan",status["plan"])
                b2.metric("AI credits remaining",f'{status["remaining"]:,}')
                b3.metric("AI credits used",f'{status["used"]:,} / {status["limit"]:,}')
                b4.metric("Team seats",f'{seats_used} / {status["seat_limit"]}')


                # V19 safe billing diagnostic: never exposes secrets.
                billing_diag = v19_billing_diagnostic(cid)
                st.markdown("### Billing sync")
                if billing_diag["state"] == "connected":
                    st.success(
                        f'✅ Supabase connected — '
                        f'{billing_diag["remote_plan"]} / {billing_diag["remote_status"]}'
                    )
                    st.caption(
                        f'Company {billing_diag["company_id"]} • '
                        f'{int(billing_diag["remote_ai_credits"] or 0):,} AI credits • '
                        f'{int(billing_diag["remote_seat_limit"] or 1)} seats'
                    )
                else:
                    st.error(
                        "❌ Supabase billing sync is not connected.\n\n"
                        f"**Reason:** {billing_diag['message'] or billing_diag['state']}"
                    )
                    st.caption(
                        f'Company {billing_diag["company_id"]} • '
                        f'State: {billing_diag["state"]} • '
                        f'Secrets detected: {"Yes" if billing_diag["supabase_configured"] else "No"}'
                    )

                if status["plan"]=="Trial":
                    st.markdown("### Try Sullivan AI once — free")
                    saved_demo=v18_demo_result(cid)

                    if not status["demo_used"]:
                        st.write(
                            "Before paying, test Sullivan AI on one small transaction. "
                            "This preview only suggests a category and explanation — it **does not post anything to your books**."
                        )
                        d1,d2=st.columns([2,1])
                        demo_desc=d1.text_input(
                            "Transaction description",
                            value="Home Depot - Milwaukee drill",
                            key="v18_demo_desc"
                        )
                        demo_amt=d2.number_input(
                            "Amount",
                            value=-184.72,
                            step=10.0,
                            format="%.2f",
                            key="v18_demo_amount"
                        )

                        if st.button("✨ Analyze my free demo transaction",type="primary",key="v18_demo_button"):
                            try:
                                v18_run_demo(demo_desc,demo_amt,p)
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    else:
                        st.success("✓ Your company has used its one free Sullivan AI transaction demo.")

                    if saved_demo:
                        st.markdown("#### Your Sullivan AI demo")
                        st.write(f"**Transaction:** {saved_demo['description']}")
                        st.write(f"**Amount:** ${abs(float(saved_demo['amount'])):,.2f}")
                        r1,r2=st.columns(2)
                        r1.metric("Suggested category",saved_demo["category"])
                        r2.metric("Suggested account",saved_demo["account"])
                        st.write(f"**Why Sullivan suggested it:** {saved_demo['explanation']}")
                        if saved_demo.get("question"):
                            st.info(f"One thing Sullivan would ask before final posting: {saved_demo['question']}")
                        st.caption(
                            f"Confidence: {float(saved_demo['confidence']):.0%}. "
                            "Free preview only — nothing was posted to the books."
                        )

                st.markdown("### Sullivan plans")

                p1,p2,p3=st.columns(3)
                first_row=[(p1,"Starter"),(p2,"Business"),(p3,"Pro")]
                for col,name in first_row:
                    spec=SULLIVAN_PLANS[name]
                    with col:
                        st.markdown(f"### {name}")
                        st.markdown(f"## ${spec['price']}/mo")
                        st.write(spec["label"])
                        st.write(f"**{spec['ai_credits']:,}** AI credits / month")
                        st.write(f"**{spec['seat_limit']}** team seat{'s' if spec['seat_limit']!=1 else ''}")
                        st.caption("Normal accounting actions do not use AI credits.")
                        if stripe_checkout_ready(name):
                            if st.button(
                                ("Current plan ✓" if status["plan"] == name and status["status"] == "Active" else f"Choose {name}"),
                                type="primary",
                                use_container_width=True,
                                key=f"v183_plan_{name.lower()}"
                            ):
                                try:
                                    checkout_url=v183_create_checkout_session(cid,name)
                                    st.link_button(
                                        "Continue to secure Stripe checkout →",
                                        checkout_url,
                                        use_container_width=True
                                    )
                                    st.info("Stripe Checkout is ready. Click the button above to continue.")
                                except Exception as e:
                                    st.error(str(e))
                        else:
                            st.button(
                                "Stripe setup incomplete",
                                disabled=True,
                                use_container_width=True,
                                key=f"v183_missing_{name.lower()}"
                            )

                f1,f2=st.columns(2)

                with f1:
                    spec=SULLIVAN_PLANS["Accounting Firm"]
                    st.markdown("### Accounting Firm")
                    st.markdown("## $250/mo")
                    st.write("Built for accounting firms and larger finance teams.")
                    st.write("**30,000** AI credits / month")
                    st.write("**Up to 50 people**")
                    st.write("Designed for future multi-client workspace management.")
                    st.caption("Normal accounting actions do not use AI credits.")
                    if stripe_checkout_ready("Accounting Firm"):
                        if st.button(
                            ("Current plan ✓" if status["plan"] == "Accounting Firm" and status["status"] == "Active" else "Choose Accounting Firm"),
                            type="primary",
                            use_container_width=True,
                            key="v183_plan_accounting_firm"
                        ):
                            try:
                                checkout_url=v183_create_checkout_session(cid,"Accounting Firm")
                                st.link_button(
                                    "Continue to secure Stripe checkout →",
                                    checkout_url,
                                    use_container_width=True
                                )
                                st.info("Stripe Checkout is ready. Click the button above to continue.")
                            except Exception as e:
                                st.error(str(e))
                    else:
                        st.button(
                            "Stripe setup incomplete",
                            disabled=True,
                            use_container_width=True,
                            key="v183_missing_accounting_firm"
                        )

                with f2:
                    st.markdown("### Enterprise")
                    st.markdown("## Custom quote")
                    st.write("For organizations needing **51+ people**.")
                    st.write("Seat limits and AI allowances are customized.")
                    st.write("Get a preliminary Sullivan estimate below.")
                    st.caption("Final enterprise pricing requires approval before any contract is created.")

                st.markdown("### Enterprise quote")
                st.write(
                    "Enter how many people need Sullivan. This calculator starts at 51 people "
                    "and gives a preliminary AI-assisted estimate for planning."
                )

                q1,q2=st.columns([1,1])
                quote_seats=q1.number_input(
                    "How many people need access?",
                    min_value=51,
                    value=75,
                    step=1,
                    key="v182_enterprise_seats"
                )
                quote_usage=q2.selectbox(
                    "Expected Sullivan AI usage",
                    ["Light","Standard","Heavy","Very heavy"],
                    index=1,
                    key="v182_enterprise_ai_usage"
                )

                if st.button("✨ Get enterprise AI quote",type="primary",key="v182_enterprise_quote"):
                    try:
                        quote=v18_enterprise_quote(quote_seats,quote_usage)
                        v18_save_enterprise_quote(
                            cid,quote["seats"],quote["usage"],quote["estimate"],quote["summary"]
                        )
                        st.session_state["v182_quote"]=quote
                    except Exception as e:
                        st.error(str(e))

                if st.session_state.get("v182_quote"):
                    quote=st.session_state["v182_quote"]
                    st.success("Preliminary Sullivan Enterprise estimate")
                    qv1,qv2=st.columns(2)
                    qv1.metric("People",f'{quote["seats"]:,}')
                    qv2.metric("Estimated monthly price",f'${quote["estimate"]:,.2f}')
                    st.write(quote["summary"])
                    st.caption(
                        "This estimate does not activate a plan, charge a card, or create a binding agreement. "
                        "A final enterprise quote would be approved before purchase."
                    )

                st.info(
                    "V18.3 still does not activate paid plans. Stripe billing is the next connection step, "
                    "so no disabled plan button can accidentally grant paid access."
                )

                usage=read("""SELECT action,credits,source,detail,created_at
                              FROM ai_usage WHERE company_id=?
                              ORDER BY id DESC LIMIT 25""",(cid,))
                with st.expander("AI usage history"):
                    if usage.empty:
                        st.caption("No Sullivan AI usage recorded yet.")
                    else:
                        st.dataframe(usage,use_container_width=True,hide_index=True)

with main_sections[8]:
    accountant_tabs=st.tabs([
        "Import & Analyze","Question Queue","Chart of Accounts","Opening Balances",
        "Saved Ledger","General Ledger","Manual Journals","Corrections / Reversals",
        "Accounting Periods","Smart Close","Integrity Center","Audit Trail","Accountant Export"
    ])

with main_sections[9]:
    st.subheader("Settings")
    st.caption("Manage your Sullivan preferences, workspaces, account details, and support options.")

    settings_user = current_user()

    if not settings_user:
        st.info("Sign in to manage Sullivan settings.")
        if st.button("Sign in to open settings", type="primary", key="v194_settings_signin"):
            v171_open_auth("Sign in to manage your Sullivan settings.")
            st.rerun()
    else:
        pref_tab, workspace_tab, support_tab, account_tab = st.tabs([
            "Preferences","Workspaces","Help & Support","Account"
        ])

        with pref_tab:
            st.markdown("### Appearance")
            current_theme = get_user_ui_theme(settings_user["id"])
            chosen_theme = st.radio(
                "Theme",
                ["Light","Dark"],
                index=0 if current_theme == "Light" else 1,
                horizontal=True,
                key="v194_theme_choice"
            )
            st.caption(
                "Dark mode changes Sullivan's interface only. Your accounting data and reports are not changed."
            )

            if st.button("Save appearance", type="primary", key="v194_save_theme"):
                try:
                    save_user_ui_theme(settings_user["id"], chosen_theme)
                    st.session_state["v19_ui_theme"] = chosen_theme
                    st.success(f"Appearance saved: {chosen_theme} mode.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            st.markdown("### Workspace behavior")
            active_settings_company = current_company()
            if active_settings_company:
                st.info(
                    f'Current workspace: **{active_settings_company["company_name"]}** '
                    f'({active_settings_company["company_code"]})'
                )
            else:
                st.info("Current workspace: **Personal**")

            st.caption(
                "Sullivan remembers the last workspace you confirmed. Switching workspaces always asks for confirmation."
            )

        with workspace_tab:
            st.markdown("### Workspace manager")
            st.write(
                "Review every company workspace you own. Company IDs are shown so duplicate names can always be distinguished."
            )

            owner_workspaces = owned_company_workspaces(settings_user["id"])

            if owner_workspaces.empty:
                st.info("You do not own any company workspaces yet.")
            else:
                for _, wr in owner_workspaces.iterrows():
                    wid = int(wr.company_id)
                    wname = str(wr.company_name)
                    wcode = str(wr.company_code)
                    wplan = str(wr.subscription_plan or "Trial")
                    wstatus = str(wr.subscription_status or "Trial")

                    with st.expander(f"{wname} · {wcode}", expanded=False):
                        c1,c2,c3 = st.columns(3)
                        c1.metric("Plan", wplan)
                        c2.metric("Subscription", wstatus)
                        c3.metric("Company ID", wid)

                        is_active = bool(
                            current_company()
                            and int(current_company().get("company_id", -1)) == wid
                        )
                        if is_active:
                            st.success("This is your current workspace.")

                        st.markdown("#### Rename workspace")
                        new_workspace_name = st.text_input(
                            "Company name",
                            value=wname,
                            key=f"v194_rename_name_{wid}"
                        )
                        if st.button(
                            "Rename company",
                            key=f"v194_rename_btn_{wid}",
                            width="content"
                        ):
                            try:
                                old_name, renamed = rename_company_workspace(
                                    settings_user["id"], wid, new_workspace_name
                                )
                                if is_active and st.session_state.get("auth_company"):
                                    st.session_state["auth_company"]["company_name"] = renamed
                                st.session_state["v19_workspace_select_reset"] = None
                                st.success(f'Renamed "{old_name}" to "{renamed}".')
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                        st.markdown("#### Delete workspace")
                        st.warning(
                            "Permanent deletion removes this company's Sullivan bookkeeping workspace. "
                            "This cannot be undone."
                        )
                        delete_confirm = st.text_input(
                            f'Type "{wname}" to delete this company',
                            key=f"v194_delete_name_{wid}"
                        )

                        if st.button(
                            "Delete company permanently",
                            key=f"v194_delete_btn_{wid}",
                            width="content"
                        ):
                            try:
                                deleted_name = delete_company_workspace(
                                    settings_user["id"], wid, delete_confirm
                                )

                                if is_active:
                                    activate_workspace(settings_user, None, persist=True)
                                    st.session_state["v19_workspace_select_reset"] = "Personal"

                                st.success(f'Deleted company workspace "{deleted_name}".')
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

            st.divider()
            st.caption(
                "Personal is your private Sullivan account and cannot be deleted here. "
                "Each company and Personal have completely separate bookkeeping records."
            )

        with support_tab:
            st.markdown("### Help & Support")
            st.caption(
                "Start with Sullivan AI for fast product help. Human support can be added later as Sullivan grows."
            )

            v196_render_support_ai()

            st.divider()
            st.markdown("#### Useful account information")
            st.code(
                f"Sullivan User ID: {settings_user.get('user_code','')}\n"
                f"Signed in with: {settings_user.get('auth_provider','email').title()}",
                language=None
            )

        with account_tab:
            st.markdown("### Account")
            st.write(f"**Name:** {settings_user.get('full_name','')}")
            st.write(f"**Email:** {settings_user.get('email','')}")
            st.write(f"**Sullivan User ID:** {settings_user.get('user_code','')}")
            st.write(
                f"**Sign-in method:** {settings_user.get('auth_provider','email').title()}"
            )

            st.markdown("### Security")
            st.caption(
                "Billing API keys, Supabase credentials, Google authentication credentials, "
                "and OpenAI keys stay in deployment secrets and are never displayed here."
            )

# V18.3 navigation / action clarity
if st.session_state.get("v13_destination"):
    st.success("You are looking for: **" + st.session_state["v13_destination"] + "**")
    if st.button("Clear destination"):
        st.session_state.pop("v13_destination",None)
        st.session_state.pop("v13_action",None)
        st.rerun()

with home_tabs[0]:
    m=v13_home_metrics()
    business=v14_business_name()
    now=date.today()
    hour=pd.Timestamp.now().hour
    greeting="Good morning" if hour<12 else ("Good afternoon" if hour<18 else "Good evening")

    st.markdown(
        f"""<section class="hero-v15">
            <div class="hero-copy">
                <div class="hero-kicker">TODAY'S BUSINESS OVERVIEW</div>
                <h1>{greeting} 👋</h1>
                <p>Here's what's happening with your business today.</p>
            </div>
            <div class="hero-meta">
                <div class="hero-chip">📅 <span>{now.strftime("%b %d, %Y")}</span></div>
                <div class="hero-chip">🏢 <span>{business}</span></div>
            </div>
        </section>""",
        unsafe_allow_html=True
    )

    kpis=[
        ("$","Money in bank",m["bank"],"#E9F8EF","#159C52","Current bank balance"),
        ("↗","Money earned",m["revenue"],"#EAF3FF","#1769E0","This month"),
        ("■","Money left (profit)",m["profit"],"#F0ECFF","#6750D8","After expenses"),
        ("◷","GST/QST to remit",m["tax_due"],"#FFF2DF","#F08A00","Estimated sales tax"),
        ("●●","Customers owe you",m["ar"],"#E4F6FA","#0786A4",f'{m["overdue_count"]} overdue'),
        ("▤","Bills you owe",m["ap"],"#FDE9EC","#E9344E",f'{m["due_ap_count"]} due soon'),
    ]
    cards=""
    for icon,label,value,bg,color,sub in kpis:
        cards += (
            '<div class="kpi-v15">'
            '<div class="kpi-head"><span class="kpi-icon" style="background:'+bg+';color:'+color+'">'+icon+'</span>'
            '<span>'+label+'</span></div>'
            '<div class="kpi-value" style="color:'+color+'">$'+f"{value:,.2f}"+'</div>'
            '<div class="kpi-sub">'+sub+'</div></div>'
        )
    st.markdown('<div class="kpi-grid-v15">'+cards+'</div>',unsafe_allow_html=True)

    top_left,top_right=st.columns([1.12,1],gap="medium")

    with top_left:
        st.markdown('<div class="section-heading">What needs your attention</div>',unsafe_allow_html=True)
        alerts=[]
        if m["overdue_count"]:
            alerts.append(("!","#FFF0D8","#F28A00",f'{m["overdue_count"]} invoices are overdue',f'Total overdue: ${m["overdue_total"]:,.2f}'))
        if m["due_ap_count"]:
            alerts.append(("◷","#FFF0D8","#F28A00",f'{m["due_ap_count"]} bills are due within 7 days',f'Total due: ${m["due_ap_total"]:,.2f}'))
        if m["unreconciled"]:
            alerts.append(("i","#EAF3FF","#1769E0",f'{m["unreconciled"]} bank transactions need review',"Go to Bank → Reconciliation"))
        if not alerts:
            alerts.append(("✓","#E9F8EF","#159C52","You're all caught up","Nothing urgent needs attention right now."))

        rows=""
        for icon,bg,color,title,sub in alerts:
            rows += (
                '<div class="list-row"><span class="round-v15" style="background:'+bg+';color:'+color+'">'+icon+'</span>'
                '<div class="list-copy"><b>'+title+'</b><span>'+sub+'</span></div></div>'
            )
        st.markdown('<div class="panel-v15">'+rows+'</div>',unsafe_allow_html=True)

    with top_right:
        st.markdown('<div class="section-heading">Quick actions</div>',unsafe_allow_html=True)
        a,b,c=st.columns(3,gap="small")
        if a.button("Send invoice",use_container_width=True):
            st.session_state["v13_destination"]="Money In → Invoices"; st.rerun()
        if b.button("Enter a bill",use_container_width=True):
            st.session_state["v13_destination"]="Money Out → Bills"; st.rerun()
        if c.button("Record payment",use_container_width=True):
            st.session_state["v13_destination"]="Money In → Invoices"; st.rerun()
        st.markdown('<div class="quick-labels"><span>Create & send</span><span>Add a bill to pay</span><span>Customer paid you</span></div>',unsafe_allow_html=True)

        d,e,f=st.columns(3,gap="small")
        if d.button("Match bank",use_container_width=True):
            st.session_state["v13_destination"]="Bank → Reconciliation"; st.rerun()
        if e.button("Add expense",use_container_width=True):
            st.session_state["v13_destination"]="Money Out → Bills"; st.rerun()
        if f.button("See reports",use_container_width=True):
            st.session_state["v13_destination"]="Reports"; st.rerun()

        if st.session_state.get("v13_destination"):
            st.info("Next step: **"+st.session_state["v13_destination"]+"**")

    lower_left,lower_right=st.columns([1.12,1],gap="medium")

    with lower_left:
        st.markdown('<div class="section-heading">Cash activity</div>',unsafe_allow_html=True)
        # V19.5 — Recent cash activity.
        # No Altair/Vega dependency: Sullivan renders a responsive stock-style SVG
        # using the current workspace's actual recent bank transactions.
        try:
            activity = trans().copy()

            if not activity.empty:
                activity["amount"] = pd.to_numeric(activity["amount"], errors="coerce").fillna(0.0)
                activity["chart_date"] = pd.to_datetime(activity["date"], errors="coerce")
                activity = activity.dropna(subset=["chart_date"])
                activity = activity.sort_values(["chart_date", "id"]).tail(30).reset_index(drop=True)

            current_cash = float(m["bank"])

            if not activity.empty:
                displayed_net = float(activity["amount"].sum())
                opening_cash = current_cash - displayed_net

                cash_points = [opening_cash]
                cash_dates = [activity.iloc[0]["chart_date"] - pd.Timedelta(days=1)]
                event_amounts = [0.0]

                running = opening_cash
                for _, row in activity.iterrows():
                    running += float(row["amount"])
                    cash_points.append(running)
                    cash_dates.append(row["chart_date"])
                    event_amounts.append(float(row["amount"]))
            else:
                today_ts = pd.Timestamp(date.today())
                cash_points = [current_cash, current_cash]
                cash_dates = [today_ts - pd.Timedelta(days=7), today_ts]
                event_amounts = [0.0, 0.0]

            is_dark = st.session_state.get("v19_ui_theme") == "Dark"

            card_bg = "#102338" if is_dark else "#FFFFFF"
            plot_bg = "#0A1828" if is_dark else "#F4F9FD"
            border = "#294763" if is_dark else "#DCE8F2"
            grid = "#29445E" if is_dark else "#DCE8F2"
            text_main = "#F7FBFF" if is_dark else "#102A43"
            text_muted = "#9FB4C8" if is_dark else "#718499"

            blue = "#2F80ED"
            blue_light = "#69AEFF"
            green = "#22C879"
            red = "#F25563"

            W, H = 1000.0, 330.0
            left, right, top, bottom = 74.0, 24.0, 24.0, 48.0
            plot_w = W - left - right
            plot_h = H - top - bottom

            vals = [float(v) for v in cash_points]
            raw_min = min(vals)
            raw_max = max(vals)
            span = max(raw_max - raw_min, abs(raw_max) * 0.025, 75.0)
            pad = max(span * 0.28, 75.0)
            y_min = raw_min - pad
            y_max = raw_max + pad
            y_span = max(y_max - y_min, 1.0)

            n = max(len(vals), 1)

            def x_at(i):
                if n <= 1:
                    return left + plot_w / 2
                return left + (i / (n - 1)) * plot_w

            def y_at(v):
                return top + (1 - ((float(v) - y_min) / y_span)) * plot_h

            pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals)]

            def smooth_svg_path(points):
                if not points:
                    return ""
                if len(points) == 1:
                    x, y = points[0]
                    return f"M {x:.2f} {y:.2f}"

                out = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
                for i in range(1, len(points)):
                    x0, y0 = points[i - 1]
                    x1, y1 = points[i]
                    mid_x = (x0 + x1) / 2
                    out.append(
                        f"C {mid_x:.2f} {y0:.2f}, {mid_x:.2f} {y1:.2f}, {x1:.2f} {y1:.2f}"
                    )
                return " ".join(out)

            line_path = smooth_svg_path(pts)
            floor_y = top + plot_h
            area_path = (
                line_path
                + f" L {pts[-1][0]:.2f} {floor_y:.2f}"
                + f" L {pts[0][0]:.2f} {floor_y:.2f} Z"
            )

            grid_parts = []
            for i in range(4):
                value = y_min + (y_span * i / 3)
                yy = y_at(value)
                grid_parts.append(
                    f'<line x1="{left:.1f}" y1="{yy:.1f}" x2="{W-right:.1f}" y2="{yy:.1f}" '
                    f'stroke="{grid}" stroke-width="1" opacity="0.42" stroke-dasharray="4 7"/>'
                )
                grid_parts.append(
                    f'<text x="{left-12:.1f}" y="{yy+4:.1f}" text-anchor="end" '
                    f'fill="{text_muted}" font-size="11">${value:,.0f}</text>'
                )

            date_parts = []
            label_count = min(6, len(cash_dates))
            if label_count:
                indexes = sorted(set(
                    int(round(i * (len(cash_dates)-1) / max(label_count-1, 1)))
                    for i in range(label_count)
                ))
                for idx in indexes:
                    label = pd.to_datetime(cash_dates[idx]).strftime("%b %d")
                    date_parts.append(
                        f'<text x="{x_at(idx):.1f}" y="{H-15:.1f}" text-anchor="middle" '
                        f'fill="{text_muted}" font-size="11">{label}</text>'
                    )

            event_parts = []
            for i in range(1, len(vals)):
                amount = float(event_amounts[i])
                x, y = pts[i]
                dt_label = pd.to_datetime(cash_dates[i]).strftime("%b %d, %Y")
                event_color = green if amount >= 0 else red
                direction = "Money in" if amount >= 0 else "Money out"

                event_parts.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5.0" '
                    f'fill="{event_color}" stroke="{plot_bg}" stroke-width="2.2">'
                    f'<title>{dt_label} — {direction} ${abs(amount):,.2f} — Cash ${vals[i]:,.2f}</title>'
                    f'</circle>'
                )
                event_parts.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="12" fill="transparent">'
                    f'<title>{dt_label} — Cash ${vals[i]:,.2f}</title></circle>'
                )

            start_cash = float(vals[0])
            end_cash = float(vals[-1])
            change = end_cash - start_cash
            change_sign = "+" if change >= 0 else ""
            change_color = green if change >= 0 else red

            end_x, end_y = pts[-1]

            chart_html = textwrap.dedent(f"""
                <style>
                html, body {{
                    margin:0;
                    padding:0;
                    background:transparent;
                    overflow:hidden;
                    font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                }}
                * {{ box-sizing:border-box; }}
                </style>
                <div style="
                    background:{card_bg};
                    border:1px solid {border};
                    border-radius:24px;
                    overflow:hidden;
                    box-shadow:0 14px 34px rgba(0,0,0,.08);
                ">
                    <div style="
                        display:flex;
                        justify-content:space-between;
                        align-items:flex-end;
                        flex-wrap:wrap;
                        gap:16px;
                        padding:20px 22px 12px 22px;
                    ">
                        <div>
                            <div style="
                                color:{text_muted};
                                font-size:.72rem;
                                font-weight:800;
                                letter-spacing:.08em;
                                text-transform:uppercase;
                            ">Recent cash activity</div>
                            <div style="
                                color:{text_main};
                                font-size:2rem;
                                line-height:1.05;
                                font-weight:850;
                                margin-top:5px;
                            ">${end_cash:,.2f}</div>
                        </div>

                        <div style="text-align:right;">
                            <div style="color:{text_muted};font-size:.77rem;font-weight:650;">
                                Last {len(activity)} transactions
                            </div>
                            <div style="
                                color:{change_color};
                                font-size:1rem;
                                font-weight:850;
                                margin-top:4px;
                            ">{change_sign}${change:,.2f}</div>
                        </div>
                    </div>

                    <div style="
                        margin:6px 12px 12px 12px;
                        background:{plot_bg};
                        border:1px solid {border};
                        border-radius:20px;
                        overflow:hidden;
                    ">
                        <svg viewBox="0 0 {W:.0f} {H:.0f}"
                             width="100%"
                             preserveAspectRatio="xMidYMid meet"
                             style="display:block;min-height:260px;">
                            <defs>
                                <linearGradient id="cashFillV195" x1="0" y1="0" x2="0" y2="1">
                                    <stop offset="0%" stop-color="{blue}" stop-opacity=".28"/>
                                    <stop offset="60%" stop-color="{blue}" stop-opacity=".08"/>
                                    <stop offset="100%" stop-color="{blue}" stop-opacity="0"/>
                                </linearGradient>
                                <filter id="cashGlowV195" x="-20%" y="-20%" width="140%" height="140%">
                                    <feGaussianBlur stdDeviation="4"/>
                                </filter>
                            </defs>

                            {''.join(grid_parts)}
                            {''.join(date_parts)}

                            <path d="{area_path}" fill="url(#cashFillV195)"/>

                            <path d="{line_path}"
                                  fill="none"
                                  stroke="{blue_light}"
                                  stroke-width="10"
                                  opacity=".10"
                                  stroke-linecap="round"
                                  stroke-linejoin="round"
                                  filter="url(#cashGlowV195)"/>

                            <path d="{line_path}"
                                  fill="none"
                                  stroke="{blue}"
                                  stroke-width="4"
                                  stroke-linecap="round"
                                  stroke-linejoin="round"/>

                            {''.join(event_parts)}

                            <circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="10"
                                    fill="{blue}" opacity=".16"/>
                            <circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="5.2"
                                    fill="{blue}" stroke="{plot_bg}" stroke-width="2"/>
                        </svg>
                    </div>

                    <div style="
                        display:flex;
                        gap:18px;
                        align-items:center;
                        flex-wrap:wrap;
                        color:{text_muted};
                        font-size:.76rem;
                        padding:0 20px 18px 20px;
                    ">
                        <span><b style="color:{blue};">●</b>&nbsp; Cash balance</span>
                        <span><b style="color:{green};">●</b>&nbsp; Money in</span>
                        <span><b style="color:{red};">●</b>&nbsp; Money out</span>
                        <span style="margin-left:auto;">Hover over transaction points for details</span>
                    </div>
                </div>
                """).strip()

            # V19.5.2: render the SVG inside a real HTML component.
            # st.markdown was still treating portions of the SVG/HTML as Markdown
            # source in Streamlit Cloud. components.html bypasses Markdown parsing.
            components.html(
                chart_html,
                height=500,
                scrolling=False,
            )

        except Exception as cash_chart_error:
            st.error(
                f"Cash activity chart error: "
                f"{type(cash_chart_error).__name__}: {cash_chart_error}"
            )

        x,y,z=st.columns(3)
        recent_tx = trans().copy()
        if not recent_tx.empty:
            recent_tx["amount"] = pd.to_numeric(recent_tx["amount"], errors="coerce").fillna(0.0)
            recent_tx = recent_tx.tail(30)
            recent_in = float(recent_tx.loc[recent_tx["amount"] > 0, "amount"].sum())
            recent_out = abs(float(recent_tx.loc[recent_tx["amount"] < 0, "amount"].sum()))
        else:
            recent_in = 0.0
            recent_out = 0.0
        x.metric("Recent money in",f"${recent_in:,.2f}")
        y.metric("Recent money out",f"${recent_out:,.2f}")
        z.metric("Current cash",f'${float(m["bank"]):,.2f}')
        st.caption("Based on the most recent bank transactions in this workspace.")

    with lower_right:
        st.markdown('<div class="section-heading">Recent activity</div>',unsafe_allow_html=True)
        activity=v14_recent_activity(5)
        if not activity:
            st.markdown('<div class="panel-v15 empty-state"><b>No recent activity yet</b><span>Your latest invoices and bills will appear here.</span></div>',unsafe_allow_html=True)
        else:
            rows=""
            for item in activity:
                positive=item["amount"]>=0
                bg="#E9F8EF" if positive else "#FDE9EC"
                color="#159C52" if positive else "#E9344E"
                symbol="↗" if positive else "↘"
                amount=("+" if positive else "-")+"$"+f"{abs(item['amount']):,.2f}"
                rows += (
                    '<div class="activity-v15"><span class="round-v15" style="background:'+bg+';color:'+color+'">'+symbol+'</span>'
                    '<div class="list-copy"><b>'+item["title"]+'</b><span>'+item["detail"]+'</span></div>'
                    '<div class="activity-amount" style="color:'+color+'"><b>'+amount+'</b><span>'+item["date"]+'</span></div></div>'
                )
            st.markdown('<div class="panel-v15">'+rows+'</div>',unsafe_allow_html=True)

    st.markdown(
        '<footer class="footer-v15"><span>🛡 Your data is protected &nbsp; • &nbsp; Accounting controls are active &nbsp; • &nbsp; Advanced tools stay available</span><b>◆ Sullivan <small>V18.3</small></b></footer>',
        unsafe_allow_html=True
    )

with accountant_tabs[0]:
    st.subheader("Bring in bank transactions")
    st.caption("No special filing system needed. Export transactions from your bank, drop the file here, and Sullivan does the cleanup.")

    st.info(
        "### The easy way\n"
        "**1.** Download transactions from your bank as CSV or Excel.  "
        "**2.** Drag one or several files below.  "
        "**3.** Sullivan combines them, removes duplicate imports when saving, and prepares them for review."
    )

    uploads=st.file_uploader(
        "Drag your bank files here",
        type=["csv","xlsx","xls"],
        accept_multiple_files=True,
        key="easy_bank_import",
        help="You can select several monthly exports at once. CSV and Excel files are supported."
    )

    if uploads:
        frames=[]
        import_errors=[]
        for uploaded_file in uploads:
            try:
                name=uploaded_file.name.lower()
                if name.endswith(".csv"):
                    try:
                        source=pd.read_csv(uploaded_file)
                    except UnicodeDecodeError:
                        uploaded_file.seek(0)
                        source=pd.read_csv(uploaded_file,encoding="latin-1")
                else:
                    source=pd.read_excel(uploaded_file)
                cleaned=normalize_csv(source)
                cleaned["_source_file"]=uploaded_file.name
                frames.append(cleaned)
            except Exception as e:
                import_errors.append((uploaded_file.name,str(e)))

        for fname,msg in import_errors:
            st.error(f"Could not read **{fname}** automatically: {msg}")

        if frames:
            raw=pd.concat(frames,ignore_index=True)
            raw["date"]=pd.to_datetime(raw["date"],errors="coerce").dt.date.astype(str)
            raw=raw.dropna(subset=["amount"])

            # Remove exact duplicates inside this upload batch before analysis.
            before=len(raw)
            raw=raw.drop_duplicates(subset=["date","description","amount"],keep="first").reset_index(drop=True)
            removed=before-len(raw)

            c1,c2,c3=st.columns(3)
            c1.metric("Files accepted",len(frames))
            c2.metric("Transactions found",len(raw))
            c3.metric("Duplicates skipped",removed)

            with st.expander("Preview what Sullivan found",expanded=False):
                st.dataframe(raw[["date","description","amount","_source_file"]],use_container_width=True,hide_index=True)

            st.success("Files look readable. Sullivan is ready to organize these transactions.")

            if st.button("✨ Organize my transactions",type="primary",key="easy_analyze"):
                try:
                    work_input=raw[["date","description","amount"]].copy()
                    st.session_state["work"]=analyze(work_input,p)
                    st.session_state["filename"]=" + ".join([u.name for u in uploads])
                    st.session_state["easy_import_done"]=True
                    st.rerun()
                except Exception as e:
                    st.error(f"Sullivan could not analyze this import: {e}")

    if "work" in st.session_state:
        w=st.session_state["work"]
        ready_count=int((w.status=="Ready for books").sum()) if "status" in w.columns else 0
        question_count=int((w.status=="Needs your answer").sum()) if "status" in w.columns else 0

        st.markdown("## Sullivan organized your transactions")
        a,b=st.columns(2)
        a.metric("Ready",ready_count)
        b.metric("Need a quick answer",question_count)

        if question_count:
            st.warning(
                f"{question_count} transaction(s) need your help before Sullivan can confidently finish them. "
                "Open **Advanced → Question Queue** and answer what each purchase was for."
            )
        else:
            st.success("Everything in this batch is ready. You do not have to fix categories manually.")

        with st.expander("Review transaction details",expanded=False):
            st.dataframe(w,use_container_width=True,hide_index=True)

        registered_for_sales_tax=bool(gstreg or qstreg)
        if registered_for_sales_tax:
            if st.button("Save imported transactions",type="primary",key="easy_save_tax"):
                s,u=save_rows(w,st.session_state.get("filename","Bank import"))
                st.success(
                    f"Done. {s} new transaction(s) saved and {u} existing transaction(s) updated. "
                    "Next, Sullivan will keep tax review available in Taxes before posting."
                )
        else:
            if st.button("✅ Save everything that's ready",type="primary",key="easy_save_post"):
                try:
                    s,u=save_rows(w,st.session_state.get("filename","Bank import"))
                    result=post_bank()
                    msg=(f"Done — {s} new transaction(s) saved, {u} existing transaction(s) updated, "
                         f"and {result['posted_sources']} ready transaction(s) posted to your books.")
                    if question_count:
                        msg += f" {question_count} item(s) are still waiting for your answer in Question Queue."
                    if result["skipped_locked"]:
                        msg += f" {result['skipped_locked']} item(s) were protected because their accounting period is locked."
                    st.success(msg)
                except Exception as e:
                    st.error(f"Could not finish the import: {e}")

        st.caption("Sullivan keeps the original accounting controls underneath this simple workflow; this screen just removes the filing and navigation work.")

with accountant_tabs[1]:
    st.subheader("Question Queue")
    if "work" not in st.session_state:st.info("Analyze a CSV first.")
    else:
        pending=st.session_state["work"][st.session_state["work"].status=="Needs your answer"]
        if pending.empty:st.success("Everything in this import is ready for books.")
        for idx,r in pending.iterrows():
            with st.container(border=True):
                st.write(f"**{r.description} — {r.amount}**");st.write(r.question or "What was this for?")
                ans=st.text_area("Owner answer",key=f"a{idx}")
                if st.button("Resolve with AI",key=f"r{idx}"):
                    if not ans.strip():st.warning("Enter an answer.")
                    elif not key():st.error("Configure API key.")
                    else:
                        x=resolve_ai(OpenAI(api_key=key()),p,r,ans.strip())
                        for k2,v in x.items():st.session_state["work"].at[idx,k2]=v
                        st.session_state["work"].at[idx,"account"]=CATEGORY_TO_ACCOUNT.get(x["category"],"6999 Uncategorized Expense")
                        if x["status"]=="Ready for books":learn(r.description,x["category"]);log("question_resolved","working_transaction",idx,{"answer":ans,"category":x["category"]})
                        st.rerun()

with accountant_tabs[2]:
    st.subheader("Chart of Accounts")
    ac=read("SELECT * FROM accounts ORDER BY code");st.dataframe(ac,use_container_width=True,hide_index=True)
    with st.container(border=True):
        code=st.text_input("New account code");name=st.text_input("New account name")
        typ=st.selectbox("Account type",["Asset","Liability","Equity","Revenue","Expense"])
        nat=st.selectbox("Natural balance",["Debit","Credit"]);grp=st.text_input("Report group")
        if st.button("Create account"):
            if not code or not name:st.warning("Code and name required.")
            else:
                def f(c):
                    c.execute("INSERT INTO accounts(code,name,type,natural_balance,group_name,active,system_account) VALUES(?,?,?,?,?,1,0)",(code,name,typ,nat,grp))
                    audit_row(c,"account_created","account",code,{"name":name,"type":typ})
                try:write(f);st.success("Account created.")
                except Exception as e:st.error(str(e))
    non_system=ac[ac.system_account==0]
    if not non_system.empty:
        opts={f"{r.code} {r['name']}":r.code for _,r in non_system.iterrows()}
        ch=st.selectbox("Custom account to activate/deactivate",list(opts))
        active=st.checkbox("Active",value=True)
        if st.button("Update account status"):
            write(lambda c:c.execute("UPDATE accounts SET active=? WHERE code=?",(int(active),opts[ch])));log("account_status_changed","account",opts[ch],{"active":active});st.success("Updated.")

with accountant_tabs[3]:
    st.subheader("Opening Balances")
    st.caption("Use once when moving existing books into Sullivan. Debits must equal credits.")
    od=st.date_input("Opening balance date",date.today(),key="obd");lines=[]
    for i in range(6):
        c1,c2,c3=st.columns([2,1,1]);acct=c1.selectbox(f"Account {i+1}",active_accounts(),key=f"oba{i}")
        dr=c2.number_input(f"Debit {i+1}",0.0,step=100.0,key=f"obd{i}");cr=c3.number_input(f"Credit {i+1}",0.0,step=100.0,key=f"obc{i}");lines.append((acct,dr,cr))
    td=sum(x[1] for x in lines);tc=sum(x[2] for x in lines);st.write(f"Debits **${td:,.2f}** | Credits **${tc:,.2f}** | Difference **${td-tc:,.2f}**")
    if st.button("Create opening balance journal",disabled=abs(td-tc)>.001 or td<=0):
        try:jid=opening_balance(od,lines);post_journal(jid);st.success("Opening balances posted.")
        except Exception as e:st.error(str(e))

with expense_tabs[3]:
    st.subheader("Documents & Receipts")
    et=st.selectbox("Attach to",["Transaction","Invoice","Bill","Journal","General"])
    eid=st.text_input("Entity ID / reference");uploaded=st.file_uploader("Receipt, invoice or supporting document",type=["pdf","png","jpg","jpeg","csv","txt"],key="doc")
    note=st.text_input("Document note")
    if st.button("Attach document",disabled=uploaded is None):
        create_simple_doc(et,eid or "general",uploaded,note);st.success("Document stored and linked.")
    d=docs()
    if not d.empty:st.dataframe(d,use_container_width=True,hide_index=True)

with accountant_tabs[4]:
    st.subheader("Saved Ledger")
    t = trans()

    if t.empty:
        st.info("No saved transactions.")
    else:
        posted_ids = set(
            read(
                """SELECT DISTINCT transaction_id
                   FROM journal_entries
                   WHERE source_type='Bank' AND transaction_id IS NOT NULL"""
            ).transaction_id.dropna().astype(int).tolist()
        )

        view = t.copy()
        view["gl_posted"] = view["id"].astype(int).isin(posted_ids)
        preferred_cols = [
            "id","date","description","amount","category","account","status",
            "gst_amount","qst_amount","tax_included","tax_eligible","tax_reviewed",
            "business_use_pct","gl_posted","confidence","review","explanation"
        ]
        shown_cols = [c for c in preferred_cols if c in view.columns]
        st.dataframe(view[shown_cols],use_container_width=True,hide_index=True)

        ready_unposted = view[
            (view.status=="Ready for books") &
            (~view.gl_posted)
        ]

        c1,c2,c3 = st.columns(3)
        c1.metric("Saved transactions", len(view))
        c2.metric("Ready but unposted", len(ready_unposted))
        c3.metric("Already in GL", int(view.gl_posted.sum()))

        if gstreg or qstreg:
            tax_pending = view[
                (~view.gl_posted) &
                (view.status=="Ready for books") &
                (view.tax_reviewed.fillna(0).astype(int)==0)
            ]
            st.metric("Awaiting Tax Center review", len(tax_pending))

        if st.button("Post all ready transactions to General Ledger",type="primary"):
            try:
                result = post_bank()
                st.session_state["saved_ledger_post_msg"] = (
                    f"Verified posting complete: {result['posted_sources']} transaction(s), "
                    f"{result['posted_rows']} GL row(s)."
                )
                if result["skipped_locked"]:
                    st.session_state["saved_ledger_post_msg"] += (
                        f" {result['skipped_locked']} locked-period transaction(s) skipped."
                    )
                if result.get("skipped_tax_review"):
                    ids = ", ".join(str(x) for x in result["skipped_tax_review"])
                    st.session_state["saved_ledger_post_msg"] += (
                        f" Tax review required before posting transaction ID(s): {ids}."
                    )
                st.rerun()
            except Exception as e:
                st.error(f"Posting failed: {e}")

        if st.session_state.get("saved_ledger_post_msg"):
            st.success(st.session_state.pop("saved_ledger_post_msg"))

with accountant_tabs[5]:
    st.subheader("General Ledger")
    st.caption("Advanced view: Sullivan records your approved invoices, bills, payments and other accounting entries here automatically. Most users do not need to change anything on this screen.")
    j = gl()
    status = gl_status()

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("GL rows", status["rows"])
    c2.metric("Total debits", f"${status['debits']:,.2f}")
    c3.metric("Total credits", f"${status['credits']:,.2f}")
    c4.metric("Difference", f"${status['difference']:,.2f}")

    if j.empty:
        st.info(
            "Nothing has been posted to the General Ledger yet. "
            "Saving a transaction or journal is not the same as posting it."
        )
    else:
        st.dataframe(j,use_container_width=True,hide_index=True)
        if abs(status["difference"]) < 0.01:
            st.success("General Ledger is balanced.")
        else:
            st.error("General Ledger is out of balance.")

with accountant_tabs[6]:
    st.subheader("Manual Journal Entries")
    st.caption("Create controlled double-entry adjustments. Sullivan will not allow an unbalanced journal or a journal dated inside a locked period.")

    if "manual_journal_no_value" not in st.session_state:
        st.session_state["manual_journal_no_value"] = generate_unique_journal_no()

    j_no = st.text_input(
        "Journal number",
        value=st.session_state["manual_journal_no_value"],
        disabled=True,
        help="Sullivan generates a unique number automatically for every journal."
    )
    j_date = st.date_input("Journal date", date.today(), key="manual_journal_date")
    j_memo = st.text_input("Journal memo", key="manual_journal_memo")

    journal_lines = []
    st.write("Enter up to 6 lines.")

    for i in range(6):
        c1, c2, c3 = st.columns([2,1,1])
        acct = c1.selectbox(
            f"Account {i+1}",
            active_accounts(),
            key=f"manual_acct_{i}"
        )
        dr = c2.number_input(
            f"Debit {i+1}",
            min_value=0.0,
            value=0.0,
            step=10.0,
            key=f"manual_dr_{i}"
        )
        cr = c3.number_input(
            f"Credit {i+1}",
            min_value=0.0,
            value=0.0,
            step=10.0,
            key=f"manual_cr_{i}"
        )
        journal_lines.append((acct, dr, cr))

    total_debits = sum(float(x[1]) for x in journal_lines)
    total_credits = sum(float(x[2]) for x in journal_lines)
    difference = round(total_debits-total_credits, 2)

    m1,m2,m3 = st.columns(3)
    m1.metric("Debits", f"${total_debits:,.2f}")
    m2.metric("Credits", f"${total_credits:,.2f}")
    m3.metric("Difference", f"${difference:,.2f}")

    balanced = abs(difference) < 0.001 and total_debits > 0

    if st.button(
        "Save manual journal",
        type="primary",
        disabled=not balanced,
        key="save_manual_journal"
    ):
        try:
            jid = create_journal(
                j_no.strip(),
                j_date,
                j_memo.strip() or "Manual journal entry",
                journal_lines
            )
            st.session_state["last_manual_journal_id"] = jid
            st.session_state["manual_journal_no_value"] = generate_unique_journal_no()
            st.session_state["journal_save_message"] = (
                f"Manual journal saved as ID {jid}. A fresh journal number is ready."
            )
            st.rerun()
        except ValueError as e:
            if "journal number" in str(e).lower():
                st.session_state["manual_journal_no_value"] = generate_unique_journal_no()
                st.warning(str(e))
            else:
                st.error(str(e))
        except Exception as e:
            st.error(f"Could not save journal: {e}")

    if st.session_state.get("journal_save_message"):
        st.success(st.session_state.pop("journal_save_message"))

    if st.button(
        "Save + post this journal to General Ledger",
        disabled=not balanced,
        key="save_and_post_manual_journal"
    ):
        try:
            jid = create_journal(
                j_no.strip(),
                j_date,
                j_memo.strip() or "Manual journal entry",
                journal_lines
            )
            created = post_journal(jid)

            # Verify from a fresh direct DB read.
            verification = read(
                """SELECT COUNT(*) AS rows,
                          COALESCE(SUM(debit),0) AS debits,
                          COALESCE(SUM(credit),0) AS credits
                   FROM journal_entries
                   WHERE source_type='Manual Journal' AND source_id=?""",
                (str(jid),)
            ).iloc[0]

            if int(verification["rows"]) != int(created):
                raise RuntimeError("Manual journal GL verification failed after posting.")

            if round(float(verification["debits"]) - float(verification["credits"]),2) != 0:
                raise RuntimeError("Manual journal posted out of balance.")

            st.session_state["manual_journal_no_value"] = generate_unique_journal_no()
            st.session_state["manual_post_message"] = (
                f"Journal {jid} saved and posted successfully. "
                f"{created} verified GL row(s) created."
            )
            st.rerun()
        except Exception as e:
            st.error(f"Could not save/post manual journal: {e}")

    if st.session_state.get("manual_post_message"):
        st.success(st.session_state.pop("manual_post_message"))

    journals = read("""SELECT m.id,m.journal_no,m.journal_date,m.memo,m.posted,m.reversal_of,
        COALESCE(SUM(l.debit),0) AS total_debit,
        COALESCE(SUM(l.credit),0) AS total_credit
        FROM manual_journals m
        LEFT JOIN manual_journal_lines l ON l.journal_id=m.id
        GROUP BY m.id
        ORDER BY m.id DESC""")

    if journals.empty:
        st.info("No manual journals have been created yet.")
    else:
        st.markdown("### Saved manual journals")
        st.dataframe(journals,use_container_width=True,hide_index=True)

        unposted = journals[journals.posted==0]
        if not unposted.empty:
            opts = {
                f"{int(r.id)}: {r.journal_no} — {r.memo} | ${float(r.total_debit):,.2f}":
                int(r.id)
                for _,r in unposted.iterrows()
            }
            selected = st.selectbox(
                "Journal to post",
                list(opts),
                key="manual_journal_to_post"
            )

            if st.button("Post selected journal to General Ledger", key="post_manual_journal"):
                try:
                    post_journal(opts[selected])
                    st.success("Manual journal posted to the General Ledger.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

with accountant_tabs[7]:
    st.subheader("Corrections / Reversals")
    st.warning("Posted accounting history is never deleted. Correct it with a reversal in an open period.")

    j = gl()

    if j.empty:
        st.info("Nothing posted yet.")
    else:
        originals = j[j.source_type != "Reversal"][["source_type","source_id","memo"]].drop_duplicates()

        reversed_source_ids = set(
            j.loc[j.source_type == "Reversal", "source_id"].astype(str).tolist()
        )

        available_rows = []
        for _, r in originals.iterrows():
            reversal_key = f"{r.source_type}:{r.source_id}"
            if reversal_key not in reversed_source_ids:
                available_rows.append(r)

        if available_rows:
            sources = pd.DataFrame(available_rows)
            opts = {
                f"{r.source_type} {r.source_id} — {r.memo}": (r.source_type, r.source_id)
                for _, r in sources.iterrows()
            }

            selected_source = st.selectbox("Posted source to reverse", list(opts))
            reversal_date = st.date_input("Reversal date", date.today(), key="revdate")
            reason = st.text_input("Reason for correction")

            if st.button("Create reversal", type="primary"):
                try:
                    created = reverse_source(
                        *opts[selected_source],
                        reversal_date,
                        reason
                    )
                    st.session_state["reversal_success"] = (
                        f"Reversal posted and verified. {created} GL row(s) created."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            st.info("There are no unreversed posted sources available.")

        if st.session_state.get("reversal_success"):
            st.success(st.session_state.pop("reversal_success"))

        reversal_rows = j[j.source_type == "Reversal"]
        if not reversal_rows.empty:
            st.markdown("### Reversal history")
            st.dataframe(
                reversal_rows[
                    ["id","date","memo","account","debit","credit","source_id","reversal_of","created_at"]
                ],
                use_container_width=True,
                hide_index=True
            )

with report_tabs[0]:
    st.subheader("Financial Reports")
    st.caption(
        "Reports are built directly from posted General Ledger entries. "
        "Draft/saved transactions and journals do not appear until they are posted."
    )

    gl_all = gl()

    if gl_all.empty:
        st.warning("The General Ledger is empty, so there is nothing to report.")
    else:
        valid_dates = pd.to_datetime(gl_all["date"], errors="coerce").dropna()
        default_start = valid_dates.min().date() if not valid_dates.empty else date(date.today().year,1,1)
        default_end = max(
            date.today(),
            valid_dates.max().date() if not valid_dates.empty else date.today()
        )

        c1,c2 = st.columns(2)
        start = c1.date_input("Start", default_start, key="rstart")
        end = c2.date_input("End", default_end, key="rend")

        if start > end:
            st.error("Report start date cannot be after end date.")
        else:
            tb,pnl,bs,net,bs_summary = statements(start,end)

            report = st.selectbox(
                "Report",
                ["Trial Balance","Income Statement","Balance Sheet","General Ledger"]
            )

            if report=="Trial Balance":
                if tb.empty:
                    st.info("No posted GL activity exists inside this report date range.")
                else:
                    st.dataframe(tb,use_container_width=True,hide_index=True)
                    st.metric(
                        "Trial-balance debit/credit difference",
                        f"${tb['Debits'].sum()-tb['Credits'].sum():,.2f}"
                    )

            elif report=="Income Statement":
                if pnl.empty:
                    st.info("No revenue or expense activity exists inside this report date range.")
                else:
                    st.dataframe(pnl,use_container_width=True,hide_index=True)
                    st.metric("Net income",f"${net:,.2f}")

            elif report=="Balance Sheet":
                st.caption(
                    f"Balance Sheet as of {end}. Current-period earnings from "
                    f"{start} through {end} are included in equity."
                )

                if bs.empty:
                    st.info("No balance-sheet activity exists through this date.")
                else:
                    st.dataframe(bs,use_container_width=True,hide_index=True)

                    b1,b2,b3 = st.columns(3)
                    b1.metric("Total assets", f"${bs_summary['total_assets']:,.2f}")
                    b2.metric("Total liabilities", f"${bs_summary['total_liabilities']:,.2f}")
                    b3.metric("Total equity", f"${bs_summary['total_equity']:,.2f}")

                    e1,e2 = st.columns(2)
                    e1.metric(
                        "Current-period net income in equity",
                        f"${bs_summary['current_period_net_income']:,.2f}"
                    )
                    e2.metric(
                        "Liabilities + equity",
                        f"${bs_summary['liabilities_plus_equity']:,.2f}"
                    )

                    if abs(bs_summary["difference"]) < 0.01:
                        st.success(
                            f"Balance Sheet balances. "
                            f"Assets − (Liabilities + Equity) = ${bs_summary['difference']:,.2f}"
                        )
                    else:
                        st.error(
                            f"Balance Sheet is out of balance by "
                            f"${bs_summary['difference']:,.2f}. "
                            "Do not close the period until this is resolved."
                        )

            else:
                j = gl_all[
                    (pd.to_datetime(gl_all.date)>=pd.to_datetime(start)) &
                    (pd.to_datetime(gl_all.date)<=pd.to_datetime(end))
                ]
                if j.empty:
                    st.info("No posted GL activity exists inside this report date range.")
                else:
                    st.dataframe(j,use_container_width=True,hide_index=True)

with tax_tabs[0]:
    st.subheader("Sales Tax Center")
    st.caption(
        "V10.6 stores tax treatment on individual transactions before they are posted. "
        "The tax summary below is then built from the posted General Ledger tax accounts."
    )

    if not (gstreg or qstreg):
        st.warning(
            "This business profile is not currently marked GST/HST or QST registered. "
            "For the Québec tax test, check both registration boxes in the sidebar and save the business profile."
        )

    st.markdown("### Apply tax treatment to a saved transaction")

    tx = trans()
    if tx.empty:
        st.info("No saved transactions are available.")
    else:
        posted_bank_ids = set()
        posted_df = read(
            """SELECT DISTINCT transaction_id
               FROM journal_entries
               WHERE source_type='Bank' AND transaction_id IS NOT NULL"""
        )
        if not posted_df.empty:
            posted_bank_ids = set(posted_df.transaction_id.dropna().astype(int).tolist())

        tax_work = tx[~tx.id.astype(int).isin(posted_bank_ids)].copy()

        if tax_work.empty:
            st.info(
                "Every saved bank transaction is already posted. "
                "Tax treatment must be applied before posting."
            )
        else:
            tx_opts = {
                f"{int(r.id)}: {r.date} | {r.description} | {float(r.amount):,.2f}":
                int(r.id)
                for _,r in tax_work.iterrows()
            }

            selected_label = st.selectbox(
                "Saved unposted transaction",
                list(tx_opts),
                key="tax_tx_select"
            )
            selected_id = tx_opts[selected_label]
            selected = tax_work[tax_work.id.astype(int)==selected_id].iloc[0]
            gross = abs(float(selected.amount))

            st.write(
                f"**Gross bank amount:** ${gross:,.2f}  "
                f"({'Money received' if float(selected.amount)>0 else 'Money paid'})"
            )

            treatment = st.selectbox(
                "Tax treatment",
                [
                    "Québec GST + QST included in bank amount",
                    "No tax / exempt / zero-rated",
                    "Manual tax amounts"
                ],
                key="tax_treatment"
            )

            business_pct = 100.0
            eligible = True
            manual_gst = 0.0
            manual_qst = 0.0

            if float(selected.amount) < 0:
                eligible = st.checkbox(
                    "Purchase tax is eligible for business ITC/ITR",
                    value=True,
                    key="tax_eligible_ui"
                )
                business_pct = st.slider(
                    "Eligible business-use %",
                    min_value=0,
                    max_value=100,
                    value=100,
                    key="tax_business_pct"
                )
            else:
                st.info(
                    "For a sale, GST/QST amounts will post to tax payable accounts."
                )

            if treatment == "Québec GST + QST included in bank amount":
                subtotal_preview, gst_preview, qst_preview, rounding_preview = split_quebec_tax_from_gross(gross)

                p1,p2,p3,p4 = st.columns(4)
                p1.metric("Pre-tax", f"${subtotal_preview:,.2f}")
                p2.metric("GST", f"${gst_preview:,.2f}")
                p3.metric("QST", f"${qst_preview:,.2f}")
                p4.metric("Gross", f"${gross:,.2f}")

                if abs(rounding_preview) >= 0.01:
                    st.caption(
                        f"Invoice-level rounding adjustment included: ${rounding_preview:,.2f}."
                    )

            elif treatment == "Manual tax amounts":
                m1,m2 = st.columns(2)
                manual_gst = m1.number_input(
                    "GST/HST amount",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                    format="%.2f",
                    key="manual_tax_gst"
                )
                manual_qst = m2.number_input(
                    "QST amount",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                    format="%.2f",
                    key="manual_tax_qst"
                )
                manual_subtotal = round(gross-manual_gst-manual_qst,2)
                st.write(f"Pre-tax amount after manual tax split: **${manual_subtotal:,.2f}**")

            if st.button("Save transaction tax treatment", type="primary"):
                try:
                    result = apply_tax_treatment(
                        selected_id,
                        treatment,
                        business_pct,
                        eligible,
                        manual_gst,
                        manual_qst
                    )
                    st.session_state["tax_save_msg"] = (
                        f"Tax treatment saved for transaction {selected_id}: "
                        f"pre-tax ${result['subtotal']:,.2f}, "
                        f"GST ${result['gst']:,.2f}, QST ${result['qst']:,.2f}."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.session_state.get("tax_save_msg"):
                st.success(st.session_state.pop("tax_save_msg"))

    st.divider()
    st.markdown("### Posted tax summary")

    c1,c2 = st.columns(2)
    ts = c1.date_input(
        "Tax period start",
        date(date.today().year,1,1),
        key="ts"
    )
    te = c2.date_input(
        "Tax period end",
        date.today(),
        key="te"
    )

    s = tax_summary(ts,te)

    a,b,c = st.columns(3)
    a.metric("GST/HST collected",f"${s['GST/HST collected']:,.2f}")
    b.metric("GST/HST ITCs",f"${s['GST/HST ITCs']:,.2f}")
    c.metric("GST/HST net payable",f"${s['GST/HST net payable']:,.2f}")

    d,e,f = st.columns(3)
    d.metric("QST collected",f"${s['QST collected']:,.2f}")
    e.metric("QST ITRs",f"${s['QST ITRs']:,.2f}")
    f.metric("QST net payable",f"${s['QST net payable']:,.2f}")

    st.caption(
        "Bookkeeping summary only. Taxability, registration, ITC/ITR eligibility, "
        "place-of-supply rules and filing treatment require appropriate tax review."
    )

with sales_tabs[0]:
    st.subheader("Customers")
    st.caption("Permanent customer profiles used by invoices, balances, and account history.")
    st.info("Saved customers live here in the Customers tab and become selectable in Invoices / AR.")

    customers_df = customer_profiles()

    st.markdown("### Customer directory")
    if customers_df.empty:
        st.info("No saved customers yet.")
    else:
        directory_cols=[c for c in ["name","email","phone","city","province_state","payment_terms_days","active"] if c in customers_df.columns]
        st.dataframe(customers_df[directory_cols],use_container_width=True,hide_index=True)

    if customers_df.empty:
        st.info("No customers yet.")
        customer_choice = None
    else:
        customer_choice = st.selectbox(
            "Customer profile",
            customers_df["name"].tolist(),
            key="customer_profile_select"
        )

    if customer_choice:
        row = customers_df[customers_df.name==customer_choice].iloc[0]
        default_name = row["name"]
        default_email = row.get("email","") or ""
        default_phone = row.get("phone","") or ""
        default_address1 = row.get("address1","") or ""
        default_address2 = row.get("address2","") or ""
        default_city = row.get("city","") or ""
        default_region = row.get("province_state","") or ""
        default_postal = row.get("postal_zip","") or ""
        default_country = row.get("country","") or "Canada"
        default_terms = int(row.get("payment_terms_days",30) or 30)
        default_notes = row.get("notes","") or ""
        default_active = bool(int(row.get("active",1) or 0))
    else:
        default_name = ""
        default_email = default_phone = default_address1 = default_address2 = ""
        default_city = default_region = default_postal = default_notes = ""
        default_country = "Canada"
        default_terms = 30
        default_active = True

    st.markdown("### Add / update customer")
    c1,c2,c3=st.columns(3)
    cname=c1.text_input("Customer name",value=default_name,key="cust_name")
    cemail=c2.text_input("Email",value=default_email,key="cust_email")
    cphone=c3.text_input("Phone",value=default_phone,key="cust_phone")

    c4,c5=st.columns(2)
    caddr1=c4.text_input("Address line 1",value=default_address1,key="cust_addr1")
    caddr2=c5.text_input("Address line 2",value=default_address2,key="cust_addr2")

    c6,c7,c8,c9=st.columns(4)
    ccity=c6.text_input("City",value=default_city,key="cust_city")
    cregion=c7.text_input("Province / State",value=default_region,key="cust_region")
    cpostal=c8.text_input("Postal / ZIP",value=default_postal,key="cust_postal")
    ccountry=c9.text_input("Country",value=default_country,key="cust_country")

    c10,c11=st.columns(2)
    cterms=c10.number_input("Payment terms (days)",min_value=0,max_value=365,value=default_terms,key="cust_terms")
    cactive=c11.checkbox("Active customer",value=default_active,key="cust_active")
    cnotes=st.text_area("Customer notes",value=default_notes,key="cust_notes")

    if st.button("Save customer profile",type="primary",key="save_customer"):
        try:
            save_party_profile(
                "customer",cname,cemail,cphone,caddr1,caddr2,ccity,cregion,cpostal,
                ccountry,cterms,cnotes,cactive
            )
            st.session_state["customer_saved_message"] = (
                f"Customer '{cname}' saved in Customers. "
                "You can now select this customer in Invoices / AR."
            )
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if st.session_state.get("customer_saved_message"):
        st.success(st.session_state.pop("customer_saved_message"))

    if customer_choice:
        st.divider()
        st.metric("Outstanding customer balance",f"${party_balance('customer',customer_choice):,.2f}")
        inv_hist,pay_hist=customer_history(customer_choice)
        st.markdown("### Invoice history")
        if inv_hist.empty: st.info("No invoices for this customer.")
        else: st.dataframe(inv_hist,use_container_width=True,hide_index=True)
        st.markdown("### Payment history")
        if pay_hist.empty: st.info("No payments for this customer.")
        else: st.dataframe(pay_hist,use_container_width=True,hide_index=True)

with expense_tabs[0]:
    st.subheader("Vendors")
    st.caption("Permanent vendor profiles used by bills, balances, and payment history.")
    st.info("Saved vendors live here in the Vendors tab and become selectable in Bills / AP.")

    vendors_df = vendor_profiles()

    st.markdown("### Vendor directory")
    if vendors_df.empty:
        st.info("No saved vendors yet.")
    else:
        directory_cols=[c for c in ["name","email","phone","city","province_state","payment_terms_days","active"] if c in vendors_df.columns]
        st.dataframe(vendors_df[directory_cols],use_container_width=True,hide_index=True)

    if vendors_df.empty:
        st.info("No vendors yet.")
        vendor_choice = None
    else:
        vendor_choice = st.selectbox(
            "Vendor profile",
            vendors_df["name"].tolist(),
            key="vendor_profile_select"
        )

    if vendor_choice:
        row = vendors_df[vendors_df.name==vendor_choice].iloc[0]
        default_name = row["name"]
        default_email = row.get("email","") or ""
        default_phone = row.get("phone","") or ""
        default_address1 = row.get("address1","") or ""
        default_address2 = row.get("address2","") or ""
        default_city = row.get("city","") or ""
        default_region = row.get("province_state","") or ""
        default_postal = row.get("postal_zip","") or ""
        default_country = row.get("country","") or "Canada"
        default_terms = int(row.get("payment_terms_days",30) or 30)
        default_notes = row.get("notes","") or ""
        default_active = bool(int(row.get("active",1) or 0))
    else:
        default_name = ""
        default_email = default_phone = default_address1 = default_address2 = ""
        default_city = default_region = default_postal = default_notes = ""
        default_country = "Canada"
        default_terms = 30
        default_active = True

    st.markdown("### Add / update vendor")
    v1,v2,v3=st.columns(3)
    vname=v1.text_input("Vendor name",value=default_name,key="vend_name")
    vemail=v2.text_input("Email",value=default_email,key="vend_email")
    vphone=v3.text_input("Phone",value=default_phone,key="vend_phone")

    v4,v5=st.columns(2)
    vaddr1=v4.text_input("Address line 1",value=default_address1,key="vend_addr1")
    vaddr2=v5.text_input("Address line 2",value=default_address2,key="vend_addr2")

    v6,v7,v8,v9=st.columns(4)
    vcity=v6.text_input("City",value=default_city,key="vend_city")
    vregion=v7.text_input("Province / State",value=default_region,key="vend_region")
    vpostal=v8.text_input("Postal / ZIP",value=default_postal,key="vend_postal")
    vcountry=v9.text_input("Country",value=default_country,key="vend_country")

    v10,v11=st.columns(2)
    vterms=v10.number_input("Payment terms (days)",min_value=0,max_value=365,value=default_terms,key="vend_terms")
    vactive=v11.checkbox("Active vendor",value=default_active,key="vend_active")
    vnotes=st.text_area("Vendor notes",value=default_notes,key="vend_notes")

    if st.button("Save vendor profile",type="primary",key="save_vendor"):
        try:
            save_party_profile(
                "vendor",vname,vemail,vphone,vaddr1,vaddr2,vcity,vregion,vpostal,
                vcountry,vterms,vnotes,vactive
            )
            st.session_state["vendor_saved_message"] = (
                f"Vendor '{vname}' saved in Vendors. "
                "You can now select this vendor in Bills / AP."
            )
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if st.session_state.get("vendor_saved_message"):
        st.success(st.session_state.pop("vendor_saved_message"))

    if vendor_choice:
        st.divider()
        st.metric("Outstanding vendor balance",f"${party_balance('vendor',vendor_choice):,.2f}")
        bill_hist,pay_hist=vendor_history(vendor_choice)
        st.markdown("### Bill history")
        if bill_hist.empty: st.info("No bills for this vendor.")
        else: st.dataframe(bill_hist,use_container_width=True,hide_index=True)
        st.markdown("### Payment history")
        if pay_hist.empty: st.info("No payments for this vendor.")
        else: st.dataframe(pay_hist,use_container_width=True,hide_index=True)

with sales_tabs[1]:
    st.subheader("Estimates / Quotes")
    customers=customer_profiles(active_only=True)
    if customers.empty:
        st.info("Create a customer first.")
    else:
        ec=st.selectbox("Customer",customers.name.tolist(),key="est_cust")
        d1,d2=st.columns(2); ed=d1.date_input("Estimate date",date.today(),key="est_date"); ex=d2.date_input("Expiry date",date.today()+timedelta(days=30),key="est_exp")
        n=st.number_input("Number of line items",1,10,1,key="est_n")
        lines=[]
        for i in range(int(n)):
            a,b,c,d=st.columns([4,1,1,1])
            desc=a.text_input("Description",key=f"est_desc_{i}")
            qty=b.number_input("Qty",min_value=0.0,value=1.0,key=f"est_qty_{i}")
            rate=c.number_input("Rate",min_value=0.0,value=0.0,key=f"est_rate_{i}")
            taxable=d.checkbox("Tax",value=True,key=f"est_tax_{i}")
            lines.append({"description":desc,"quantity":qty,"rate":rate,"taxable":taxable})
        notes=st.text_area("Estimate notes",key="est_notes")
        est_gst_rate = 0.05 if gstreg else 0.0
        est_qst_rate = 0.09975 if qstreg else 0.0
        sub,gst,qst,total=calc_lines(lines,est_gst_rate,est_qst_rate)
        st.write(f"**Subtotal ${sub:,.2f} | GST ${gst:,.2f} | QST ${qst:,.2f} | Total ${total:,.2f}**")
        if st.button("Create estimate",type="primary"):
            try:create_estimate(ec,ed,ex,notes,lines,est_gst_rate,est_qst_rate);st.rerun()
            except Exception as e:st.error(str(e))
    est=read("SELECT * FROM estimates ORDER BY id DESC")
    if not est.empty:
        st.dataframe(est,use_container_width=True,hide_index=True)
        openest=est[est.converted_invoice_id.isna()]
        if not openest.empty:
            opts={f"{r.estimate_no} - {r.customer_name} - ${r.total:,.2f}":int(r.id) for _,r in openest.iterrows()}
            ch=st.selectbox("Estimate to convert",list(opts),key="est_convert")
            if st.button("Convert estimate to invoice"):
                try:convert_estimate_to_invoice(opts[ch]);st.rerun()
                except Exception as e:st.error(str(e))
        r=est.iloc[0]; l=read("SELECT description,quantity,rate,taxable FROM estimate_lines WHERE estimate_id=?",(int(r.id),))
        if not l.empty:
            pdf=simple_document_pdf("ESTIMATE",r.estimate_no,r.customer_name,r.estimate_date,r.expiry_date,l.to_dict("records"),r.subtotal,r.gst,r.qst,r.total,r.notes or "")
            st.download_button("Download latest estimate PDF",pdf,file_name=f"{r.estimate_no}.pdf",mime="application/pdf")

with sales_tabs[2]:
    st.subheader("Invoices")
    st.caption("Customers created in the Customers tab appear here automatically.")

    c1,c2,c3=st.columns(3)
    invoice_number_date = st.session_state.get("ar_idate", date.today())
    auto_invoice_no = next_document_number("invoice", invoice_number_date)
    cached_invoice_no = str(st.session_state.get("ar_ino","") or "")
    invoice_conflict = (not read("SELECT id FROM invoices WHERE invoice_no=?",(cached_invoice_no,)).empty) if cached_invoice_no else False
    invoice_number_signature = f"{pd.to_datetime(invoice_number_date).date().isoformat()}|{auto_invoice_no}"
    if (not cached_invoice_no) or invoice_conflict or st.session_state.get("ar_ino_signature") != invoice_number_signature:
        st.session_state["ar_ino"] = auto_invoice_no
        st.session_state["ar_ino_signature"] = invoice_number_signature
    ino=c1.text_input("Invoice number",key="ar_ino",disabled=True)
    active_customers = customer_profiles(active_only=True)
    if active_customers.empty:
        cust=c2.text_input("Customer",value="Test Customer",key="ar_cust")
        customer_terms = 30
    else:
        cust=c2.selectbox("Customer",active_customers["name"].tolist(),key="ar_cust_select")
        customer_row = active_customers[active_customers.name==cust].iloc[0]
        customer_terms = int(customer_row["payment_terms_days"] if pd.notna(customer_row["payment_terms_days"]) else 30)
        st.caption(f"Payment terms for {cust}: {customer_terms} day(s)")
    idate=c3.date_input("Invoice date",date.today(),key="ar_idate")
    correct_invoice_no = next_document_number("invoice", idate)
    if st.session_state.get("ar_ino") != correct_invoice_no:
        st.session_state["ar_ino"] = correct_invoice_no
        st.session_state["ar_ino_signature"] = f"{idate.isoformat()}|{correct_invoice_no}"
        st.rerun()

    c4,c5=st.columns(2)
    calculated_invoice_due = idate + timedelta(days=customer_terms)
    # Keep the due-date widget synchronized with the selected customer's terms
    # and the invoice date. This avoids Streamlit retaining an older 30-day value.
    due_signature = f"{cust}|{idate.isoformat()}|{customer_terms}"
    if st.session_state.get("ar_due_signature") != due_signature:
        st.session_state["ar_due"] = calculated_invoice_due
        st.session_state["ar_due_signature"] = due_signature
    idue=c4.date_input("Due date",key="ar_due")
    rev=[a for a in active_accounts() if account_meta(a)["type"]=="Revenue"]
    iacct=c5.selectbox("Revenue account",rev,key="ar_acct")

    st.markdown("### Invoice line items")
    inv_line_count=st.number_input("Number of invoice line items",1,20,1,key="ar_line_count")
    inv_lines=[]
    for i in range(int(inv_line_count)):
        lc1,lc2,lc3,lc4=st.columns([4,1,1,1])
        ldesc=lc1.text_input("Description",value="Service" if i==0 else "",key=f"ar_line_desc_{i}")
        lqty=lc2.number_input("Qty",min_value=0.0,value=1.0,key=f"ar_line_qty_{i}")
        lrate=lc3.number_input("Rate",min_value=0.0,value=1000.0 if i==0 else 0.0,step=10.0,key=f"ar_line_rate_{i}")
        ltax=lc4.checkbox("Tax",value=True,key=f"ar_line_tax_{i}")
        inv_lines.append({"description":ldesc,"quantity":lqty,"rate":lrate,"taxable":ltax})

    inv_gst_rate=0.05 if gstreg else 0.0
    inv_qst_rate=0.09975 if qstreg else 0.0
    isub,igst,iqst,inv_total=calc_lines(inv_lines,inv_gst_rate,inv_qst_rate)
    st.write(f"**Subtotal ${isub:,.2f} | GST ${igst:,.2f} | QST ${iqst:,.2f} | Total ${inv_total:,.2f}**")

    if st.button("Create invoice",type="primary",key="ar_create"):
        try:
            iid=create_invoice_record(ino,cust,idate,idue,iacct,isub,igst,iqst)
            def save_invoice_lines(c):
                for x in inv_lines:
                    lt=round(float(x["quantity"])*float(x["rate"]),2)
                    c.execute("""INSERT INTO invoice_lines(invoice_id,description,quantity,rate,taxable,line_total)
                                 VALUES(?,?,?,?,?,?)""",
                              (iid,x["description"],x["quantity"],x["rate"],1 if x.get("taxable",True) else 0,lt))
            write(save_invoice_lines)
            st.session_state.pop("ar_ino", None)
            st.session_state.pop("ar_ino_signature", None)
            st.rerun()
        except Exception as e:
            msg=str(e)
            if "Invoice number already exists" in msg:
                st.session_state.pop("ar_ino",None)
                st.session_state.pop("ar_ino_signature",None)
                st.error("That invoice number was just used. Sullivan will generate the next available number automatically.")
            else:
                st.error(msg)

    d=read(
        """SELECT i.*,
                  COALESCE((SELECT SUM(amount) FROM invoice_payments p WHERE p.invoice_id=i.id),0) AS paid,
                  COALESCE((SELECT SUM(amount) FROM credit_notes c WHERE c.invoice_id=i.id),0) AS credits
           FROM invoices i ORDER BY i.id DESC"""
    )

    if d.empty:
        st.info("No invoices yet.")
    else:
        d["remaining"]=(d["total"].astype(float)-d["paid"].astype(float)-d["credits"].astype(float)).round(2)
        st.dataframe(d,use_container_width=True,hide_index=True)

        unposted=d[d.posted==0]
        if not unposted.empty:
            opts={
                f"{int(r.id)}: {r.invoice_no} - {r.customer_name} | ${float(r.total):,.2f}":
                int(r.id) for _,r in unposted.iterrows()
            }
            ch=st.selectbox("Invoice to post",list(opts),key="ar_post_sel")
            if st.button("Approve & record invoice",key="ar_post"):
                try:
                    post_invoice_record(opts[ch]);st.rerun()
                except Exception as e:
                    st.error(str(e))

        open_inv=d[(d.posted==1)&(d["remaining"]>0.005)]
        if not open_inv.empty:
            st.markdown("### Record customer payment")

            opts={
                f"{int(r.id)}: {r.invoice_no} - {r.customer_name} | Remaining ${float(r.remaining):,.2f}":
                int(r.id) for _,r in open_inv.iterrows()
            }
            ch=st.selectbox("Open invoice",list(opts),key="ar_pay_sel")
            selected=open_inv[open_inv.id==opts[ch]].iloc[0]

            m1,m2,m3=st.columns(3)
            m1.metric("Invoice total",f"${float(selected.total):,.2f}")
            m2.metric("Paid to date",f"${float(selected.paid):,.2f}")
            m3.metric("Remaining",f"${float(selected.remaining):,.2f}")

            p1,p2=st.columns(2)
            pdate=p1.date_input("Payment date",date.today(),key="ar_pay_date")
            pamt=p2.number_input(
                "Payment amount",
                min_value=0.01,
                max_value=float(selected.remaining),
                value=min(float(selected.remaining),250.0),
                step=10.0,
                key="ar_pay_amount"
            )

            if st.button("Record invoice payment",key="ar_pay"):
                try:
                    result=pay_invoice_record(opts[ch],pdate,pamt)
                    st.success(
                        f"Payment recorded. Paid to date ${result['paid_to_date']:,.2f}; "
                        f"remaining ${result['remaining']:,.2f}."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.markdown("### Invoice payment history")
        ph=read(
            """SELECT p.id,p.invoice_id,i.invoice_no,i.customer_name,p.payment_date,p.amount,p.created_at
               FROM invoice_payments p
               JOIN invoices i ON i.id=p.invoice_id
               ORDER BY p.id DESC"""
        )
        if ph.empty:
            st.info("No invoice payments recorded yet.")
        else:
            st.dataframe(ph,use_container_width=True,hide_index=True)



    st.markdown("### Invoice PDF")
    latest=read("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    if not latest.empty:
        r=latest.iloc[0]
        lines=read("SELECT description,quantity,rate,taxable FROM invoice_lines WHERE invoice_id=?",(int(r.id),))
        if lines.empty:
            lines=pd.DataFrame([{"description":"Services","quantity":1.0,"rate":float(r.subtotal),"taxable":True}])
        pdf=simple_document_pdf("INVOICE",r.invoice_no,r.customer_name,r.invoice_date,r.due_date,lines.to_dict("records"),float(r.subtotal),float(r.gst),float(r.qst),float(r.total),"")
        st.download_button("Download latest invoice PDF",pdf,file_name=f"{r.invoice_no}.pdf",mime="application/pdf",key="invoice_pdf")

with sales_tabs[3]:
    st.subheader("Credit Notes / Refund Adjustments")
    inv=read("SELECT id,invoice_no,customer_name,total FROM invoices WHERE posted=1 ORDER BY id DESC")
    if inv.empty: st.info("No posted invoices.")
    else:
        opts={f"{r.invoice_no} - {r.customer_name} - ${r.total:,.2f}":int(r.id) for _,r in inv.iterrows()}
        ch=st.selectbox("Invoice",list(opts),key="credit_inv"); iid=opts[ch]
        row=inv[inv.id==iid].iloc[0]
        c1,c2=st.columns(2); cd=c1.date_input("Credit date",date.today()); amt=c2.number_input("Credit amount",min_value=.01,value=min(100.0,float(row.total)))
        reason=st.text_input("Reason",value="Customer credit")
        if st.button("Post credit note",type="primary"):
            try:create_credit_note(row.customer_name,iid,cd,amt,reason);st.rerun()
            except Exception as e:st.error(str(e))
    cr=read("SELECT * FROM credit_notes ORDER BY id DESC")
    if not cr.empty:st.dataframe(cr,use_container_width=True,hide_index=True)

with sales_tabs[4]:
    st.subheader("Recurring Invoices")
    cust=customer_profiles(active_only=True); rev=[a for a in active_accounts() if account_meta(a)["type"]=="Revenue"]
    if cust.empty:st.info("Create a customer first.")
    else:
        c1,c2=st.columns(2); rc=c1.selectbox("Customer",cust.name.tolist(),key="rec_cust"); freq=c2.selectbox("Frequency",["Monthly","Quarterly","Yearly"])
        desc=st.text_input("Description",value="Recurring service"); c3,c4=st.columns(2)
        amt=c3.number_input("Amount",min_value=0.0,value=100.0); nd=c4.date_input("Next invoice date",date.today())
        acct=st.selectbox("Revenue account",rev,key="rec_rev")
        rec_taxable=st.checkbox("Taxable recurring invoice",value=True,key="rec_taxable")
        if st.button("Save recurring invoice"):
            create_recurring_invoice(rc,desc,amt,freq,nd,acct,rec_taxable);st.rerun()
    if st.button("Generate invoices due today"):
        try:
            n=generate_due_recurring(date.today());st.success(f"Generated {n} invoice(s).");st.rerun()
        except Exception as e:st.error(str(e))
    rd=read("SELECT * FROM recurring_invoices ORDER BY id DESC")
    if not rd.empty:st.dataframe(rd,use_container_width=True,hide_index=True)

with expense_tabs[1]:
    st.subheader("Purchase Orders")
    vd=vendor_profiles(active_only=True)
    if vd.empty:st.info("Create a vendor first.")
    else:
        v=st.selectbox("Vendor",vd.name.tolist(),key="po_vendor"); c1,c2=st.columns(2)
        pod=c1.date_input("PO date",date.today()); exp=c2.date_input("Expected date",date.today()+timedelta(days=7))
        n=st.number_input("PO line items",1,10,1)
        lines=[]
        for i in range(int(n)):
            a,b,c,d=st.columns([4,1,1,1]);desc=a.text_input("Description",key=f"po_d_{i}")
            qty=b.number_input("Qty",min_value=0.0,value=1.0,key=f"po_q_{i}");rate=c.number_input("Rate",min_value=0.0,value=0.0,key=f"po_r_{i}")
            taxable=d.checkbox("Tax",True,key=f"po_t_{i}");lines.append({"description":desc,"quantity":qty,"rate":rate,"taxable":taxable})
        notes=st.text_area("PO notes")
        po_gst_rate = 0.05 if gstreg else 0.0
        po_qst_rate = 0.09975 if qstreg else 0.0
        sub,gst,qst,total=calc_lines(lines,po_gst_rate,po_qst_rate);st.write(f"**PO Total: ${total:,.2f}**")
        if st.button("Create purchase order",type="primary"):
            try:create_po(v,pod,exp,notes,lines,po_gst_rate,po_qst_rate);st.rerun()
            except Exception as e:st.error(str(e))
    po=read("SELECT * FROM purchase_orders ORDER BY id DESC")
    if not po.empty:
        st.dataframe(po,use_container_width=True,hide_index=True)
        op=po[po.converted_bill_id.isna()]
        if not op.empty:
            opts={f"{r.po_no} - {r.vendor_name} - ${r.total:,.2f}":int(r.id) for _,r in op.iterrows()}
            ch=st.selectbox("PO to convert",list(opts))
            if st.button("Convert PO to bill"):
                try:convert_po_to_bill(opts[ch]);st.rerun()
                except Exception as e:st.error(str(e))

with expense_tabs[2]:
    st.subheader("Bills")
    st.caption("Vendors created in the Vendors tab appear here automatically.")

    c1,c2,c3=st.columns(3)
    bill_number_date = st.session_state.get("ap_bdate", date.today())
    auto_bill_no = next_document_number("bill", bill_number_date)
    cached_bill_no = str(st.session_state.get("ap_bno","") or "")
    bill_conflict = (not read("SELECT id FROM bills WHERE bill_no=?",(cached_bill_no,)).empty) if cached_bill_no else False
    bill_number_signature = f"{pd.to_datetime(bill_number_date).date().isoformat()}|{auto_bill_no}"
    if (not cached_bill_no) or bill_conflict or st.session_state.get("ap_bno_signature") != bill_number_signature:
        st.session_state["ap_bno"] = auto_bill_no
        st.session_state["ap_bno_signature"] = bill_number_signature
    bno=c1.text_input("Bill number",key="ap_bno",disabled=True)
    active_vendors = vendor_profiles(active_only=True)
    if active_vendors.empty:
        vend=c2.text_input("Vendor",value="Test Electrical Supply",key="ap_vend")
        vendor_terms = 30
    else:
        vend=c2.selectbox("Vendor",active_vendors["name"].tolist(),key="ap_vend_select")
        vendor_row = active_vendors[active_vendors.name==vend].iloc[0]
        vendor_terms = int(vendor_row["payment_terms_days"] if pd.notna(vendor_row["payment_terms_days"]) else 30)
        st.caption(f"Payment terms for {vend}: {vendor_terms} day(s)")
    bdate=c3.date_input("Bill date",date.today(),key="ap_bdate")
    correct_bill_no = next_document_number("bill", bdate)
    if st.session_state.get("ap_bno") != correct_bill_no:
        st.session_state["ap_bno"] = correct_bill_no
        st.session_state["ap_bno_signature"] = f"{bdate.isoformat()}|{correct_bill_no}"
        st.rerun()

    c4,c5=st.columns(2)
    calculated_bill_due = bdate + timedelta(days=vendor_terms)
    bill_due_signature = f"{vend}|{bdate.isoformat()}|{vendor_terms}"
    if st.session_state.get("ap_due_signature") != bill_due_signature:
        st.session_state["ap_due"] = calculated_bill_due
        st.session_state["ap_due_signature"] = bill_due_signature
    bdue=c4.date_input("Due date",key="ap_due")
    exp=[a for a in active_accounts() if account_meta(a)["type"] in ["Expense","Asset"]]
    bacct=c5.selectbox("Expense / asset account",exp,key="ap_acct")

    c6,c7,c8=st.columns(3)
    bsub=c6.number_input("Subtotal",min_value=0.0,value=500.0,step=10.0,key="ap_sub")
    bgst=c7.number_input("GST/HST",min_value=0.0,value=0.0,step=0.01,key="ap_gst")
    bqst=c8.number_input("QST",min_value=0.0,value=0.0,step=0.01,key="ap_qst")
    st.write(f"**Bill total:** ${bsub+bgst+bqst:,.2f}")

    if st.button("Create bill",type="primary",key="ap_create"):
        try:
            create_bill_record(bno,vend,bdate,bdue,bacct,bsub,bgst,bqst)
            st.session_state.pop("ap_bno", None)
            st.session_state.pop("ap_bno_signature", None)
            st.rerun()
        except Exception as e:
            msg=str(e)
            if "Bill number already exists" in msg:
                st.session_state.pop("ap_bno",None)
                st.session_state.pop("ap_bno_signature",None)
                st.error("That bill number was just used. Sullivan will generate the next available number automatically.")
            else:
                st.error(msg)

    d=read(
        """SELECT b.*,
                  COALESCE((SELECT SUM(amount) FROM bill_payments p WHERE p.bill_id=b.id),0) AS paid
           FROM bills b ORDER BY b.id DESC"""
    )

    if d.empty:
        st.info("No bills yet.")
    else:
        d["remaining"]=(d["total"].astype(float)-d["paid"].astype(float)).round(2)
        st.dataframe(d,use_container_width=True,hide_index=True)

        unposted=d[d.posted==0]
        if not unposted.empty:
            opts={
                f"{int(r.id)}: {r.bill_no} - {r.vendor_name} | ${float(r.total):,.2f}":
                int(r.id) for _,r in unposted.iterrows()
            }
            ch=st.selectbox("Bill to post",list(opts),key="ap_post_sel")
            if st.button("Approve & record bill",key="ap_post"):
                try:
                    post_bill_record(opts[ch]);st.rerun()
                except Exception as e:
                    st.error(str(e))

        open_bills=d[(d.posted==1)&(d["remaining"]>0.005)]
        if not open_bills.empty:
            st.markdown("### Record vendor payment")

            opts={
                f"{int(r.id)}: {r.bill_no} - {r.vendor_name} | Remaining ${float(r.remaining):,.2f}":
                int(r.id) for _,r in open_bills.iterrows()
            }
            ch=st.selectbox("Open bill",list(opts),key="ap_pay_sel")
            selected=open_bills[open_bills.id==opts[ch]].iloc[0]

            m1,m2,m3=st.columns(3)
            m1.metric("Bill total",f"${float(selected.total):,.2f}")
            m2.metric("Paid to date",f"${float(selected.paid):,.2f}")
            m3.metric("Remaining",f"${float(selected.remaining):,.2f}")

            p1,p2=st.columns(2)
            pdate=p1.date_input("Payment date",date.today(),key="ap_pay_date")
            pamt=p2.number_input(
                "Payment amount",
                min_value=0.01,
                max_value=float(selected.remaining),
                value=min(float(selected.remaining),200.0),
                step=10.0,
                key="ap_pay_amount"
            )

            if st.button("Record bill payment",key="ap_pay"):
                try:
                    result=pay_bill_record(opts[ch],pdate,pamt)
                    st.success(
                        f"Payment recorded. Paid to date ${result['paid_to_date']:,.2f}; "
                        f"remaining ${result['remaining']:,.2f}."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.markdown("### Bill payment history")
        ph=read(
            """SELECT p.id,p.bill_id,b.bill_no,b.vendor_name,p.payment_date,p.amount,p.created_at
               FROM bill_payments p
               JOIN bills b ON b.id=p.bill_id
               ORDER BY p.id DESC"""
        )
        if ph.empty:
            st.info("No bill payments recorded yet.")
        else:
            st.dataframe(ph,use_container_width=True,hide_index=True)


with sales_tabs[5]:
    st.subheader("Customer Statements")
    cd=customer_profiles()
    if cd.empty:st.info("No customers.")
    else:
        c=st.selectbox("Customer",cd.name.tolist(),key="statement_customer")
        sd=customer_statement_df(c)
        st.metric("Outstanding balance",f"${0 if sd.empty else sd.balance.sum():,.2f}")
        if sd.empty:st.info("No posted invoice activity.")
        else:st.dataframe(sd,use_container_width=True,hide_index=True)

with report_tabs[1]:
    st.subheader("Money Owed / Bills Owed")
    asof=st.date_input("Aging as of",date.today(),key="aging")

    ar=aging("AR",asof)
    ap=aging("AP",asof)

    st.markdown("### AR Aging")
    if ar.empty:
        st.success("No outstanding receivables.")
    else:
        st.dataframe(ar,use_container_width=True,hide_index=True)
        st.metric("Total outstanding AR",f"${ar['outstanding'].sum():,.2f}")

    st.markdown("### AP Aging")
    if ap.empty:
        st.success("No outstanding payables.")
    else:
        st.dataframe(ap,use_container_width=True,hide_index=True)
        st.metric("Total outstanding AP",f"${ap['outstanding'].sum():,.2f}")


with banking_tabs[1]:
    st.subheader("Reconcile Bank")
    st.caption("Sullivan will guide you through this. You do not need to know accounting terminology.")

    st.info(
        "💡 **What reconciliation means:** compare Sullivan with your bank statement. "
        "You enter the statement balances, then mark only the transactions that actually appear on that statement. "
        "When the difference reaches $0.00, the statement matches Sullivan."
    )

    st.markdown("## Step 1 — Tell Sullivan what your bank statement says")
    st.write(
        "Open your bank statement (PDF, paper statement, or online statement). "
        "Copy the **opening balance**, **ending balance**, and **statement ending date** exactly as the bank shows them."
    )

    rc1,rc2=st.columns(2)
    statement_name=rc1.text_input(
        "Name this statement",
        value=f"Bank statement {date.today().strftime('%Y-%m')}",
        key="recon_statement_name",
        help="Example: RBC Business Chequing — August 2026"
    )
    statement_date=rc2.date_input(
        "Statement ending date",
        date.today(),
        key="recon_statement_date",
        help="Use the last date printed on the bank statement."
    )

    rb1,rb2=st.columns(2)
    opening_balance=rb1.number_input(
        "Opening balance shown by your bank",
        value=0.0,step=100.0,format="%.2f",
        key="recon_opening",
        help="Copy the opening/beginning balance from the bank statement."
    )
    ending_balance=rb2.number_input(
        "Ending balance shown by your bank",
        value=0.0,step=100.0,format="%.2f",
        key="recon_ending",
        help="Copy the ending/closing balance from the bank statement. Do not calculate this yourself."
    )

    st.caption("Important: these are BANK STATEMENT numbers — not numbers you are trying to make Sullivan equal.")

    if st.button("Start reconciliation →",type="primary",key="create_recon"):
        try:
            rid=create_bank_reconciliation(
                statement_name,statement_date,opening_balance,ending_balance
            )
            st.session_state["active_recon_id"]=rid
            st.success("Statement started. Now complete Step 2 below.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    recs=read(
        """SELECT id,statement_name,statement_date,opening_balance,ending_balance,status,
                  created_at,completed_at
           FROM bank_reconciliations
           ORDER BY id DESC"""
    )

    if recs.empty:
        st.info("No reconciliation started yet. Complete Step 1 above first.")
    else:
        with st.expander("Previous / open reconciliations", expanded=False):
            st.dataframe(recs,use_container_width=True,hide_index=True)

        opts={
            f"{int(r.id)}: {r.statement_name} | {r.statement_date} | {r.status}":
            int(r.id) for _,r in recs.iterrows()
        }
        default_index=0
        if st.session_state.get("active_recon_id") in opts.values():
            vals=list(opts.values())
            default_index=vals.index(st.session_state["active_recon_id"])

        selected_label=st.selectbox(
            "Statement you are working on",
            list(opts),
            index=default_index,
            key="recon_selected"
        )
        rid=opts[selected_label]
        st.session_state["active_recon_id"]=rid

        detail=reconciliation_detail(rid)
        summary=reconciliation_summary(rid)

        st.markdown("## Step 2 — Check what appears on your bank statement")
        st.write(
            "Go down your bank statement one transaction at a time. "
            "**Check a box only if that exact transaction appears on the bank statement.** "
            "If it is in Sullivan but not on the statement, leave it unchecked."
        )
        st.caption("Checked = I can see this transaction on my bank statement.  •  Unchecked = I cannot see it there.")

        if detail.empty:
            st.warning("Sullivan has no posted Bank transactions through this statement date.")
        else:
            for _,r in detail.iterrows():
                kind='Deposit' if float(r.bank_effect)>=0 else 'Payment'
                label=f"{r.date}  •  {r.memo}  •  {kind} ${abs(float(r.bank_effect)):,.2f}"
                current=bool(int(r.cleared))
                checked=st.checkbox(
                    label,
                    value=current,
                    key=f"recon_{rid}_{int(r.journal_entry_id)}",
                    help="Check this only if you can find the same transaction on the bank statement."
                )
                if checked != current:
                    set_reconciliation_item(rid,int(r.journal_entry_id),checked)
                    st.rerun()

        # Refresh after selections.
        summary=reconciliation_summary(rid)
        diff=abs(float(summary["statement_difference"]))

        st.markdown("## Step 3 — Does your bank match Sullivan?")
        if diff < 0.01:
            st.success("### ✅ Your bank statement matches Sullivan\n**Difference: $0.00** — you're ready to finish.")
        else:
            st.warning(
                f"### Almost there — difference remaining: ${diff:,.2f}\n"
                "Do **not** change your bank's ending balance just to make this zero. "
                "Go back through Step 2 and check for a missing, extra, or incorrectly selected transaction."
            )
            st.write(
                "**If you're stuck:** compare the amount above with the transactions in Step 2. "
                "A transaction for the same amount is often the one that was missed. "
                "Also confirm the opening and ending balances in Step 1 were copied exactly from the same bank statement."
            )

        c1,c2,c3=st.columns(3)
        c1.metric("Bank statement ending balance",f"${summary['ending_balance']:,.2f}")
        c2.metric("Sullivan matched balance",f"${summary['calculated_statement_balance']:,.2f}")
        c3.metric("Difference to fix",f"${diff:,.2f}")

        with st.expander("Accountant details (optional)"):
            st.caption("These calculations are kept for accountants and troubleshooting. Most owners can ignore them.")
            a1,a2,a3=st.columns(3)
            a1.metric("Opening balance",f"${summary['opening_balance']:,.2f}")
            a2.metric("Cleared net change",f"${summary['cleared_net_change']:,.2f}")
            a3.metric("Book bank balance",f"${summary['book_balance']:,.2f}")
            a4,a5,a6=st.columns(3)
            a4.metric("Deposits in transit",f"${summary['deposits_in_transit']:,.2f}")
            a5.metric("Outstanding payments",f"${summary['outstanding_payments']:,.2f}")
            a6.metric("Book reconciliation difference",f"${summary['book_difference']:,.2f}")

        if summary["status"]!="Reconciled":
            ready=(abs(summary["statement_difference"]) < 0.01 and abs(summary["book_difference"]) < 0.01)
            if ready:
                if st.button("✅ Finish reconciliation",type="primary",key="complete_recon"):
                    try:
                        complete_bank_reconciliation(rid)
                        st.success("Done — this bank statement is reconciled.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            else:
                st.button("Finish reconciliation",disabled=True,key="complete_recon_disabled")
                st.caption("This button unlocks automatically when Sullivan confirms the reconciliation is balanced.")
        else:
            st.success("✅ This bank statement has already been reconciled.")

with accountant_tabs[8]:
    st.subheader("Accounting Periods")
    st.caption("Closed periods block invoices, bills, payments, credits, journals, reversals, and conversions dated inside the closed range.")
    c1,c2=st.columns(2); ps=c1.date_input("Period start",date.today().replace(day=1),key="period_start"); pe=c2.date_input("Period end",date.today(),key="period_end")
    if st.button("Close accounting period",type="primary"):
        try:close_period(ps,pe);st.rerun()
        except Exception as e:st.error(str(e))
    pdx=read("SELECT * FROM accounting_periods ORDER BY period_start DESC")
    if not pdx.empty:
        st.dataframe(pdx,use_container_width=True,hide_index=True)
        closed=pdx[pdx.status=="Closed"]
        if not closed.empty:
            opts={f"{r.period_start} to {r.period_end}":int(r.id) for _,r in closed.iterrows()}
            ch=st.selectbox("Closed period to reopen",list(opts))
            if st.button("Reopen selected period"):reopen_period(opts[ch]);st.rerun()

with accountant_tabs[9]:
    st.subheader("Smart Close")
    pe=st.date_input("Period end",date.today().replace(day=1)-timedelta(days=1),key="pe");checks,stats=close_checks(pe)
    for label,ok in checks.items():
        (st.success if ok else st.error)(("✓ " if ok else "✗ ")+label)
    st.write(stats)
    sign=st.checkbox("I reviewed outstanding AR/AP and supporting documents.")
    if st.button("Lock period",disabled=not(all(checks.values()) and sign)):
        try:lock_period(pe,checks);st.success(f"Period through {pe} locked.")
        except Exception as e:st.error(str(e))

with accountant_tabs[10]:
    st.subheader("Accounting Integrity Center");issues=integrity()
    if not issues:st.success("No integrity issues detected.")
    else:
        for x in issues:st.error(x)
    st.caption("V10 checks GL balance, invalid journal rows, duplicate fingerprints, and unresolved/uncategorized transactions.")

with accountant_tabs[11]:
    st.subheader("Audit Trail");a=audits()
    if a.empty:st.info("No audit events.")
    else:st.dataframe(a,use_container_width=True,hide_index=True)

with accountant_tabs[12]:
    st.subheader("Accountant / Year-End Export")
    st.caption("Exports ledger data, reports, audit history, counterparties, invoices, bills, document index and stored supporting documents.")
    if st.button("Build accountant package",type="primary"):
        path=export_package()
        with open(path,"rb") as f:st.download_button("Download package",f.read(),"sullivan_v15_4_accountant_package.zip","application/zip")

st.divider()
st.caption("Sullivan V19 globally enforces closed accounting periods while retaining V12.3 automatic document numbering.")