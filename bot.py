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
#
# License:
# This project is open-source for learning only.
# Commercial use is prohibited.
# Credit is required if you use any part of this code.
# Copyright © Sabarna Barik
# Non-commercial use only. Credit required if used.

import os
import sys
import time
import json
import base64
import logging
import smtplib
import traceback
import requests

from datetime import datetime
from urllib.parse import urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
        sys.exit(1)
    return val


SPREADSHEET_ID = _require("SPREADSHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "Posts")
OUTPUT_SHEET_NAME = os.environ.get("OUTPUT_SHEET_NAME", "BlogQueue")

OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
SITE_URL = os.environ.get("SITE_URL", "https://affiliate-blog-bot")

MAX_PRODUCTS = int(os.environ.get("MAX_PRODUCTS", "1"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", EMAIL_USER).strip()

SEND_EMAIL = bool(EMAIL_USER and EMAIL_PASS and NOTIFY_EMAIL)

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
# GOOGLE CREDS
# ───────────────────────────────────────────────────────────────

def _decode_google_creds() -> dict:
    raw_b64 = _require("GOOGLE_CREDS_BASE64")
    try:
        json_bytes = base64.b64decode(raw_b64)
        return json.loads(json_bytes)
    except Exception as e:
        log.error(f"Failed to decode GOOGLE_CREDS_BASE64: {e}")
        sys.exit(1)


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

def get_sheets():
    client = _make_gspread_client()
    workbook = client.open_by_key(SPREADSHEET_ID)

    input_sheet = workbook.worksheet(SHEET_NAME)
    log.info(f"Input worksheet ready: {SHEET_NAME}")

    try:
        output_sheet = workbook.worksheet(OUTPUT_SHEET_NAME)
        log.info(f"Output worksheet ready: {OUTPUT_SHEET_NAME}")
    except gspread.exceptions.WorksheetNotFound:
        output_sheet = workbook.add_worksheet(
            title=OUTPUT_SHEET_NAME,
            rows=1000,
            cols=14,
        )
        output_sheet.append_row([
            "timestamp", "status", "product_url", "affiliate_url", "product_title",
            "blog_title", "html_content", "meta_description",
            "focus_keyword", "tags", "image_url",
            "wp_post_id", "wp_post_url", "published_at",
        ], value_input_option="RAW")
        log.info(f"Created output worksheet: {OUTPUT_SHEET_NAME}")

    return input_sheet, output_sheet


def read_first_row(sheet):
    """
    Row 1 is data row 1.
    Column B = product URL to scrape
    Column C = affiliate URL for CTA
    """
    all_rows = sheet.get_all_values()
    if not all_rows:
        log.info("Input sheet is empty.")
        return None

    first = all_rows[0]

    product_url = first[1].strip() if len(first) >= 2 else ""
    affiliate_url = first[2].strip() if len(first) >= 3 else ""

    if not product_url:
        log.warning("Row 1 has no product URL in column B.")
        return None

    return {
        "row_number": 1,
        "product_url": product_url,
        "affiliate_url": affiliate_url,
    }


def delete_row(sheet, row_number: int):
    sheet.delete_rows(row_number)
    log.info(f"Deleted input row {row_number}.")


# ───────────────────────────────────────────────────────────────
# SCRAPING
# ───────────────────────────────────────────────────────────────

def _fetch_with_retry(url: str, retries: int = 3) -> BeautifulSoup:
    domain = urlparse(url).scheme + "://" + urlparse(url).netloc
    headers = {**SCRAPE_HEADERS, "Referer": domain}
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            log.info(f"Fetch attempt {attempt}/{retries}: {url[:120]}")
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status in (403, 429, 503):
                log.warning(f"Blocked ({status}) on attempt {attempt}. Retrying...")
                time.sleep(3 * attempt)
            else:
                log.warning(f"HTTP {status} on attempt {attempt}: {e}")
                time.sleep(2)
            last_exc = e
        except requests.exceptions.Timeout as e:
            log.warning(f"Timeout on attempt {attempt}")
            last_exc = e
            time.sleep(2)
        except Exception as e:
            log.warning(f"Request error on attempt {attempt}: {e}")
            last_exc = e
            time.sleep(2)

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
            image = (
                img_tag.get("data-old-hires")
                or img_tag.get("src")
                or img_tag.get("data-a-dynamic-image")
                or ""
            )
            if image.startswith("{"):
                try:
                    image = list(json.loads(image).keys())[0]
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

    title = (title or "").strip()
    desc = (desc or "").strip()
    price = (price or "").strip()
    image = (image or "").strip()

    if not title:
        title = "Unknown Product"
    if not desc:
        desc = "No description available."
    if not price:
        price = "Check website for price"

    product = {
        "url": url,
        "title": title,
        "desc": desc,
        "price": price,
        "image": image,
    }

    log.info(f"Scraped: {product['title']} | {product['price']}")
    return product


# ───────────────────────────────────────────────────────────────
# OPENROUTER BLOG GENERATION
# ───────────────────────────────────────────────────────────────

def generate_blog(product: dict) -> dict:
    log.info("Generating blog with OpenRouter...")

    affiliate_url = product.get("affiliate_url") or product["url"]

    prompt = f"""
Write a useful, original affiliate blog post for ONE specific product.

STRICT RULES:
- Stay on this product only.
- Do NOT turn it into a generic store or platform article.
- Do NOT mention Flipkart/Amazon/etc unless it is truly part of the product page.
- Do NOT invent specs, prices, or claims that are not supported by the product data.
- Use SEO, AEO, and GEO naturally.
- Keep the tone practical and Indian-commerce friendly.
- Output ONLY inner-body HTML.

PRODUCT DATA:
Product name: {product['title']}
Price: {product['price']}
Description: {product['desc'][:900]}
Product page URL: {product['url']}
Affiliate URL: {affiliate_url}

REQUIRED STRUCTURE:
<h1>
<h2>Introduction</h2>
<h2>Key Features</h2>
<h2>Pros and Cons</h2>
<h2>Who Should Buy</h2>
<h2>Comparison</h2>
<h2>FAQ</h2>
<h2>Conclusion</h2>
CTA links using:
<a href="{affiliate_url}" rel="nofollow sponsored" target="_blank">Buy Now</a>

FAQ:
Include 4 to 6 FAQ items.
Use <div class="faq-item"> around each FAQ block.

End with a short affiliate disclaimer in <em> tags.

After the HTML, output exactly this metadata block:

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

    data = resp.json()
    raw = data["choices"][0]["message"]["content"]

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

    blog = {
        "html_content": html_part.strip(),
        "blog_title": _meta("BLOG_TITLE", product["title"][:60]),
        "meta_desc": _meta("META_DESC", f"Review of {product['title']}."),
        "focus_keyword": _meta("FOCUS_KEYWORD", product["title"]),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
    }

    log.info(f"Blog generated: {blog['blog_title']}")
    return blog


# ───────────────────────────────────────────────────────────────
# WRITE OUTPUT ROW
# ───────────────────────────────────────────────────────────────

def append_to_sheet(output_sheet, product: dict, blog: dict):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tags_str = ", ".join(blog["tags"])
    affiliate_url = product.get("affiliate_url") or product["url"]

    row = [
        timestamp,                  # A
        "PENDING",                  # B
        product["url"],             # C product_url
        affiliate_url,              # D affiliate_url
        product["title"],           # E product_title
        blog["blog_title"],         # F blog_title
        blog["html_content"],       # G html_content
        blog["meta_desc"],          # H meta_description
        blog["focus_keyword"],      # I focus_keyword
        tags_str,                   # J tags
        product["image"],           # K image_url
        "",                         # L wp_post_id
        "",                         # M wp_post_url
        "",                         # N published_at
    ]

    output_sheet.append_row(row, value_input_option="RAW")
    log.info(f"Saved post to output sheet: {blog['blog_title']}")


# ───────────────────────────────────────────────────────────────
# OPTIONAL EMAIL
# ───────────────────────────────────────────────────────────────

def _send_smtp(subject: str, html_body: str):
    if not SEND_EMAIL:
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, NOTIFY_EMAIL, msg.as_string())

        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def email_queued(blog_title: str, product_url: str):
    if not SEND_EMAIL:
        return

    _send_smtp(
        f"Blog queued: {blog_title[:50]}",
        f"""
        <html><body>
        <h2>Blog content queued</h2>
        <p><b>Title:</b> {blog_title}</p>
        <p><b>Product URL:</b> <a href="{product_url}">{product_url}</a></p>
        <p><b>Status:</b> PENDING</p>
        </body></html>
        """,
    )


def email_error(context: str, error: str):
    if not SEND_EMAIL:
        return

    _send_smtp(
        "Blog Bot Error",
        f"""
        <html><body>
        <h2>Automation Error</h2>
        <p><b>Step failed:</b> {context}</p>
        <pre>{error}</pre>
        </body></html>
        """,
    )


def email_empty_sheet():
    if not SEND_EMAIL:
        return

    _send_smtp(
        "Blog Bot: input sheet empty",
        f"""
        <html><body>
        <h2>No products to process</h2>
        <p>The input sheet was empty.</p>
        </body></html>
        """,
    )


# ───────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ───────────────────────────────────────────────────────────────

def process_one(input_sheet, output_sheet) -> bool | None:
    row_data = read_first_row(input_sheet)
    if row_data is None:
        return None

    row_number = row_data["row_number"]
    product_url = row_data["product_url"]
    affiliate_url = row_data["affiliate_url"] or product_url

    try:
        product = scrape_product(product_url)
        product["affiliate_url"] = affiliate_url
    except Exception:
        err = traceback.format_exc()
        log.error(f"Scraping failed:\n{err}")
        email_error("Product Scraping", err)
        return False

    time.sleep(1)

    try:
        blog = generate_blog(product)
    except Exception:
        err = traceback.format_exc()
        log.error(f"Blog generation failed:\n{err}")
        email_error("OpenRouter Blog Generation", err)
        return False

    time.sleep(1)

    try:
        append_to_sheet(output_sheet, product, blog)
    except Exception:
        err = traceback.format_exc()
        log.error(f"Output sheet write failed:\n{err}")
        email_error("Google Sheets Append", err)
        return False

    try:
        delete_row(input_sheet, row_number)
    except Exception as e:
        log.warning(f"Input row deletion failed after successful save: {e}")

    email_queued(blog["blog_title"], product["url"])
    return True


def main():
    sep = "━" * 60
    log.info(sep)
    log.info("Affiliate Blog Bot starting")
    log.info(f"Input sheet : {SHEET_NAME}")
    log.info(f"Output sheet: {OUTPUT_SHEET_NAME}")
    log.info(f"Max products: {MAX_PRODUCTS}")
    log.info(sep)

    input_sheet, output_sheet = get_sheets()
    processed = 0
    errors = 0

    for i in range(MAX_PRODUCTS):
        log.info(f"Product {i + 1} of {MAX_PRODUCTS}")

        result = process_one(input_sheet, output_sheet)

        if result is None:
            if processed == 0:
                email_empty_sheet()
            log.info("No more rows to process.")
            break

        if result:
            processed += 1
        else:
            errors += 1

        if i < MAX_PRODUCTS - 1:
            time.sleep(2)

    log.info(sep)
    log.info(f"Run complete: {processed} queued, {errors} failed")
    log.info(sep)

    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
