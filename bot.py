# Copyright © Sabarna Barik
#
# This code is open-source for **educational and non-commercial purposes only**.
#
# You may:
# - Read, study, and learn from this code.
# - Modify or experiment with it for personal learning.
#
# You may NOT:
# - Claim this code as your own.
# - Use this code in commercial projects or for profit without written permission.
# - Distribute this code as your own work.
#
# If you use or adapt this code, you **must give credit** to the original author: Sabarna Barik
# For commercial use or special permissions, contact: sabarnabarik@gmail.com
#
# # Copyright © 2026 Sabarna Barik
# # Non-commercial use only. Credit required if used.

import os
import sys
import time
import json
import base64
import logging
import requests
import traceback

from datetime import datetime
from urllib.parse import urlparse

import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup


# ───────────────────────────────────────────────────────────────
# LOGGING
# ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot")


# ───────────────────────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error(f"Missing required environment variable: {name}")
        return ""
    return val


SPREADSHEET_ID = _require("SPREADSHEET_ID")
INPUT_SHEET_NAME = os.environ.get("INPUT_SHEET_NAME", "Sheet1").strip()
OUTPUT_SHEET_NAME = os.environ.get("OUTPUT_SHEET_NAME", "Posts").strip()
OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3-haiku").strip()
SITE_URL = os.environ.get("SITE_URL", "https://affiliate-blog-bot").strip()

MAX_PRODUCTS = 1
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

COL_PRODUCT_URL = 2  # B
COL_AFFILIATE_URL = 3  # C

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
}


# ───────────────────────────────────────────────────────────────
# GOOGLE AUTH
# ───────────────────────────────────────────────────────────────

def _decode_google_creds() -> dict:
    raw_b64 = _require("GOOGLE_CREDS_BASE64")
    if not raw_b64:
        return {}
    try:
        json_bytes = base64.b64decode(raw_b64)
        return json.loads(json_bytes)
    except Exception as e:
        log.error(f"Failed to decode GOOGLE_CREDS_BASE64: {e}")
        return {}


def _make_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = _decode_google_creds()
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


# ───────────────────────────────────────────────────────────────
# SHEETS
# ───────────────────────────────────────────────────────────────

def get_workbook():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID missing")
    client = _make_gspread_client()
    return client.open_by_key(SPREADSHEET_ID)


