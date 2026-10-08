#!/usr/bin/env python3
"""Build the site page from src/index.html.

    python3 tools/finalize.py                              # writes index.html
    python3 tools/finalize.py --artifact build/page.html   # also writes the claude.ai artifact form

src/index.html is the editable page: a <title>, styles, the markup and one inline script, with no
<html>/<head>/<body> wrapper. The output is made ASCII-only (non-ASCII characters in the script become
\\uXXXX escapes, elsewhere &#N; references) so it reads the same whatever charset a server declares.

index.html wraps the page in a complete document for GitHub Pages or any static host.
The artifact form is the bare page, which the claude.ai artifact service wraps itself.
"""
import argparse, os, re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESCRIPTION = ("Stock trades disclosed by members of Congress, the President and senior executive-branch officials, "
               "rebuilt from the public filings every day.")
# a small icon: one bar bought (blue) above the line, one sold (orange) below
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230e1420'/%3E"
        "%3Crect x='7' y='7' width='7' height='9' rx='1.5' fill='%233987e5'/%3E%3Crect x='18' y='10' width='7' height='6' rx='1.5' fill='%233987e5'/%3E"
        "%3Crect x='7' y='17' width='7' height='5' rx='1.5' fill='%23eb6834'/%3E%3Crect x='18' y='17' width='7' height='9' rx='1.5' fill='%23eb6834'/%3E%3C/svg%3E")


def to_ascii(page):
    m = re.search(r"(<script>\n)(.*?)(</script>\s*)$", page, re.S)
    head, js = page[:m.start(2)], m.group(2)
    esc_js = "".join(c if ord(c) < 128 else ("\\u%04x" % ord(c) if ord(c) < 0x10000 else "".join(
        "\\u%04x" % u for u in [0xD800 + ((ord(c) - 0x10000) >> 10), 0xDC00 + ((ord(c) - 0x10000) & 0x3FF)])) for c in js)
    style = re.search(r"<style>(.*?)</style>", head, re.S)
    assert all(ord(c) < 128 for c in style.group(1)), "non-ASCII in CSS"
    out = "".join(c if ord(c) < 128 else "&#%d;" % ord(c) for c in head) + esc_js + m.group(3)
    assert all(ord(c) < 128 for c in out)
    return out


def full_document(page):
    """Move the <title>, font link and styles into <head>; everything from the page markup on goes in <body>."""
    k = page.index('<div class="page">')
    head, body = page[:k].strip(), page[k:].rstrip()
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n"
            "<meta name=\"description\" content=\"" + DESCRIPTION + "\">\n"
            "<meta name=\"color-scheme\" content=\"light dark\">\n"
            "<link rel=\"icon\" href=\"" + ICON + "\">\n"
            + head + "\n<style>body{margin:0}</style>\n</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(HERE, "src", "index.html"))
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--artifact", help="also write the bare page for the claude.ai artifact here")
    a = ap.parse_args()
    page = to_ascii(open(a.src, encoding="utf-8").read())
    with open(a.out, "w", encoding="ascii", newline="\n") as f:
        f.write(full_document(page))
    print(a.out, os.path.getsize(a.out), "bytes")
    if a.artifact:
        os.makedirs(os.path.dirname(os.path.abspath(a.artifact)), exist_ok=True)
        with open(a.artifact, "w", encoding="ascii", newline="\n") as f:
            f.write(page)
        print(a.artifact, os.path.getsize(a.artifact), "bytes")


if __name__ == "__main__":
    main()
