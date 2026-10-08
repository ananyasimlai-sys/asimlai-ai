#!/usr/bin/env python3
"""Build the data files for the Washington Trade Ledger site.

Standard library only.

    python3 -I build.py --work WORKDIR --out OUTDIR [--prev PREVIOUS_core.json] [--offline]

Downloads the public sources into WORKDIR (unless --offline, which reuses what is already
there), normalises them into one table of disclosed trades, checks the result and writes
OUTDIR/data/core.json, trades.json, prices.json plus OUTDIR/build-report.json.

Exit code 0: the output passed every hard check and is safe to publish.
Exit code 2: a hard check failed. Do not publish; read build-report.json.

Sources
  House        kadoa-org/congress-trading-monitor (House Clerk periodic reports, parsed)
  Senate       KasperSK-DK/senate-ptr-data (daily mirror of efdsearch.senate.gov)
  Executive    tbrown034/open-cabinet (OGE 278-T and annual-report transactions)
  Ro Khanna    kanetronv2/khanna-disclosure-explorer (his paper filings, transcribed, CC0)
  Roster       unitedstates/congress-legislators (names, party, state)
"""
import argparse, bisect, collections, csv, datetime, io, json, os, re, shutil, subprocess, sys, time

START = "2025-04-01"                      # first trade date / filing date kept
BASE = datetime.date(2020, 1, 1)          # day numbers in the output count from here
RAW = "https://raw.githubusercontent.com/"
KADOA_REPO = "https://github.com/kadoa-org/congress-trading-monitor"
URLS = {
    "senate.json": RAW + "KasperSK-DK/senate-ptr-data/HEAD/data/filings.json",
    "oc/all-transactions.csv": RAW + "tbrown034/open-cabinet/HEAD/public/data/all-transactions.csv",
    "oc/officials-index.json": RAW + "tbrown034/open-cabinet/HEAD/data/meta/officials-index.json",
    "oc/sec-company-tickers.json": RAW + "tbrown034/open-cabinet/HEAD/data/meta/sources/sec-company-tickers.json",
    "khanna.csv": RAW + "kanetronv2/khanna-disclosure-explorer/HEAD/data/normalized/transactions.csv",
    "legislators-current.json": RAW + "unitedstates/congress-legislators/gh-pages/legislators-current.json",
}
OC_OFFICIAL = RAW + "tbrown034/open-cabinet/HEAD/data/officials/%s.json"
KHANNA_BIOGUIDE = "K000389"
KHANNA_SITE = "https://www.rokhanna.money/"

# Absolute floors: a build below these is a broken source, not news. (--prev adds a relative check.)
FLOORS = {"house": 15000, "senate": 1900, "exec": 28000, "people": 170}

BANDS = [  # low, high (None = open ended), label
    (1, 1000, "$1 to $1,000"),
    (1001, 15000, "$1,001 to $15,000"),
    (15001, 50000, "$15,001 to $50,000"),
    (50001, 100000, "$50,001 to $100,000"),
    (100001, 250000, "$100,001 to $250,000"),
    (250001, 500000, "$250,001 to $500,000"),
    (500001, 1000000, "$500,001 to $1M"),
    (1000001, 5000000, "$1M to $5M"),
    (5000001, 25000000, "$5M to $25M"),
    (25000001, 50000000, "$25M to $50M"),
    (50000001, None, "Over $50M"),
    (1000001, None, "Over $1M"),
    (None, None, "Not stated"),
]
CLASSES = ["Stocks", "Funds & ETFs", "Bonds", "Options", "Crypto", "Other", "Cash funds"]
KINDS = ["Periodic report", "Periodic report (278-T)", "Annual report", "Termination report",
         "Paper report, transcribed", "Paper report, not transcribed"]
OWNERS = ["", "Self", "Spouse", "Joint", "Child"]
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
          "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
          "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
          "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
          "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
          "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
          "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
          "DC": "District of Columbia", "PR": "Puerto Rico", "GU": "Guam", "VI": "Virgin Islands", "AS": "American Samoa", "MP": "Northern Mariana Islands"}

WARN = []
def warn(msg):
    WARN.append(msg); print("WARNING:", msg, file=sys.stderr)

# ------------------------------------------------------------------ fetching
def fetch(url, dest, tries=3):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    err = ""
    for i in range(tries):
        r = subprocess.run(["curl", "-sS", "-L", "--fail", "--max-time", "300", "-o", dest + ".part", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=400)
        if r.returncode == 0 and os.path.exists(dest + ".part") and os.path.getsize(dest + ".part") > 0:
            os.replace(dest + ".part", dest); return
        err = r.stderr.decode("utf-8", "replace")[:200]
        time.sleep(3 * (i + 1))
    raise RuntimeError("download failed: %s (%s)" % (url, err))

def download_all(work):
    k = os.path.join(work, "kadoa")
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    last = ""
    for i in range(3):
        if os.path.islink(k): os.unlink(k)
        elif os.path.isdir(k): shutil.rmtree(k)
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", KADOA_REPO, k], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1500)
        if r.returncode == 0: break
        last = r.stderr.decode("utf-8", "replace")[:300]
        time.sleep(5 * (i + 1))
    else:
        raise RuntimeError("git clone failed: %s" % last)
    for rel, url in URLS.items():
        fetch(url, os.path.join(work, rel))
    idx = json.load(open(os.path.join(work, "oc/officials-index.json"), encoding="utf-8"))
    for o in idx["officials"]:
        fetch(OC_OFFICIAL % o["slug"], os.path.join(work, "oc/officials", o["slug"] + ".json"))

# ------------------------------------------------------------------ helpers
def ws(s): return re.sub(r"\s+", " ", (s or "").replace("&amp;", "&")).strip()
def dnum(s):
    return (datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10])) - BASE).days
def valid_date(s):
    if not s or not re.match(r"^\d{4}-\d{2}-\d{2}$", s): return False
    try: datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10])); return True
    except ValueError: return False
def nice_date(s):
    d = datetime.date.fromisoformat(s)
    return "%d %s %d" % (d.day, d.strftime("%B"), d.year)

def band_of(label, lo=None):
    t = (label or "").replace(",", "")
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if lo is None and nums: lo = nums[0]
    if lo is None: return 12
    lo = int(lo)
    if "over" in t.lower():
        return 10 if lo >= 50000000 else 11 if lo >= 1000000 else 12
    for i, (a, b, _) in enumerate(BANDS[:10]):
        if a <= lo <= b: return i
    return 10 if lo > 50000000 else 12

def side_of(t):
    """-> (side, part): side 0 bought, 1 sold, 2 exchanged or not stated; part 1 partial sale, 2 full sale."""
    t = (t or "").lower()
    if t.startswith("purchase") or t.startswith("buy"): return 0, 0
    if "sale" in t or t.startswith("sell") or t.startswith("sold"):
        return 1, (1 if "partial" in t else 2 if "full" in t else 0)
    return 2, 0

OWN = {"sp": 2, "spouse": 2, "jt": 3, "joint": 3, "dc": 4, "child": 4, "dependent child": 4, "self": 1}
def owner_of(o, blank=0):
    return OWN.get((o or "").strip().lower(), blank)

# ---- company-name keys (used to match the same company across differently worded filings)
STOP = set("INC INCORPORATED CORP CORPORATION CO COMPANY COMPANIES COS LTD LIMITED PLC CLASS CL A B C COM COMMON STOCK STK SHS SHARES SHARE SHRS SHR CMN ORD ORDINARY "
           "ADR ADS SPON SPONS SPONSORED THE HLDGS HLDG HOLDINGS HOLDING HLDGCO HOLDCO GROUP GRP DEL DE NV SA AG SE REIT NPV SER SERIES UNIT UNITS EACH "
           "REPRESENTING VOTING VTG NON OF AND LLC LP CLASSA CLASSB CLASSC PAIRED CTF DELA".split())