def get_input_sheet(workbook):
    try:
        return workbook.worksheet(INPUT_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(f"Input sheet '{INPUT_SHEET_NAME}' not found.")


def get_output_sheet(workbook):
    try:
        return workbook.worksheet(OUTPUT_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(f"Output sheet '{OUTPUT_SHEET_NAME}' not found.")


# ───────────────────────────────────────────────────────────────
# URL HELPERS
# ───────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("www."):
        url = "https://" + url
    return url


def is_valid_url(url: str) -> bool:
    url = normalize_url(url)
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


# ───────────────────────────────────────────────────────────────
# READ ROW 2 ONLY
# ───────────────────────────────────────────────────────────────

def read_row_2(sheet) -> dict | None:
    """
    Row 1 = header
    Row 2 = first data row
    Column B = product URL
    Column C = affiliate URL
    """
    row_number = 2
    values = sheet.row_values(row_number)

    if not values:
        log.info("Row 2 is empty.")
        return None

    product_url = normalize_url(values[COL_PRODUCT_URL - 1] if len(values) >= COL_PRODUCT_URL else "")
    affiliate_url = normalize_url(values[COL_AFFILIATE_URL - 1] if len(values) >= COL_AFFILIATE_URL else "")

    if not is_valid_url(product_url):
        log.warning(f"Row 2 has invalid product URL: {product_url!r}")
        return None

    if not affiliate_url:
        affiliate_url = product_url

    return {
        "row_number": row_number,
        "product_url": product_url,
        "affiliate_url": affiliate_url,
    }


# ───────────────────────────────────────────────────────────────
# SCRAPING
# ───────────────────────────────────────────────────────────────

def _fetch_with_retry(url: str, retries: int = 3) -> BeautifulSoup:
    if not is_valid_url(url):
        raise RuntimeError(f"Invalid URL: {url}")

    domain = urlparse(url).scheme + "://" + urlparse(url).netloc
    headers = {**SCRAPE_HEADERS, "Referer": domain}
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            log.info(f"Fetch attempt {attempt}/{retries}: {url[:120]}")
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            last_exc = e
            log.warning(f"Attempt {attempt} failed: {e}")
            time.sleep(2 * attempt)

    raise RuntimeError(f"All {retries} fetch attempts failed for {url}") from last_exc


def _txt(tag, attr=None) -> str:
    if tag is None:
        return ""
    if attr:
        return (tag.get(attr) or "").strip()
    return tag.get_text(" ", strip=True)


def _og(soup, prop: str) -> str:
    tag = soup.find("meta", property=prop)
    return (tag.get("content") or "").strip() if tag else ""


def scrape_product(url: str) -> dict:
    log.info(f"Scraping product URL: {url}")
    domain = urlparse(url).netloc.lower()
    soup = _fetch_with_retry(url)

    if "amazon" in domain:
        title = _txt(soup.select_one("#productTitle")) or _og(soup, "og:title")
        price = (
            _txt(soup.select_one(".a-price-whole"))
            or _txt(soup.select_one("#priceblock_ourprice"))
            or _txt(soup.select_one("#priceblock_dealprice"))
            or _txt(soup.select_one(".a-offscreen"))
        )
        bullets = soup.select("#feature-bullets ul li span.a-list-item")
        desc = "\n".join(b.get_text(" ", strip=True) for b in bullets if b.get_text(" ", strip=True))
        img_tag = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
        image = ""
        if img_tag:
            image = img_tag.get("data-old-hires") or img_tag.get("src") or ""
            if not image:
                dyn = img_tag.get("data-a-dynamic-image", "")
                if dyn.startswith("{"):
                    try:
                        image = list(json.loads(dyn).keys())[0]
                    except Exception:
                        image = ""

    elif "flipkart" in domain:
        title = (
            _txt(soup.select_one("span.B_NuCI"))
            or _txt(soup.select_one("h1._9E25nV"))
            or _txt(soup.select_one("h1.yhB1nd"))
            or _og(soup, "og:title")
        )
        price = (
            _txt(soup.select_one("div._30jeq3._16Jk6d"))
            or _txt(soup.select_one("div._30jeq3"))
            or _txt(soup.select_one("div.Nx9bqj"))
        )
        desc = "\n".join(d.get_text(" ", strip=True) for d in soup.select("div._1AN87F li"))
        img_tag = (
            soup.select_one("img._396cs4")
            or soup.select_one("img._2r_T1I")
            or soup.select_one("img.DByuf4")
        )
        image = img_tag.get("src", "") if img_tag else ""

    else:
        title = _og(soup, "og:title") or _txt(soup.find("h1")) or _txt(soup.find("title"))
        desc = (
            _og(soup, "og:description")
            or _txt(soup.find("meta", {"name": "description"}), "content")
            or _txt(soup.select_one('[itemprop="description"]'))
        )
        price = (
            _txt(soup.select_one('[itemprop="price"]'))
            or _txt(soup.select_one(".price"))
            or _txt(soup.select_one('[class*="price"]'))
        )
        image = _og(soup, "og:image")

    product = {
        "url": url,
        "title": (title or "Unknown Product").strip(),
        "desc": (desc or "No description available.").strip(),
        "price": (price or "Check website for price").strip(),
        "image": (image or "").strip(),
    }
    log.info(f"Scraped: {product['title']} | {product['price']}")
    return product


# ───────────────────────────────────────────────────────────────
# OPENROUTER
# ───────────────────────────────────────────────────────────────

def generate_blog(product: dict) -> dict:
    affiliate_url = product.get("affiliate_url") or product["url"]

    prompt = f"""
Write a useful, original affiliate blog post for ONE specific product.

STRICT RULES:
- Stay on this product only.
- Do not turn it into a generic store article.
- Do not invent specs, price, or claims.
- Use SEO, AEO, and GEO naturally.
- Keep the tone practical and India-friendly.
- Output ONLY inner-body HTML.

PRODUCT DATA:
Product name: {product['title']}
Price: {product['price']}
Description: {product['desc'][:900]}
Product page URL: {product['url']}
Affiliate URL: {affiliate_url}

Required structure:
<h1>
<h2>Introduction</h2>
<h2>Key Features</h2>
<h2>Pros and Cons</h2>
<h2>Who Should Buy</h2>
<h2>Comparison</h2>
<h2>FAQ</h2>
<h2>Conclusion</h2>

Use this CTA link 2 to 3 times:
<a href="{affiliate_url}" rel="nofollow sponsored" target="_blank">Buy Now</a>

Include 4 to 6 FAQ items inside <div class="faq-item"> blocks.

End with a short affiliate disclaimer in <em> tags.

After the HTML, output exactly:
---META---
BLOG_TITLE: short SEO title
META_DESC: concise meta description
FOCUS_KEYWORD: main keyword phrase
TAGS: tag1, tag2, tag3, tag4, tag5
""".strip()

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-OpenRouter-Title": "Affiliate Blog Bot",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
            "max_tokens": 2500,
        },
        timeout=120,
    )
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"]

    if "---META---" in raw:
        html_part, meta_part = raw.split("---META---", 1)
    else:
        html_part, meta_part = raw, ""

    def _meta(key: str, default: str) -> str:
        for line in meta_part.splitlines():
            line = line.strip()
            if line.upper().startswith(key.upper() + ":"):
                return line[len(key) + 1:].strip()
        return default

    tags_raw = _meta("TAGS", "review,affiliate,india")

    return {
        "html_content": html_part.strip(),
        "blog_title": _meta("BLOG_TITLE", product["title"][:60]),
        "meta_desc": _meta("META_DESC", f"Review of {product['title']}."),
        "focus_keyword": _meta("FOCUS_KEYWORD", product["title"]),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
    }


