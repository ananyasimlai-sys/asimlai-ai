#!/usr/bin/env python3
"""One-off helper: write names.json (display names and sectors for tickers, plus hand-made aliases).

    python3 -I make_names.py SP500_constituents.csv NASDAQ.json NYSE.json AMEX.json OUT.json

Sectors: S&P 500 members use their GICS sector (datasets/s-and-p-500-companies); other listed
companies use the Nasdaq screener's sector (rreichel3/US-Stock-Symbols), mapped to the same labels.
build.py works without this file; it only makes names tidier and adds the sector column.
"""
import csv, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as B

sp, nasdaq, nyse, amex, out = sys.argv[1:6]
GICS = {"Information Technology": "Technology", "Health Care": "Health care", "Financials": "Financials", "Consumer Discretionary": "Consumer discretionary",
        "Communication Services": "Communication", "Industrials": "Industrials", "Consumer Staples": "Consumer staples", "Energy": "Energy", "Utilities": "Utilities",
        "Real Estate": "Real estate", "Materials": "Materials"}
NDQ = {"Technology": "Technology", "Health Care": "Health care", "Finance": "Financials", "Consumer Discretionary": "Consumer discretionary",
       "Telecommunications": "Communication", "Industrials": "Industrials", "Consumer Staples": "Consumer staples", "Energy": "Energy", "Utilities": "Utilities",
       "Real Estate": "Real estate", "Basic Materials": "Materials"}
names, sectors = {}, {}
for path in (amex, nyse, nasdaq):
    for r in json.load(open(path, encoding="utf-8")):
        sym = (r.get("symbol") or "").strip()
        if "^" in sym: continue
        t = B.clean_ticker(sym)
        if not t: continue
        nm = B.short_name(B.pretty(re.sub(r"\s+(Class [A-C]|Series [A-C])\b.*$", "", B.ws(r.get("name") or ""))))
        if nm and t not in names: names[t] = nm
        s = NDQ.get((r.get("sector") or "").strip())
        if s and t not in sectors: sectors[t] = s
for r in csv.DictReader(open(sp, newline="", encoding="utf-8")):
    t = B.clean_ticker(r["Symbol"])
    if not t: continue
    names[t] = B.short_name(re.sub(r"\s*\((The|Class [A-C])\)", "", r["Security"]).strip())
    if r["GICS Sector"] in GICS: sectors[t] = GICS[r["GICS Sector"]]
# Hand corrections: common names for the most traded companies, and sectors the screener gets wrong
names.update({"GOOGL": "Alphabet", "META": "Meta Platforms", "BRK.B": "Berkshire Hathaway", "JPM": "JPMorgan Chase", "KO": "Coca-Cola", "DIS": "Walt Disney", "HD": "Home Depot",
              "PG": "Procter & Gamble", "LLY": "Eli Lilly", "MRK": "Merck", "T": "AT&T", "IBM": "IBM", "MMM": "3M", "HPQ": "HP", "GE": "GE Aerospace", "BA": "Boeing",
              "XOM": "ExxonMobil", "PEP": "PepsiCo", "MCD": "McDonald's", "LOW": "Lowe's", "UNH": "UnitedHealth Group", "TSM": "Taiwan Semiconductor (TSMC)",
              "SO": "Southern Company", "TRV": "Travelers", "HSY": "Hershey", "CLX": "Clorox", "EL": "Estée Lauder", "KR": "Kroger", "SCHW": "Charles Schwab",
              "GS": "Goldman Sachs", "MS": "Morgan Stanley", "BK": "BNY Mellon", "AXP": "American Express", "COF": "Capital One", "PGR": "Progressive", "ALL": "Allstate",
              "TMO": "Thermo Fisher Scientific", "BMY": "Bristol Myers Squibb", "JNJ": "Johnson & Johnson", "MO": "Altria", "PM": "Philip Morris International",
              "UPS": "UPS", "FDX": "FedEx", "CAT": "Caterpillar", "DE": "Deere", "HON": "Honeywell", "RTX": "RTX", "LMT": "Lockheed Martin", "NOC": "Northrop Grumman",
              "GD": "General Dynamics", "CMCSA": "Comcast", "VZ": "Verizon", "TMUS": "T-Mobile US", "NFLX": "Netflix", "CRM": "Salesforce", "ORCL": "Oracle",
              "CSCO": "Cisco", "INTC": "Intel", "AMD": "AMD", "QCOM": "Qualcomm", "TXN": "Texas Instruments", "AVGO": "Broadcom", "NVDA": "Nvidia", "MSFT": "Microsoft",
              "AAPL": "Apple", "AMZN": "Amazon", "TSLA": "Tesla", "V": "Visa", "MA": "Mastercard", "WMT": "Walmart", "COST": "Costco", "TGT": "Target", "NKE": "Nike",
              "SBUX": "Starbucks", "ABT": "Abbott Laboratories", "ABBV": "AbbVie", "PFE": "Pfizer", "AMGN": "Amgen", "GILD": "Gilead Sciences", "CVX": "Chevron",
              "COP": "ConocoPhillips", "BAC": "Bank of America", "WFC": "Wells Fargo", "C": "Citigroup", "BLK": "BlackRock", "BX": "Blackstone", "SPGI": "S&P Global",
              "ACN": "Accenture", "ADBE": "Adobe", "INTU": "Intuit", "NOW": "ServiceNow", "UBER": "Uber", "PLTR": "Palantir", "MU": "Micron Technology",
              "FISV": "Fiserv", "FI": "Fiserv", "BKNG": "Booking Holdings", "DASH": "DoorDash", "ZTS": "Zoetis", "ISRG": "Intuitive Surgical", "BSX": "Boston Scientific",
              "SYK": "Stryker", "DHR": "Danaher", "PAYX": "Paychex", "WDAY": "Workday", "PANW": "Palo Alto Networks", "DJT": "Trump Media & Technology Group"})
sectors.update({"V": "Financials", "MA": "Financials", "PYPL": "Financials", "BRK.B": "Financials", "BRK.A": "Financials", "DJT": "Communication", "SPOT": "Communication",
                "ARM": "Technology", "TSM": "Technology", "ASML": "Technology", "NVO": "Health care", "AZN": "Health care", "SHEL": "Energy", "BP": "Energy",
                "SAP": "Technology", "TM": "Consumer discretionary", "BABA": "Consumer discretionary", "SNY": "Health care", "NVS": "Health care", "UL": "Consumer staples",
                "DEO": "Consumer staples", "RIO": "Materials", "BHP": "Materials", "TTE": "Energy", "SONY": "Consumer discretionary"})
# Company names as written in some filings -> ticker, where automatic matching cannot work
aliases = {}
ap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.txt")
if os.path.exists(ap):
    for line in open(ap, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if "=" in line:
            nm, t = line.rsplit("=", 1)
            aliases[nm.strip()] = t.strip().upper()
json.dump({"names": names, "sectors": sectors, "aliases": aliases}, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
print(len(names), "names;", len(sectors), "sectors;", len(aliases), "aliases")