ABBR = {"INTL": "INTERNATIONAL", "INTERNATL": "INTERNATIONAL", "INTERNTNL": "INTERNATIONAL", "FINL": "FINANCIAL", "FIN": "FINANCE", "SVCS": "SERVICES", "SVC": "SERVICE",
        "TECH": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES", "TECHS": "TECHNOLOGIES", "MFG": "MANUFACTURING", "LABS": "LABORATORIES", "LABORATORY": "LABORATORIES",
        "PPTYS": "PROPERTIES", "PPTY": "PROPERTY", "SYS": "SYSTEMS", "RES": "RESOURCES", "ENTMT": "ENTERTAINMENT", "NATL": "NATIONAL", "NTNL": "NATIONAL", "NAT": "NATURAL",
        "BK": "BANK", "BKS": "BANKS", "BKG": "BANKING", "PHARMACEUTICALS": "PHARMA", "PHARMACEUTICAL": "PHARMA", "PHARM": "PHARMA", "PHARMACTLS": "PHARMA",
        "COMMUNICATIONS": "COMM", "COMMUNICATION": "COMM", "COMMUN": "COMM", "COMMUNS": "COMM", "MTRS": "MOTORS", "MTR": "MOTOR", "ELEC": "ELECTRIC", "ELECTRS": "ELECTRONICS",
        "INDS": "INDUSTRIES", "INDL": "INDUSTRIAL", "WKS": "WORKS", "WTR": "WATER", "RLTY": "REALTY", "INVT": "INVESTMENT", "INVTS": "INVESTMENTS", "INVS": "INVESTORS",
        "MGMT": "MANAGEMENT", "CTLS": "CONTROLS", "PRODS": "PRODUCTS", "MATLS": "MATERIALS", "AMER": "AMERICA", "AMERN": "AMERICAN", "GEN": "GENERAL", "WHSL": "WHOLESALE",
        "WHSE": "WAREHOUSE", "MACHS": "MACHINES", "SOLTNS": "SOLUTIONS", "HEALTHCARE": "HEALTH", "HLTH": "HEALTH", "HLTHCARE": "HEALTH", "CMNTYS": "COMMUNITIES",
        "CMNTY": "COMMUNITY", "APT": "APARTMENT", "DEV": "DEVELOPMENT", "UN": "UNION", "PAC": "PACIFIC", "SOUTHN": "SOUTHERN", "NORTHN": "NORTHERN", "WESTN": "WESTERN",
        "EASTN": "EASTERN", "CHEMS": "CHEMICALS", "CHEM": "CHEMICAL", "PWR": "POWER", "STS": "STATES", "AWYS": "AIRWAYS", "AIRLS": "AIRLINES", "PERS": "PERSONAL",
        "BLDG": "BUILDING", "BLDRS": "BUILDERS", "MED": "MEDICAL", "CALIF": "CALIFORNIA", "INS": "INSURANCE", "ASSUR": "ASSURANCE", "ASSOC": "ASSOCIATES",
        "BANCSHS": "BANCSHARES", "BRDS": "BRANDS", "CTR": "CENTER", "CTRS": "CENTERS", "ENGR": "ENGINEERING", "EXPL": "EXPLORATION", "FST": "FIRST", "HTLS": "HOTELS",
        "HMS": "HOMES", "INFRSTR": "INFRASTRUCTURE", "INFRA": "INFRASTRUCTURE", "INSTRS": "INSTRUMENTS", "INSTR": "INSTRUMENT", "MKTS": "MARKETS", "MKT": "MARKET",
        "PETE": "PETROLEUM", "PRTNRS": "PARTNERS", "RESH": "RESEARCH", "RY": "RAILWAY", "SEMICNDCTR": "SEMICONDUCTOR", "SEMICOND": "SEMICONDUCTOR", "STL": "STEEL",
        "TEL": "TELEPHONE", "TRANSN": "TRANSPORTATION", "UTD": "UNITED", "UTILS": "UTILITIES", "WLDWD": "WORLDWIDE", "TR": "TRUST", "SEC": "SECURITY",
        "ENTERPRISE": "ENTERPRISES", "ENTPR": "ENTERPRISES", "ENVIR": "ENVIRONMENTAL", "FINANCIALS": "FINANCIAL", "BANCORPORATION": "BANCORP", "SCIENCE": "SCIENCES",
        "CRP": "CORP", "NY": "NEW YORK", "STR": "STREET", "MTN": "MOUNTAIN", "BROS": "BROTHERS", "HBRS": "HARBORS", "MNG": "MINING", "CAP": "CAPITAL", "CTRY": "COUNTRY",
        "ED": "EDUCATION", "GOVT": "GOVERNMENT", "COMWLTH": "COMMONWEALTH", "COML": "COMMERCIAL", "REP": "REPUBLIC", "CTZNS": "CITIZENS", "MTG": "MORTGAGE", "RIV": "RIVER",
        "ALUM": "ALUMINUM", "CONSTR": "CONSTRUCTION", "RUBR": "RUBBER", "MLS": "MILLS", "NTHN": "NORTHERN", "STHN": "SOUTHERN", "CONS": "CONSOLIDATED", "SOFTWRE": "SOFTWARE",
        "ENTERPRSE": "ENTERPRISES", "FINCL": "FINANCIAL", "PROCESSNG": "PROCESSING", "INTST": "INTERSTATE", "ACCEP": "ACCEPTANCE", "MRKTS": "MARKETS", "WASH": "WA", "WIS": "WI", "MASS": "MA", "FLA": "FL", "OHIO": "OH", "TEX": "TX", "MICH": "MI", "MINN": "MN", "ILL": "IL",
        "TENN": "TN", "CONN": "CT", "ORE": "OR", "ARK": "AR", "ALA": "AL", "MISS": "MS", "KANS": "KS", "OKLA": "OK", "ARIZ": "AZ"}
LEGAL_TAIL = ("INC", "INCORPORATED", "CORP", "CORPORATION", "COMPANY", "GROUP", "HOLDINGS", "HLDGS", "LIMITED", "CLASS", "COMMON", "PLC", "REIT", "NEW", "INTERNATIONAL", "DEL")
TRAIL = {"NEW", "EQUITY", "EQU", "F", "CHG", "DISC", "FORMER", "FORMERLY"}
STATE_TAIL = {"PA", "WA", "WI", "MA", "FL", "MD", "OH", "NC", "SC", "PR", "VA", "GA", "TX", "MI", "MN", "IL", "TN", "CT", "OR", "AR", "AL", "MS", "MO", "KS", "OK", "AZ", "NJ",
              "KY", "LA", "ME", "VT", "NH", "RI", "WV", "ND", "SD", "NM", "DC", "HI", "ID", "IA", "UT", "CA", "NE", "MT", "WY", "AK", "IND"}
JUNK_TAIL = r"\b(UN)?SOLICITED\b.*$|\bDISCRETION EXERCISED\b.*$|\bAVERAGE UNIT PRICE\b.*$|\bEXEC ON MULT EXCHG\b.*$|\s-\sFRAC\.\d+.*$|\bMERGER ELECTION\b.*$"
_PHRASES = re.compile(JUNK_TAIL + r"|\bFORMERLY\b.*$|AMERICAN DEPOSIT[AO]RY (SHARES?|RECEIPTS?|SHS)|DEPOSIT[AO]RY (SHARES?|RECEIPTS?|SHS)|(UN)?SPONSORE?D ADR ?CMN|"
                      r"(UN)?SPONSORE?D ADR|\bEACH REP\w*( \d+)?( ORD(INARY)?)?( SHS?| SHARES?)?|PUBLIC LIMI\w*( COMPA\w*)?|PUBLIC LTD CO(MPANY)?|PUB LTD CO|CAP(ITAL)? ST(OC)?K|"
                      r"COMMON STOCK|ORDINARY SHARES?|SHS? (OF )?BEN(EFICIAL)? INT(EREST)?|\bCOM (USD|NPV|EUR)\S*( \d+)*|\bORD (USD|EUR).*$|\bISIN:? \S+|"
                      r"\b(USD|EUR)\d\S*( \d+)*|\bPAR \$?[\d.]+|\bPAR$|\bREGOFF\b.*$|\s\$[\d.]+$|\bWITH DUE BILLS\b.*$")
_core_cache = {}
def core(name):
    """Normalised company-name key, so that 'Home Depot, Inc.' and 'HOME DEPOT INC' are the same company."""
    if name in _core_cache: return _core_cache[name]
    s = name.upper().replace("&AMP;", "&")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = _PHRASES.sub(" ", s)
    s = re.sub(r"\s*/(THE|T)\b", " ", s)
    s = re.sub(r"\s*/([A-Z]{2})/?(?=\s|$)", r" \1 ", s)          # the SEC's state suffix: FIRST BANCORP /NC/
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"\b([A-Z]) & ([A-Z])\b", r"\1&\2", s)                # S & T BANCORP -> S&T BANCORP
    s = re.sub(r"(?<=\s)&(?=\S)", "& ", s); s = re.sub(r"(?<=\S)&(?=\s)", " &", s)   # MERCK &CO -> MERCK & CO
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = re.sub(r"\bMC ([A-Z]{3,})", r"MC\1", s)                      # MC DONALDS -> MCDONALDS
    s = re.sub(r"\b([A-Z]{3,}) S\b", r"\1S", s)                     # MCDONALD S -> MCDONALDS
    toks = [t for t in s.split() if t != "&"]
    if len(toks) >= 3 and toks[-1] == "F": toks.pop()               # a broker's "foreign" marker
    merged = []                                                      # J M SMUCKER -> JM SMUCKER, U S BANCORP -> US BANCORP
    for t in toks:
        one = len(t) == 1 and t.isalpha()
        if one and merged and merged[-1][1]: merged[-1] = (merged[-1][0] + t, True)
        else: merged.append((t, one))
    out = []
    for t, _ in merged:
        for u in ABBR.get(t, t).split():
            if u in STOP: continue
            if len(u) > 4 and u.endswith("S") and not u.endswith("SS"): u = u[:-1]
            out.append(u)
    while len(out) > 1 and (out[-1] in TRAIL or any(w.startswith(out[-1]) and w != out[-1] for w in LEGAL_TAIL)): out.pop()
    r = " ".join(out)
    _core_cache[name] = r
    return r