# ───────────────────────────────────────────────────────────────
# WRITE TO POSTS SHEET
# ───────────────────────────────────────────────────────────────

def append_to_posts(output_sheet, product: dict, blog: dict):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tags_str = ", ".join(blog["tags"])
    affiliate_url = product.get("affiliate_url") or product["url"]

    row = [
        timestamp,                 # A
        "PENDING",                 # B
        product["url"],            # C product_url
        affiliate_url,             # D affiliate_url
        product["title"],          # E product_title
        blog["blog_title"],        # F blog_title
        blog["html_content"],      # G html_content
        blog["meta_desc"],         # H meta_description
        blog["focus_keyword"],     # I focus_keyword
        tags_str,                  # J tags
        product["image"],          # K image_url
        "",                        # L wp_post_id
        "",                        # M wp_post_url
        "",                        # N published_at
    ]

    output_sheet.append_row(row, value_input_option="RAW")
    log.info(f"Saved blog to '{OUTPUT_SHEET_NAME}'")


# ───────────────────────────────────────────────────────────────
# DELETE ROW 2 ONLY AFTER SUCCESS
# ───────────────────────────────────────────────────────────────

def delete_row_2(sheet):
    try:
        sheet.delete_rows(2)
        log.info("Deleted row 2 from Sheet1")
    except Exception as e:
        log.warning(f"Could not delete row 2: {e}")


# ───────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────

def main():
    try:
        if not SPREADSHEET_ID or not OPENROUTER_API_KEY:
            log.error("Missing required secrets. Exiting safely.")
            return

        workbook = get_workbook()
        input_sheet = get_input_sheet(workbook)
        output_sheet = get_output_sheet(workbook)

        row_data = read_row_2(input_sheet)
        if row_data is None:
            log.info("No valid row 2 to process.")
            return

        product_url = row_data["product_url"]
        affiliate_url = row_data["affiliate_url"]

        try:
            product = scrape_product(product_url)
            product["affiliate_url"] = affiliate_url
        except Exception:
            log.error("Scraping failed:\n%s", traceback.format_exc())
            return

        try:
            blog = generate_blog(product)
        except Exception:
            log.error("Blog generation failed:\n%s", traceback.format_exc())
            return

        try:
            append_to_posts(output_sheet, product, blog)
        except Exception:
            log.error("Writing to Posts failed:\n%s", traceback.format_exc())
            return

        delete_row_2(input_sheet)

        log.info("Run complete: 1 blog created and row 2 deleted.")

    except Exception:
        log.error("Fatal error:\n%s", traceback.format_exc())
        return


if __name__ == "__main__":
    main()