def bag(c): return " ".join(sorted(c.split()))

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
def clean_ticker(t):
    t = (t or "").strip().upper().replace("/", ".")
    if t in ("", "--", "N.A", "NA", "NONE", "N.A."): return None
    t = re.sub(r"^BRK[-.]?B$", "BRK.B", t); t = re.sub(r"^BRK[-.]?A$", "BRK.A", t); t = re.sub(r"^BF[-.]?B$", "BF.B", t)
    if t == "GOOG": t = "GOOGL"
    return t if TICKER_RE.match(t) else None
NOT_TICKERS = set(STATES) | {"THE", "NEW", "USA", "UK", "LP", "LLC", "ADR", "REIT", "ETF", "INC", "PS", "ST", "OT", "OP", "CS", "GS", "MF", "EF", "HN", "OI", "OL", "VA", "AB", "RS", "CT", "ET"}
MF_TICKER = re.compile(r"^[A-Z]{4}X$")

FUND_RE = re.compile(r"\bETFS?\b|\bETN\b|\bFUNDS?\b|\bFDS?\b|\bDFA\b|\bFID ADV\b|\bPORTF(OLIO)?\b|ISHARES|VANGUARD|\bSPDR\b|PROSHARES|DIREXION|YIELDMAX|WISDOMTREE (?!INC)|SELECT SECTOR|\bINDEX\b|\bM/F\b|"
                     r"INVESCO (QQQ|S&P|EXCHANGE|EXCH|PREMIER|NASDAQ)|SCHWAB (US|U\.S\.|INTL|INTERNATIONAL|EMERGING|FUNDAMENTAL|STRATEGIC|SHORT|1000|TOTAL)|DIMENSIONAL|AVANTIS|"
                     r"GLOBAL X|VANECK|FIRST TR(UST)? |JANUS HENDERSON|GRAYSCALE|\bBDC\b|\bQQQ\b|BONDBLOXX|\bNUVEEN\b|\bPIMCO\b|TAX[- ]EXEMPT|TAX[- ]ADV", re.I)
CASH_RE = re.compile(r"MONEY MARKET|MONEY FUND|MONEY MKT|\bMMKT\b|CASH RESERVES?|GOVERNMENT CASH|TREASURY (ONLY|CASH|OBLIGATIONS|PORTFOLIO)|FEDFUND|LIQUIDITY FUND|\bSWEEP\b|"
                     r"PRIME OBLIGATIONS|GOVT (MONEY|CASH)|CASH MANAGEMENT|GOVERNMENT (PORTFOLIO|OBLIGATIONS)|BANK DEPOSIT|DEPOSIT (PROGRAM|SWEEP)|INSURED DEPOSIT", re.I)
BOND_RE = re.compile(r"\d\s*%|%\s*DUE|\bDUE\s+\d|\bDue\s+[A-Z][a-z]{2}\s+\d|\bPERP\b|RATE/COUPON|MATURES|\bTREASUR|\bT-?BILLS?\b|\bMUNI|\bBONDS?\b|\bNOTES?\s+\d|\bMTN\s+(ZERO|\d)|"
                     r"\bDEBENTURE|\bGO\s+BDS?\b|\bREVS?\s+BDS?\b|\bCTFS?\s+(PARTN|OBLIG)|\bPREFERRED\b|\bPFD\b|\bZERO\s+CPN|\bTAX\s+REVS?\b|\bREV\s+RFDG|\bRFDG\b|BE/R|"
                     r"\bINDPT\s+SCH|\bSCH\s+DIST|\bUTIL\s+SYS|\bFIN\s+AUTH|\bPORT\s+AUTH|\bCNTYS?\b|\bARPT\b|\bGEN\s+OBLIG|\bSYS\s+REV\b|\bTSY\b", re.I)
STRONG_BOND_RE = re.compile(r"HYBRID PERPETUAL|\bPERP\b|\d\s*%|\bDUE\s+\d\d?/|BE/R|\bMTN\s+(ZERO|\d)|\bZERO\s+CPN|\bREVS?\s+BDS?\b|\bGO\s+BDS?\b|\bPREFERRED STOCK\b", re.I)
MUNI_RE = re.compile(r"\bAUTH(ORITY)?\b|\bSCH\s+DIST\b|\bINDPT\s+SCH\b|\bREVS?\s+BDS\b|\bCNTY\b|\bDED\s+TAX\b|\bGO\s+BDS?\b|\bCONSERVANCY\b", re.I)
STRUCT_RE = re.compile(r"STRUCTURED NOTE|AUTOCALLABLE|LINKED NOTE|LINKED TO|\bCONTIN(GENT)?\b|\bBUFFER(ED)?\b|\bTRIGGER\b|BARRIER|\bRILA\b|ANNUITY|\bEQTY ?DN\b", re.I)
OPTION_RE = re.compile(r"OPTION TYPE|\b(CALL|PUT)S?\s+OPTIONS?\b|\bOPTIONS?\s*(EXERCISE|STRIKE)|\b(STOCK|FUTURES|INDEX|EQUITY)\s+OPTIONS?\b|\bOPTIONS?\s*$|"
                       r"\bCALLS?\b.*\b(STRIKE|EXP|\$\d)|\bPUTS?\b.*\b(STRIKE|EXP|\$\d)|^(CALL|PUT)[\s/(]|\s(CALL|PUT)S?\s*$|\bWARRANTS?\b", re.I)
CRYPTO_RE = re.compile(r"\b(BITCOIN|ETHEREUM|ETHER|SOLANA|XRP|DOGECOIN|LITECOIN|CARDANO|POLKADOT|POLYGON|CRYPTOCURRENCY|STABLECOIN|USDC|CRYPTO)\b", re.I)
TREAS_RE = re.compile(r"^(US|U\.?\s?S\.?|UNITED STATES)\s+(GOVT\s+|GOVERNMENT\s+)?TREAS|^TREASURY\s+(BILL|NOTE|BOND)|^T-?BILLS?\b", re.I)
ETF_TICKERS = set("SPY IVV VOO VTI QQQ QQQM IWM DIA VEA VWO EFA EEM IEMG AGG BND TLT IEF IEI SHY GLD SLV IBIT FBTC ARKK XLF XLK XLE XLV XLY XLP XLI XLB XLU XLRE XLC "
                  "VIG VYM SCHD VNQ VUG VTV VGT RSP IJR IJH SPYM SPLG TQQQ SQQQ TNA TZA MGK VGK VXUS BIL SGOV JEPI JEPQ IGV SMH SOXX XBI IBB GDX USO GOVT IGOV BWX COMT "
                  "SCHP MBB CMBS JMBS GIGB GBIL XFIV XTWO TLH AGZ EIPI FTGC GEM LITP VNQI PALL AVUV TPYP SCHX SCHB SCHF SCHA SCHG SCHV SCHH VB VO VV VBR VOE VT BSV BIV VCIT "
                  "VCSH VTEB MUB SPMD SPSM SPEM SPDW SPTM SPYG SPYV IWF IWD IWB IWV IVW IVE EFV EFG ACWI ACWX IXUS ITOT IUSB USMV QUAL MTUM VLUE DGRO HDV DVY SDY NOBL".split())
BOND_TYPES = {"GS", "CS", "MUNICIPAL SECURITY", "CORPORATE BOND", "MUNICIPAL_BOND", "CORPORATE_NOTE", "TREASURY", "PREFERRED", "MUNICIPAL & GOV BONDS",
              "PREFERRED & HYBRID SECURITIES", "MUNICIPAL BOND", "GOVERNMENT SECURITY"}
OTHER_TYPES = {"PS", "OI", "OL", "HN", "VA", "AB", "NON-PUBLIC STOCK", "PRIVATE", "HEDGE FUNDS & PRIVATE FUNDS", "STRUCTURED NOTES", "REAL ESTATE", "ALTERNATIVES"}
FUND_TYPES = {"MF", "EF", "ET", "ETF", "MUTUAL_FUND", "FUNDS & ETFS"}
STOCK_TYPES = {"ST", "STOCK", "COMMON_STOCK", "COMMON STOCK", "EQUITIES", "RS"}
CASH_TYPES = {"CASH & DEPOSITS", "CASH", "MONEY MARKET"}

def classify(name, stype, ticker):
    """-> index into CLASSES."""
    st = (stype or "").strip().upper()
    up = name.upper()
    tk = clean_ticker(ticker)
    lp = re.search(r"\bLLC\b|\bLP\b|\bL\.P\.|\bLLP\b", up)
    if st in ("OP", "STOCK OPTION", "OPTIONS", "OPTION") or OPTION_RE.search(name): return 3
    if st in ("CT", "CRYPTOCURRENCY", "CRYPTO") or (CRYPTO_RE.search(name) and not re.search(r"\bETF\b|\bTRUST\b|\bFUND\b|\bINC\b|\bCORP\b", up)): return 4
    if st in CASH_TYPES or CASH_RE.search(name) or re.match(r"^[A-Z]{3}XX$", tk or ""): return 6
    etf = re.search(r"\bETFS?\b", up)
    mislabelled = not etf and (STRONG_BOND_RE.search(name) or MUNI_RE.search(name))     # a bond whose name contains "fund"
    if st in FUND_TYPES and not mislabelled: return 1
    if STRUCT_RE.search(name) and not etf: return 5
    if st in BOND_TYPES or (STRONG_BOND_RE.search(name) and not etf): return 2
    if (ticker or "").startswith("US-") or TREAS_RE.search(name): return 2
    if FUND_RE.search(name) and not lp and not mislabelled: return 1
    if re.search(r"\bTREASURY (BILL|NOTE|BOND)S?\b", up) or mislabelled: return 2
    if tk in ETF_TICKERS or MF_TICKER.match(tk or ""): return 1
    if st in STOCK_TYPES: return 0                   # includes listed partnerships and LLCs
    if st in OTHER_TYPES or lp: return 5
    if BOND_RE.search(name) and not tk: return 2
    if st in ("OT", "OTHER") and not tk: return 5
    return 0

ACR = {"IBM", "AT&T", "ETF", "USA", "US", "U.S.", "LLC", "LP", "REIT", "ADR", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "NV", "AG", "SA", "SE", "PLC", "S&P", "AMD",
       "KLA", "ASML", "HP", "GE", "CVS", "UPS", "HCA", "SAP", "AES", "DTE", "PNC", "RTX", "BP", "UBS", "HSBC", "TJX", "CSX", "KKR", "MSCI", "CME", "EOG", "NRG", "PG&E", "CBRE",
       "IQVIA", "TE", "API", "EQT", "QQQ", "TIPS", "MBS", "CMBS", "EAFE", "NYSE", "NASDAQ", "3M", "ITT", "AMC", "BNY", "KB", "CDW", "NVR", "PPG", "PPL", "WEC", "CMS", "FMC",
       "GATX", "ADT", "SLM", "CSG", "MKS", "IAC", "MPLX", "PJT", "SS&C", "BBH", "RCG", "MSD", "GS", "JPM", "NJ", "NY", "CA", "TX", "FL", "MTN", "SPDR", "BDC", "ADP", "AIG",
       "CF", "EMCOR", "EPAM", "FIS", "GXO", "IDEX", "IDEXX", "IPG", "JB", "KBR", "LKQ", "MGM", "NCR", "NOV", "ONEOK", "PTC", "RH", "RPM", "SBA", "TPG", "UGI",
       "URI", "USANA", "VF", "XPO", "DXC", "HF", "HNI", "MDU", "MGIC", "MSC", "NBT", "OGE", "PBF", "PVH", "QCR", "RLI", "SJW", "SM", "TTM", "UFP", "UMB", "WSFS"}
SMALL = {"OF", "AND", "THE", "FOR", "IN", "ON", "AT", "DE", "LA", "A", "AN", "TO"}
FIXCASE = {"At&t": "AT&T", "Jpmorgan": "JPMorgan", "Jp Morgan": "JPMorgan", "JP Morgan": "JPMorgan", "Pepsico": "PepsiCo", "QUALCOMM": "Qualcomm",
           "Nvidia": "NVIDIA", "Abbvie": "AbbVie", "Unitedhealth": "UnitedHealth", "Servicenow": "ServiceNow", "Doordash": "DoorDash", "Conocophillips": "ConocoPhillips",
           "Blackrock": "BlackRock", "Mcdonald": "McDonald", "Mckesson": "McKesson", "Mccormick": "McCormick", "Paypal": "PayPal", "Fedex": "FedEx", "Autozone": "AutoZone",
           "Ebay": "eBay", "Ishares": "iShares", "Proshares": "ProShares", "Wisdomtree": "WisdomTree", "Yieldmax": "YieldMax", "Crowdstrike": "CrowdStrike",
           "Coreweave": "CoreWeave", "Carmax": "CarMax", "Nextera": "NextEra", "Dupont": "DuPont", "Metlife": "MetLife", "Lyondellbasell": "LyondellBasell",
           "Astrazeneca": "AstraZeneca", "Draftkings": "DraftKings", "Applovin": "AppLovin", "Hubspot": "HubSpot", "Godaddy": "GoDaddy", "Mongodb": "MongoDB",
           "Microstrategy": "MicroStrategy"}
def pretty(name):
    """Tidy a security description from a filing into a display name."""
    n = ws(name)
    if re.match(r"^[A-Z]{2,6}$", n): return n
    if re.match(r"^[A-Z]{1,5}\s*-\s+[A-Z][a-z]", n): n = re.sub(r"^[A-Z]{1,5}\s*-\s+", "", n)
    n = re.sub(r"\s*Rate/Coupon:\s*([\d.]+)%\s*Matures:\s*(\d{4})-\d\d-\d\d.*$", r" \1% \2", n)
    n = re.sub(r"\s*Company:.*$", "", n)
    n = re.sub(r"\s*Option Type:\s*(\w+)\s*Strike price:\s*\$([\d.,]+)\s*Expires:\s*([\d/-]+).*$",
               lambda m: " %s option, $%s strike, expires %s" % (m.group(1).lower(), m.group(2).rstrip("0").rstrip(".") if "." in m.group(2) else m.group(2), m.group(3)), n)
    n = re.sub(r"\s+(AGENCY (SALE|PURCHASE)|AS AGENT|YTM\s*=).*$", "", n)
    n = re.sub(JUNK_TAIL, "", n, flags=re.I).strip()
    n = re.sub(r"\s*--\s*\(--\)\s*$", "", n)
    n = re.sub(r"\s*\((The|DE|Holding Company|Ireland|New|Delaware)\)", "", n, flags=re.I)
    n = re.sub(r"\s*\[[A-Z]{2}\]\s*$", "", n)
    n = re.sub(r"\s+-\s+(Common Stock.*|Class [A-C]\b.*|Ordinary Shares.*|American Depositary Shares.*|Capital Stock.*|Common Shares.*|Common Units.*)$", "", n, flags=re.I)
    n = re.sub(r"\s+(Common Stock.*|Common Shares.*|Common Units.*|Ordinary Shares.*|New Common Stock|American Depositary Shares.*|Capital Stock.*|Shares of Beneficial Interest.*)$", "", n, flags=re.I)
    n = re.sub(r"\s+(CMN|COM|COMMON|CAP STK|SHS)(\s+(CLASS|CL|SERIES)\s+[A-C])?$", "", n)
    n = re.sub(r"\s+(CLASS\s+)+[A-C]$", "", n); n = re.sub(r"\s+CL\s+[A-C]$", "", n); n = re.sub(r"\s+Class\s+[A-C]$", "", n)
    n = re.sub(r"\s+(CMN|COM|EQUITY|NEW|REIT REIT)$", "", n)
    n = re.sub(r"\s*\([A-Z.]{1,6}\)\s*$", "", n)
    n = n.strip(" ,-")
    letters = [c for c in n if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.85 and len(n) > 4:
        out = []
        for i, w in enumerate(n.split(" ")):
            b = w.strip(".,()")
            if b in ACR or re.match(r"^\d", b): out.append(w)
            elif b.lower() in ("inc", "corp", "co", "ltd", "plc"): out.append({"inc": "Inc", "corp": "Corp", "co": "Co", "ltd": "Ltd", "plc": "plc"}[b.lower()] + w[len(b):])
            elif i and b in SMALL: out.append(w.lower())
            elif "&" in b and len(b) <= 4: out.append(w)
            else: out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
        n = " ".join(out)
    for a, b in FIXCASE.items():
        if a in n: n = n.replace(a, b)
    return n[:110]

LEGAL_RE = re.compile(r"(?:,?\s+(?:Inc|Incorporated|Corp|Corporation|Co|Ltd|Limited|plc|N\.V|NV|S\.A|SA|AG|SE))\.?$", re.I)
def short_name(n):
    """Drop the legal form: 'Home Depot, Inc.' -> 'Home Depot'."""
    for _ in range(2):
        m = LEGAL_RE.search(n)
        if m and len(n[:m.start()].strip(" ,")) >= 2: n = n[:m.start()].strip(" ,")
    n = re.sub(r"\s+(?:&|and)\s+(?:Co|Company)\.?$", "", n, flags=re.I)
    n = re.sub(r"\s+\((?:The|Class [A-C])\)$", "", n)
    return re.sub(r"^The\s+", "", n).strip(" ,")

CRYPTO_NAMES = [(r"\b(BITCOIN|BTC)\b", "Bitcoin"), (r"\bETHEREUM CLASSIC\b", "Ethereum Classic"), (r"\b(ETHEREUM|ETHER|ETH)\b", "Ethereum"), (r"\b(SOLANA|SOL)\b", "Solana"),
                (r"\bXRP\b", "XRP"), (r"\b(DOGECOIN|DOGE)\b", "Dogecoin"), (r"\bLITECOIN\b", "Litecoin"), (r"\bUSDC\b", "USDC"), (r"\bPOLKADOT\b", "Polkadot"),
                (r"\bPOLYGON\b", "Polygon"), (r"\bCRO\b", "Cronos")]
def canonical(name, cls):
    """-> (display name to group under, detail worth keeping as a note) for securities described many different ways."""
    if cls == 2 and TREAS_RE.search(name):
        up = name.upper()
        kind = "bills" if "BILL" in up else "notes" if "NOTE" in up else "bonds" if "BOND" in up else "securities"
        return "US Treasury " + kind, pretty(name)
    if cls == 4:
        up = name.upper()
        for rx, nm in CRYPTO_NAMES:
            if re.search(rx, up): return nm, ""
    if cls == 3:
        p = pretty(name)
        return (p if re.search(r"option|call|put|warrant", p, re.I) else p + " (option)"), ""
    return None, ""

# ------------------------------------------------------------------ loaders
def load_members(work, here):
    """bioguide -> {name, party, state, district, type, end}. Bundled snapshot first, live roster on top."""
    members = {}
    snap = os.path.join(here, "members.json")
    if os.path.exists(snap):
        members.update(json.load(open(snap, encoding="utf-8")))
    try:
        for p in json.load(open(os.path.join(work, "legislators-current.json"), encoding="utf-8")):
            t = p["terms"][-1]; nm = p["name"]
            members[p["id"]["bioguide"]] = {"name": nm.get("official_full") or (nm["first"] + " " + nm["last"]), "party": t.get("party"), "state": t.get("state"),
                                            "district": t.get("district"), "type": t.get("type"), "end": t.get("end")}
    except Exception as e:  # roster unreadable: the snapshot still covers everyone known when it was made
        warn("live roster not used (%s); falling back to the bundled member snapshot" % type(e).__name__)
    return members

PARTY = {"Democrat": "D", "Republican": "R", "Independent": "I", "D": "D", "R": "R", "I": "I"}
def person_key(first, last):
    return re.sub(r"[^a-z]", "", (first or "").lower())[:3] + re.sub(r"[^a-z]", "", (last or "").lower())

def load_congress(work, members, today):
    """House rows, plus Senate person and ticker hints, from the Kadoa dataset."""
    D = os.path.join(work, "kadoa", "public", "data")
    filers = json.load(open(os.path.join(D, "filers.json"), encoding="utf-8"))
    people = {}; rows = []; sen_filing_person = {}; sen_hint = {}; oge_latest = {}
    for f in filers:
        fid = f["id"]
        path = os.path.join(D, "filer", fid + ".json")
        if not os.path.exists(path): continue
        if f["branch"] != "congress":
            w = ws(f["full_name"]).split() or [""]
            key = person_key(w[0], w[-1])
            for t in json.load(open(path, encoding="utf-8"))["trades"]:
                fd = t.get("filing_date")
                if valid_date(fd) and fd <= today and fd > oge_latest.get(key, ""): oge_latest[key] = fd
            continue
        m = re.search(r"/([A-Z]\d{6})\.jpg", f.get("photo_url") or "")
        bio = m.group(1) if m else None
        mem = members.get(bio) if bio else None
        chamber = f["chamber"]
        pid = ("H-" if chamber == "house" else "S-") + (bio or fid)
        if pid not in people:
            name = (mem or {}).get("name") or ws(f["full_name"]).rstrip(",")
            party = PARTY.get((mem or {}).get("party") or f.get("party") or "", None)
            state = (mem or {}).get("state") or f.get("state")
            same = bool(mem) and ((mem.get("type") == "rep") == (chamber == "house"))
            dist = mem.get("district") if same and chamber == "house" else None
            if chamber == "house":
                if dist is None:
                    mm = re.search(r"-(\d+)\b", f.get("office") or ""); dist = int(mm.group(1)) if mm else None
                role = "Representative · %s%s" % (state or "", ("-%02d" % dist) if dist else "-AL" if dist == 0 else "")
            else:
                role = "Senator · %s" % STATES.get(state, state or "")
            left = None
            if mem and mem.get("end") and mem["end"] < today and same: left = mem["end"]
            people[pid] = {"id": pid, "name": name, "group": chamber, "party": party, "state": state, "role": role, "left": left, "paper": 0, "note": ""}
        for t in json.load(open(path, encoding="utf-8"))["trades"]:
            td = t.get("transaction_date"); fd = t.get("filing_date")
            if chamber == "senate":
                gid = (t.get("filing_id") or "").replace("senate_", "")
                sen_filing_person[gid] = pid
                if t.get("ticker"): sen_hint[(gid, td, core(ws(t.get("asset_name"))))] = t["ticker"]
                continue
            if not valid_date(td): continue
            if not ((td >= START) or (valid_date(fd) and fd >= START)): continue
            num = (t.get("filing_id") or "").replace("house_", "")
            paper = len(num) == 7
            s, part = side_of(t.get("transaction_type"))
            url = t.get("doc_url") or ""
            if url and not url.endswith(".pdf"): url += ".pdf"
            name = ws(t.get("asset_name")); stype = t.get("asset_type")
            mm = re.search(r"\s*\[([A-Z]{2})\]\s*$", name)
            if mm:
                if not stype: stype = mm.group(1)
                name = name[:mm.start()].strip()
            rows.append({"pid": pid, "fid": t["filing_id"], "filed": fd if valid_date(fd) else None, "url": url, "kind": 4 if paper else 0,
                         "date": td, "side": s, "part": part, "band": band_of(t.get("amount_range_label"), t.get("amount_range_low")),
                         "owner": owner_of(t.get("owner"), 1), "name": name, "ticker": t.get("ticker"), "stype": stype,
                         "note": ws(t.get("comment")), "paper": paper, "src": "house"})
    return people, rows, sen_filing_person, sen_hint, oge_latest

def norm_last(s):
    s = re.sub(r"[^a-z ]", " ", (s or "").lower())
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv"))

def load_senate(work, people, sen_filing_person, sen_hint, members, today):
    j = json.load(open(os.path.join(work, "senate.json"), encoding="utf-8"))
    by_last = collections.defaultdict(list)
    def last_of(nm):
        w = norm_last(nm.replace(",", " ")).split()
        return w[-1] if w else ""
    for pid, p in people.items():
        if p["group"] == "senate": by_last[last_of(p["name"])].append(pid)
    for bio, m in members.items():   # senators who have never appeared in the Kadoa data
        if m.get("type") == "sen" and ("S-" + bio) not in people: by_last[last_of(m["name"])].append("S-" + bio)
    rows = []; paper = []; unmatched = collections.Counter()
    def name_of(pid): return (people.get(pid) or {"name": members.get(pid[2:], {}).get("name", "")})["name"]
    def person_for(f):
        pid = sen_filing_person.get(f["id"])
        if pid: return pid
        last = norm_last(f.get("last")).split()
        cands = by_last.get(last[-1] if last else "", [])
        first = (f.get("first") or "").strip().lower()[:1]
        if len(cands) > 1: cands = [c for c in cands if name_of(c).lower()[:1] == first] or cands
        if len(cands) > 1: cands = [c for c in cands if not ((members.get(c[2:]) or {}).get("end") or "9999") < START] or cands
        if not cands: return None
        pid = cands[0]
        if pid not in people:
            m = members[pid[2:]]
            people[pid] = {"id": pid, "name": m["name"], "group": "senate", "party": PARTY.get(m.get("party") or "", None), "state": m.get("state"),
                           "role": "Senator · %s" % STATES.get(m.get("state"), m.get("state") or ""), "left": m["end"] if m.get("end") and m["end"] < today else None, "paper": 0, "note": ""}
        return pid
    for f in j["filings"]:
        filed = f.get("filed")
        if not valid_date(filed): continue
        pid = person_for(f)
        if f.get("paper") or not f.get("transactions"):
            if f.get("paper") and filed >= START:
                if pid: paper.append({"pid": pid, "filed": filed, "url": f.get("url") or ""}); people[pid]["paper"] += 1
                else: unmatched[(f.get("first"), f.get("last"))] += 1
            continue
        if not pid:
            unmatched[(f.get("first"), f.get("last"))] += 1; continue
        for t in f["transactions"]:
            td = t.get("date")
            if not valid_date(td): continue
            if not (td >= START or filed >= START): continue
            name = ws(t.get("asset"))
            tk = clean_ticker(t.get("ticker")) or sen_hint.get((f["id"], td, core(name)))
            s, part = side_of(t.get("type"))
            note = ws(t.get("comment")); note = "" if note in ("--", "-", "N/A", "None") else note
            rows.append({"pid": pid, "fid": "senate_" + f["id"], "filed": filed, "url": f.get("url") or "", "kind": 0, "date": td, "side": s, "part": part,
                         "band": band_of(t.get("amount")), "owner": owner_of(t.get("owner"), 0), "name": name, "ticker": tk, "stype": t.get("asset_type"), "note": note,
                         "paper": False, "src": "senate"})
    if unmatched: warn("Senate filings with no matching senator: %s" % dict(unmatched))
    return rows, paper, j.get("generated_at", "")

EXEC_NAMES = {"kennedy-robert-f": "Robert F. Kennedy Jr.", "turner-eric-scott": "Scott Turner", "burgum-douglas-j": "Doug Burgum", "wright-christopher": "Chris Wright"}
AGENCY_ABBR = {"National Aeronautics and Space Administration": "NASA", "Environmental Protection Agency": "EPA"}
def exec_role(title, agency):
    t, a = ws(title), ws(agency)
    short = re.sub(r"^(Department of (the )?|Office of (the )?)", "", a)
    if not a or "president" in t.lower() or "attorney general" in t.lower() or (short and short.lower() in t.lower()): return t
    if re.match(r"^(Acting )?(Deputy )?Secretary$", t) and a.startswith("Department of "): return t + " of " + a[len("Department of "):]
    if t == "Administrator" and a in AGENCY_ABBR: return AGENCY_ABBR[a] + " Administrator"
    if "Federal Reserve" in a and "Federal Reserve" not in t: return t + ", Federal Reserve"
    if "," in t or re.search(r"\bof\b", t): return t
    return t + ", " + a
def exec_name(raw, slug):
    if slug in EXEC_NAMES: return EXEC_NAMES[slug]
    parts = [x.strip() for x in raw.split(",", 1)]
    n = ws((parts[1] + " " + parts[0]) if len(parts) == 2 else raw)
    return re.sub(r"\b([A-Z])(?=\s)", r"\1.", n)

def load_exec(work):
    idx = json.load(open(os.path.join(work, "oc/officials-index.json"), encoding="utf-8"))
    by_name = {o["name"]: o for o in idx["officials"]}
    filed = {}
    for o in idx["officials"]:
        p = os.path.join(work, "oc/officials", o["slug"] + ".json")
        if os.path.exists(p):
            for s in json.load(open(p, encoding="utf-8")).get("sourceFilings", []):
                d = (s.get("date") or "")[:10]
                if valid_date(d): filed[s["url"]] = d
    with open(os.path.join(work, "oc/all-transactions.csv"), newline="", encoding="utf-8") as fh:
        lines = [l for l in fh if not l.startswith("#")]
    people = {}; rows = []; nodate = 0; keys = {}
    for r in csv.DictReader(io.StringIO("".join(lines))):
        if r.get("historical_report") == "yes": continue
        td = r.get("date")
        if not valid_date(td): nodate += 1; continue
        url = r.get("source_filing_url") or ""
        fd = filed.get(url)
        if not fd:
            m = re.search(r"(\d{1,2})[._](\d{1,2})[._](20\d\d)", url)
            if m and valid_date("%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))): fd = "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
        if not (td >= START or (fd and fd >= START)): continue
        o = by_name.get(r["official_name"], {})
        slug = o.get("slug") or re.sub(r"[^a-z]+", "-", r["official_name"].lower()).strip("-")
        pid = "E-" + slug
        if pid not in people:
            people[pid] = {"id": pid, "name": exec_name(r["official_name"], slug), "group": "exec", "party": None, "state": None,
                           "role": exec_role(r.get("official_title"), r.get("agency")), "left": (r.get("departed_date") or "")[:10] or None, "paper": 0, "note": ""}
            parts = [x.strip() for x in r["official_name"].split(",", 1)]
            keys[person_key((parts[1].split() or [""])[0] if len(parts) == 2 else "", parts[0])] = pid
        sk = r.get("source_kind") or "278-T"
        kind = 1 if sk == "278-T" else 2 if sk.startswith("annual") else 3
        s, part = side_of(r.get("type"))
        rows.append({"pid": pid, "fid": "oge_" + url, "filed": fd, "url": url, "kind": kind, "date": td, "side": s, "part": part, "band": band_of(r.get("amount_range")),
                     "owner": 0, "name": ws(r.get("description")), "ticker": r.get("resolved_ticker") or r.get("ticker") or None, "stype": r.get("instrument_type"),
                     "note": "", "paper": False, "src": "exec"})
    if nodate: warn("%d executive rows skipped for a missing date" % nodate)
    return people, rows, idx.get("lastUpdated", ""), keys

def load_khanna(work, pid, kadoa_rows):
    """Ro Khanna files on paper. Use the Khanna Disclosure Explorer transcription up to its last date."""
    path = os.path.join(work, "khanna.csv")
    csv.field_size_limit(10 ** 9)
    out = []; skipped = 0; docs = collections.defaultdict(list)
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not re.match(r"^20(2[5-9]|3\d)$", r.get("year") or ""): continue
            td = r.get("transaction_date_iso")
            if not valid_date(td): skipped += 1; continue
            if td < START: continue
            docs[r["document_id"]].append(r)
    if not docs: return [], None, 0
    last = max(r["transaction_date_iso"] for rs in docs.values() for r in rs)
    # filing dates: borrow them from the Kadoa copy of the same paper report when most trade dates agree
    kfil = collections.defaultdict(list)
    for k in kadoa_rows:
        if k["pid"] == pid: kfil[k["fid"]].append(k)
    for doc, rs in docs.items():
        annual = len(set(r["transaction_date_iso"][:7] for r in rs)) > 3
        filed = None; url = KHANNA_SITE
        if not annual:
            dates = collections.Counter(r["transaction_date_iso"] for r in rs)
            best = None
            for fid, ks in kfil.items():
                kd = collections.Counter(k["date"] for k in ks)
                overlap = sum((dates & kd).values())
                if overlap >= 0.6 * len(ks) and (best is None or overlap > best[0]): best = (overlap, ks[0]["filed"], ks[0]["url"])
            if best: filed, url = best[1], best[2] or url
        for r in rs:
            s, part = side_of(r.get("transaction_type"))
            out.append({"pid": pid, "fid": "khanna_" + doc, "filed": filed, "url": url, "kind": 2 if annual else 4, "date": r["transaction_date_iso"], "side": s, "part": part,
                        "band": band_of(r.get("reported_amount"), int(r["amount_min_usd"]) if (r.get("amount_min_usd") or "").isdigit() else None),
                        "owner": owner_of(r.get("owner_code"), 0), "name": ws(r.get("asset_name")), "ticker": None,
                        "stype": "" if (r.get("asset_class") == "Cash & deposits" and not CASH_RE.search(r.get("asset_name") or "")) else r.get("asset_class"), "note": "",
                        "paper": True, "src": "khanna"})
    return out, last, skipped

# ------------------------------------------------------------------ assembling
def dedupe(rows):
    """Count a trade once when an amended report lists it again. Rows repeated inside one report are kept."""
    by_person = collections.defaultdict(list)
    for r in rows: by_person[r["pid"]].append(r)
    out = []; dropped = 0
    for pid, rs in by_person.items():
        fil = collections.OrderedDict()
        for r in sorted(rs, key=lambda r: (r["filed"] or "9999", r["fid"])): fil.setdefault(r["fid"], []).append(r)
        seen = collections.Counter()
        for fid, frs in fil.items():
            cnt = collections.Counter(); keep = collections.defaultdict(list)
            for r in frs:
                k = (r["date"], core(r["name"]) or r["name"].upper(), r["side"], r["part"], r["band"], r["owner"])
                cnt[k] += 1; keep[k].append(r)
            for k, c in cnt.items():
                new = max(0, c - seen[k])
                out.extend(keep[k][:new]); dropped += c - new
                seen[k] = max(seen[k], c)
    return out, dropped

class Resolver:
    """Company name -> ticker, learned from rows that carry a ticker and from reference lists."""
    def __init__(self):
        self.exact = collections.defaultdict(collections.Counter); self.bags = collections.defaultdict(collections.Counter)
        self.nostate = collections.defaultdict(collections.Counter); self.keys = []
    def add(self, name, ticker, weight):
        c = core(name)
        if len(c) < 2: return
        self.exact[c][ticker] += weight; self.bags[bag(c)][ticker] += weight
        w = c.split()
        if len(w) >= 3 and w[-1] in STATE_TAIL: self.nostate[" ".join(w[:-1])][ticker] += weight
    def pin(self, name, ticker):
        c = core(name)
        self.exact[c] = collections.Counter({ticker: 10 ** 6}); self.bags[bag(c)] = collections.Counter({ticker: 10 ** 6})
    def freeze(self): self.keys = sorted(self.exact)
    def lookup(self, name, prefix=True):
        c = core(name)
        if len(c) < 2: return None, ""
        if c in self.exact: return self.exact[c].most_common(1)[0][0], "exact"
        b = bag(c)
        if b in self.bags and len(c) >= 6: return self.bags[b].most_common(1)[0][0], "reordered"
        w = c.split()
        if len(w) >= 2 and w[-1] in STATE_TAIL and " ".join(w[:-1]) in self.exact:      # FULTON FINL CORP PA
            return self.exact[" ".join(w[:-1])].most_common(1)[0][0], "state"
        if c in self.nostate and len(self.nostate[c]) == 1: return next(iter(self.nostate[c])), "state"
        if prefix and len(c) >= 10 and " " in c:   # names cut short in some filings: accept an unambiguous prefix match
            i = bisect.bisect_left(self.keys, c); hits = set()
            while i < len(self.keys) and self.keys[i].startswith(c) and len(hits) < 3:
                hits.add(self.exact[self.keys[i]].most_common(1)[0][0]); i += 1
            if len(hits) == 1: return hits.pop(), "prefix"
        return None, ""

def build(work, out, here, today, prev=None, debug_rows=None):
    members = load_members(work, here)
    people, hrows, sen_fp, sen_hint, oge_latest = load_congress(work, members, today)
    kpid = "H-" + KHANNA_BIOGUIDE
    krows, klast, kskip = load_khanna(work, kpid, hrows) if kpid in people else ([], None, 0)
    if klast:
        before = len(hrows)
        hrows = [r for r in hrows if not (r["pid"] == kpid and r["date"] <= klast)]
        people[kpid]["note"] = ("Files on paper. Trades up to %s come from the Khanna Disclosure Explorer's transcription of his reports; later ones come from the Congress Trading Monitor."
                                % nice_date(klast))
        print("khanna: %d transcribed rows to %s, replaced %d Kadoa rows, %d undated rows skipped" % (len(krows), klast, before - len(hrows), kskip))
    srows, paper, sen_gen = load_senate(work, people, sen_fp, sen_hint, members, today)
    epeople, erows, oc_updated, ekeys = load_exec(work)
    people.update(epeople)

    groups = {"house": hrows + krows, "senate": srows, "exec": erows}
    rows = []; stats = {}
    for g, rs in groups.items():
        # annual-report rows were already matched against periodic reports upstream; only periodic reports can be amended
        d, dropped = dedupe([r for r in rs if r["kind"] != 2])
        d += [r for r in rs if r["kind"] == 2]
        fut = [r for r in d if r["date"] > today]
        d = [r for r in d if "2012-01-01" <= r["date"] <= today]
        for r in d:
            if r["filed"] and r["filed"] > today: r["filed"] = today
        stats[g] = {"rows": len(d), "amended_duplicates_removed": dropped, "future_dated_removed": len(fut)}
        rows += d
    for r in rows:
        if r["paper"] and r["pid"] != kpid and r["pid"].startswith("H-"):
            people[r["pid"]]["note"] = "Files on paper. Reports before March 2026 are missing from the source data and later ones may be incomplete."
    for pid, p in people.items():
        if p["paper"]:
            p["note"] = ("Files most reports on paper. %d paper report%s since April 2025 %s scanned and not yet transcribed, so those trades are missing here."
                         % (p["paper"], "" if p["paper"] == 1 else "s", "is" if p["paper"] == 1 else "are"))

    # ---- reference lists
    sec = {}
    sp = os.path.join(work, "oc/sec-company-tickers.json")
    if os.path.exists(sp):
        for v in json.load(open(sp, encoding="utf-8")).values():
            t = clean_ticker(v.get("ticker"))
            if t: sec.setdefault(t, v.get("title") or "")
    names_path = os.path.join(here, "names.json")
    NAMES = json.load(open(names_path, encoding="utf-8")) if os.path.exists(names_path) else {}
    for k in ("names", "sectors", "aliases"): NAMES.setdefault(k, {})
    universe = set(sec) | set(NAMES["names"])
    tdir = os.path.join(work, "kadoa", "public", "data", "ticker")
    if os.path.isdir(tdir): universe |= set(clean_ticker(f[:-5]) for f in os.listdir(tdir) if f.endswith(".json")) - {None}

    # ---- classify, and pick up tickers written inside the description
    for r in rows:
        if not clean_ticker(r["ticker"]):
            nm = r["name"]
            m = (re.search(r"\(([A-Z]{1,5})\)\s*$", nm) or re.match(r"^([A-Z]{2,5}) \(", nm) or re.search(r"\(symbol:\s*([A-Za-z]{1,5})\)", nm, re.I)
                 or re.match(r"^([A-Z]{2,5})\s*-\s*[A-Z][A-Za-z]", nm) or re.match(r"^([A-Z]{2,5})( ETF)?$", nm) or re.search(r"[A-Za-z]\s+-\s*([A-Z]{2,5})$", nm)
                 or re.search(r"(?:[a-z]|\bETF)\s+([A-Z]{3,5})$", nm))
            if m:
                c = m.group(1).upper()
                if c not in NOT_TICKERS and (c in universe or MF_TICKER.match(c)): r["ticker"] = c
        r["cls"] = classify(r["name"], r["stype"], r["ticker"])
        r["tk"] = clean_ticker(r["ticker"]) if r["cls"] in (0, 1) else None
    res = Resolver()
    for r in rows:
        if r["tk"]: res.add(r["name"], r["tk"], 3)
    for t, title in sec.items(): res.add(title, t, 1)
    for t, nm in NAMES["names"].items(): res.add(nm, t, 2)
    for nm, t in NAMES["aliases"].items(): res.pin(nm, t)
    res.freeze()
    how = collections.Counter(); cache = {}
    for r in rows:
        if r["tk"]: continue
        st = (r["stype"] or "").upper()
        relabel = r["src"] == "exec" and st in ("MUNICIPAL_BOND", "PREFERRED", "PRIVATE") and r["cls"] in (2, 5) and not re.search(r"\d", r["name"])
        if r["cls"] not in (0, 1) and not relabel: continue
        key = (r["name"], relabel, r["cls"])
        if key not in cache:   # funds share long family prefixes, so a cut-short fund name needs four words before it may match
            cache[key] = res.lookup(r["name"], prefix=not relabel and (r["cls"] == 0 or len(core(r["name"]).split()) >= 4))
        t, h = cache[key]
        if not t and r["src"] == "khanna" and r["cls"] == 0:
            for alt in ("I" + r["name"], "S" + r["name"] if r["name"].startswith("&") else "", r["name"][1:]):
                if alt and core(alt) in res.exact and len(core(alt)) >= 4:
                    t, h = res.exact[core(alt)].most_common(1)[0][0], "slip"; cache[key] = (t, h); break
        if not t or (relabel and h != "exact"): continue
        r["tk"] = t; r["how"] = h + (":relabelled" if relabel else ""); how[r["src"] + ":" + r["how"]] += 1
        if relabel: r["cls"] = 0
        if t in ETF_TICKERS or MF_TICKER.match(t): r["cls"] = 1
    for r in rows:
        r["disp"], keep = canonical(r["name"], r["cls"])
        if r["disp"] and keep and not r["note"] and keep.lower() != r["disp"].lower(): r["note"] = keep

    # ---- assets
    assets = {}; aname = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["tk"]: key = ("T", r["tk"])
        elif r["disp"]: key = ("D", r["cls"], r["disp"].upper())
        else: key = ("N", r["cls"], core(r["name"]) or r["name"].upper())
        r["akey"] = key
        aname[key][r["disp"] or r["name"]] += 1
        a = assets.setdefault(key, {"cls": collections.Counter(), "n": 0})
        a["cls"][r["cls"]] += 1; a["n"] += 1
    alist = []; aidx = {}
    for key, a in sorted(assets.items(), key=lambda kv: (-kv[1]["n"], str(kv[0]))):
        cls = a["cls"].most_common(1)[0][0]
        top = aname[key].most_common(1)[0][0]
        if key[0] == "T":
            t = key[1]
            nm = NAMES["names"].get(t)
            if not nm:
                proper = [n for n, _ in aname[key].most_common() if sum(1 for ch in n if ch.islower()) > 3]
                nm = pretty(proper[0] if proper else (sec.get(t) or top))
                if sec.get(t) and (len(nm) > 44 or not proper): nm = pretty(sec[t])
                if cls == 0: nm = short_name(nm)
            alist.append([nm, t, cls, NAMES["sectors"].get(t, "") if cls == 0 else ""])
        elif key[0] == "D":
            alist.append([top, None, cls, ""])
        else:
            alist.append([pretty(top), None, cls, ""])
        aidx[key] = len(alist) - 1

    # ---- people, filings, notes
    plist = sorted(people.values(), key=lambda p: (p["group"], p["name"]))
    used = set(r["pid"] for r in rows) | set(p["pid"] for p in paper)
    plist = [p for p in plist if p["id"] in used]
    pidx = {p["id"]: i for i, p in enumerate(plist)}
    filings = collections.OrderedDict()
    for r in rows:
        f = filings.setdefault(r["fid"], {"pid": r["pid"], "filed": r["filed"], "kind": r["kind"], "url": r["url"], "n": 0})
        f["n"] += 1
    for p in paper:
        filings["paper_" + p["url"]] = {"pid": p["pid"], "filed": p["filed"], "kind": 5, "url": p["url"], "n": 0}
    flist = []; fidx = {}
    for fid, f in filings.items():
        fidx[fid] = len(flist)
        flist.append([pidx[f["pid"]], dnum(f["filed"]) if f["filed"] else None, f["kind"], f["url"], f["n"]])
    notes = [""]; nidx = {"": 0}
    def note_i(s):
        s = s[:140]
        if s not in nidx: nidx[s] = len(notes); notes.append(s)
        return nidx[s]
    rows.sort(key=lambda r: (r["date"], r["filed"] or "", r["pid"], r["name"]), reverse=True)
    T = {"p": [], "a": [], "d": [], "f": [], "s": [], "b": [], "o": [], "n": []}
    for r in rows:
        T["p"].append(pidx[r["pid"]]); T["a"].append(aidx[r["akey"]]); T["d"].append(dnum(r["date"])); T["f"].append(fidx[r["fid"]])
        T["s"].append(r["side"] * 3 + r["part"]); T["b"].append(r["band"]); T["o"].append(r["owner"]); T["n"].append(note_i(r["note"]))

    # ---- weekly closing prices for every ticker traded at least twice (from the Kadoa dataset)
    tcount = collections.Counter(r["tk"] for r in rows if r["tk"] and r["cls"] in (0, 1))
    prices = {}
    pstart = (datetime.date.fromisoformat(START) - datetime.timedelta(days=35)).isoformat()
    for t, c in tcount.most_common():
        if c < 2: break
        fn = os.path.join(tdir, t + ".json")
        if not os.path.exists(fn): fn = os.path.join(tdir, t.replace(".", "-") + ".json")
        if not os.path.exists(fn): fn = os.path.join(tdir, t.replace(".", "") + ".json")
        if not os.path.exists(fn): continue
        try:
            h = json.load(open(fn, encoding="utf-8")).get("history") or []
        except Exception:
            continue
        pts = [[dnum(d), round(c2, 2)] for d, c2 in h if valid_date(d) and d >= pstart and isinstance(c2, (int, float)) and c2 > 0]
        if len(pts) >= 8: prices[t] = pts

    # ---- checks
    n_by = {g: stats[g]["rows"] for g in stats}
    errors = []
    for g in ("house", "senate", "exec"):
        if n_by[g] < FLOORS[g]: errors.append("%s: %d trades is below the floor of %d" % (g, n_by[g], FLOORS[g]))
    if len(plist) < FLOORS["people"]: errors.append("only %d people" % len(plist))
    latest = {}
    for g in ("house", "senate", "exec"):
        fd = [r["filed"] for r in rows if r["filed"] and r["src"] in ((g, "khanna") if g == "house" else (g,))]
        latest[g] = max(fd) if fd else None
        if not latest[g]: errors.append("no filing dates for %s" % g)
        else:
            age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(latest[g])).days
            if age > (60 if g == "exec" else 21): warn("%s: the newest filing is %d days old (%s)" % (g, age, latest[g]))
    behind = sorted((d, k) for k, d in oge_latest.items() if k in ekeys and latest.get("exec") and d > latest["exec"])
    if behind:
        who = sorted(set(people[ekeys[k]]["name"] for _, k in behind))
        warn("executive data is behind: another source already lists filings up to %s (%s); this build stops at %s" % (behind[-1][0], ", ".join(who), latest["exec"]))
    if prev:
        try:
            pc = json.load(open(prev, encoding="utf-8"))["meta"]
            for g in ("house", "senate", "exec"):
                was = pc["counts"].get(g, 0)
                if was and n_by[g] < 0.97 * was: errors.append("%s: %d trades, down more than 3%% from the published %d" % (g, n_by[g], was))
                pl = (pc.get("latest_filing") or {}).get(g)
                if pl and latest.get(g) and latest[g] < pl: warn("%s: the newest filing (%s) is older than the one already published (%s)" % (g, latest[g], pl))
            if pc["counts"].get("people") and len(plist) < 0.95 * pc["counts"]["people"]:
                errors.append("people: %d, down more than 5%% from the published %d" % (len(plist), pc["counts"]["people"]))
        except Exception as e:
            warn("could not compare with the previous build (%s: %s)" % (type(e).__name__, e))
    stock_rows = sum(1 for r in rows if r["cls"] == 0)
    unresolved = sum(1 for r in rows if r["cls"] == 0 and not r["tk"])
    meta = {
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "today": today, "start": START, "base": BASE.isoformat(),
        "counts": {"trades": len(rows), "people": len(plist), "assets": len(alist), "filings": len(flist), "house": n_by["house"], "senate": n_by["senate"], "exec": n_by["exec"],
                   "paper_not_transcribed": len(paper), "stock_rows": stock_rows, "stock_rows_without_ticker": unresolved, "priced_tickers": len(prices)},
        "latest_filing": latest, "stats": stats, "khanna_transcribed_to": klast,
        "sources": {"senate_mirror_generated": sen_gen, "executive_last_updated": oc_updated},
        "tickers_resolved_from_names": dict(how), "warnings": WARN, "errors": errors,
    }
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    core_json = {"meta": meta, "bands": [[a, b, c] for a, b, c in BANDS], "classes": CLASSES, "kinds": KINDS, "owners": OWNERS,
                 "people": [[p["name"], p["group"], p["party"], p["state"], p["role"], p["left"], p["note"], p["id"]] for p in plist],
                 "assets": alist, "filings": flist, "notes": notes}
    def dump(name, obj):
        p = os.path.join(out, "data", name)
        with open(p, "w", encoding="utf-8") as fh: json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        return os.path.getsize(p)
    sizes = {"core.json": dump("core.json", core_json), "trades.json": dump("trades.json", T), "prices.json": dump("prices.json", prices)}
    meta["sizes"] = sizes
    with open(os.path.join(out, "build-report.json"), "w", encoding="utf-8") as fh: json.dump(meta, fh, indent=1)
    if debug_rows:
        with open(debug_rows, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({k: (list(v) if isinstance(v, tuple) else v) for k, v in r.items()}, ensure_ascii=False) + "\n")
    return meta

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--prev", help="core.json of the build that is currently published, for a regression check")
    ap.add_argument("--offline", action="store_true", help="reuse the downloads already in --work")
    ap.add_argument("--today", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--debug-rows", help="also write every normalised row to this file, one JSON object per line")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(a.work, exist_ok=True)
    if not a.offline: download_all(a.work)
    meta = build(a.work, a.out, here, a.today, a.prev, a.debug_rows)
    print(json.dumps({k: meta[k] for k in ("built", "counts", "latest_filing", "tickers_resolved_from_names", "warnings", "errors", "sizes")}, indent=1))
    if meta["errors"]:
        print("BUILD FAILED: do not publish", file=sys.stderr); sys.exit(2)
    print("BUILD OK")

if __name__ == "__main__":
    main()
