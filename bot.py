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

import os
import sys
import time
import json
import base64
import logging
import smtplib
import traceback
import requests

from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from PIL import Image

# ───────────────────────────────────────────────────────────────
#  LOGGING  (stdout only — GitHub Actions captures it natively)
# ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot")

# ───────────────────────────────────────────────────────────────
#  CONFIG — loaded entirely from environment variables
# ───────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    """Fetch a required env var; abort if missing."""
    val = os.environ.get(name, "").strip()
    if not val:
        log.error(f"Missing required environment variable: {name}")
        sys.exit(1)
    return val


# Google Sheets
SPREADSHEET_ID   = _require("SPREADSHEET_ID")
SHEET_NAME       = os.environ.get("SHEET_NAME", "Sheet1")
LINK_COLUMN      = int(os.environ.get("LINK_COLUMN", "1"))

# OpenRouter
OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3-haiku")

# WordPress
WP_SITE_URL     = _require("WP_SITE_URL").rstrip("/")   # strip trailing slash
WP_USERNAME     = _require("WP_USERNAME")
WP_APP_PASSWORD = _require("WP_APP_PASSWORD")
WP_CATEGORY_ID  = int(os.environ.get("WP_CATEGORY_ID", "1"))

# Email
EMAIL_USER      = _require("EMAIL_USER")
EMAIL_PASSWORD  = _require("EMAIL_PASSWORD")
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL", EMAIL_USER)

# Scraper
REQUEST_TIMEOUT = 20
MAX_PRODUCTS    = int(os.environ.get("MAX_PRODUCTS", "3"))

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
#  STEP 0 — DECODE BASE64 SERVICE ACCOUNT CREDENTIALS
# ───────────────────────────────────────────────────────────────

def _decode_google_creds() -> dict:
    """
    Decode the base64-encoded service account JSON from the environment.

    To generate GOOGLE_CREDS_BASE64:
        base64 -w 0 service_account.json   # Linux/macOS
        [Convert]::ToBase64String([IO.File]::ReadAllBytes("service_account.json"))  # PowerShell
    Then paste the output as a GitHub Actions secret.
    """
    raw_b64 = _require("GOOGLE_CREDS_BASE64")
    try:
        json_bytes = base64.b64decode(raw_b64)
        return json.loads(json_bytes)
    except Exception as e:
        log.error(f"Failed to decode GOOGLE_CREDS_BASE64: {e}")
        sys.exit(1)

# ───────────────────────────────────────────────────────────────
#  STEP 1 — GOOGLE SHEETS
# ───────────────────────────────────────────────────────────────

def get_sheet():
    """Authenticate with in-memory service account and return the worksheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = _decode_google_creds()
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client     = gspread.authorize(creds)
    sheet      = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    log.info("Sheets: authenticated successfully.")
    return sheet


def read_first_row(sheet) -> dict | None:
    """
    Return {'row_number': 2, 'url': '...'} for the first data row.
    Row 1 is treated as the header and is always skipped.
    Returns None if the sheet has no data rows.
    """
    all_rows  = sheet.get_all_values()
    data_rows = all_rows[1:]    # skip header

    if not data_rows:
        log.info("Sheet is empty — nothing to process.")
        return None

    first = data_rows[0]
    url   = first[LINK_COLUMN - 1].strip() if len(first) >= LINK_COLUMN else ""

    if not url:
        log.warning("Row 2 exists but URL cell is empty — skipping.")
        return None

    log.info(f"Sheets: URL found in row 2 → {url}")
    return {"row_number": 2, "url": url}


def delete_row(sheet, row_number: int):
    sheet.delete_rows(row_number)
    log.info(f"Sheets: row {row_number} deleted.")

# ───────────────────────────────────────────────────────────────
#  STEP 2 — SCRAPE PRODUCT PAGE
# ───────────────────────────────────────────────────────────────

def _fetch_with_retry(url: str, retries: int = 3) -> BeautifulSoup:
    """
    Fetch a URL with retry logic, timeout handling, and bot-evasion headers.
    Adds Referer header derived from the domain.
    """
    domain  = urlparse(url).scheme + "://" + urlparse(url).netloc
    headers = {**SCRAPE_HEADERS, "Referer": domain}
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            log.info(f"Fetch attempt {attempt}/{retries}: {url[:80]}")
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status in (403, 503):
                log.warning(f"Blocked ({status}) on attempt {attempt} — backing off.")
                time.sleep(4 * attempt)
            else:
                log.warning(f"HTTP {status} on attempt {attempt}: {e}")
                time.sleep(2)
            last_exc = e
        except requests.exceptions.Timeout:
            log.warning(f"Timeout on attempt {attempt}.")
            last_exc = TimeoutError(f"Timed out after {REQUEST_TIMEOUT}s")
            time.sleep(2)
        except Exception as e:
            log.warning(f"Request error on attempt {attempt}: {e}")
            last_exc = e
            time.sleep(2)

    raise RuntimeError(f"All {retries} fetch attempts failed for {url}") from last_exc


def _txt(tag, attr=None) -> str:
    if tag is None:
        return ""
    return (tag.get(attr) or "").strip() if attr else tag.get_text(strip=True)


def _og(soup, prop: str) -> str:
    tag = soup.find("meta", property=prop)
    return (tag.get("content") or "").strip() if tag else ""


def scrape_product(url: str) -> dict:
    """
    Scrape title / price / description / image from a product URL.
    Dedicated scrapers for Amazon and Flipkart; generic OG fallback for others.
    Raises RuntimeError if the site blocks all attempts.
    """
    log.info(f"Scraping: {url}")
    domain = urlparse(url).netloc.lower()

    soup = _fetch_with_retry(url)

    if "amazon" in domain:
        title   = _txt(soup.select_one("#productTitle"))
        price   = (
            _txt(soup.select_one(".a-price-whole"))
            or _txt(soup.select_one("#priceblock_ourprice"))
            or _txt(soup.select_one("#priceblock_dealprice"))
            or _txt(soup.select_one(".a-offscreen"))
        )
        bullets = soup.select("#feature-bullets ul li span.a-list-item")
        desc    = "\n".join(b.get_text(strip=True) for b in bullets if b.get_text(strip=True))
        img_tag = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
        image   = (
            (img_tag.get("data-old-hires") or img_tag.get("data-a-dynamic-image") or img_tag.get("src") or "")
            if img_tag else ""
        )
        # data-a-dynamic-image is a JSON dict of {url: [w, h]} — extract first key
        if image.startswith("{"):
            try:
                image = list(json.loads(image).keys())[0]
            except Exception:
                image = ""

        if not title:
            raise RuntimeError("Amazon: product title not found — page may be blocked.")

    elif "flipkart" in domain:
        title   = _txt(
            soup.select_one("span.B_NuCI")
            or soup.select_one("h1._9E25nV")
            or soup.select_one("h1.yhB1nd")
        )
        price   = _txt(
            soup.select_one("div._30jeq3._16Jk6d")
            or soup.select_one("div._30jeq3")
            or soup.select_one("div.Nx9bqj")
        )
        desc    = "\n".join(d.get_text(strip=True) for d in soup.select("div._1AN87F li"))
        img_tag = (
            soup.select_one("img._396cs4")
            or soup.select_one("img._2r_T1I")
            or soup.select_one("img.DByuf4")
        )
        image   = img_tag.get("src", "") if img_tag else ""

    else:
        # Generic — Open Graph tags + microdata fallback
        title   = (
            _og(soup, "og:title")
            or _txt(soup.find("h1"))
            or _txt(soup.find("title"))
        )
        desc    = (
            _og(soup, "og:description")
            or _txt(soup.find("meta", {"name": "description"}), "content")
        )
        price   = _txt(
            soup.select_one('[itemprop="price"]')
            or soup.select_one(".price")
            or soup.select_one('[class*="price"]')
        )
        image   = _og(soup, "og:image") or ""

    product = {
        "url":   url,
        "title": (title or "Unknown Product").strip(),
        "desc":  (desc  or "No description available.").strip(),
        "price": (price or "Check website for price").strip(),
        "image": image.strip(),
    }
    log.info(f"Scraped → '{product['title'][:70]}' | {product['price']}")
    return product

# ───────────────────────────────────────────────────────────────
#  STEP 3 — GENERATE BLOG WITH OPENROUTER
# ───────────────────────────────────────────────────────────────

def generate_blog(product: dict) -> dict:
    """
    Call OpenRouter API to produce a full HTML blog post.
    Returns: html_content, blog_title, meta_desc, focus_keyword, tags
    """
    log.info("OpenRouter: generating blog post…")

    prompt = f"""You are an expert affiliate content writer for an Indian product review blog.
You specialise in SEO, AEO (Answer Engine Optimisation), and GEO (Generative Engine Optimisation).

Write a complete affiliate blog post for this product.

PRODUCT:
- Name:  {product['title']}
- Price: {product['price']}
- Features / Description: {product['desc'][:1500]}
- Affiliate URL: {product['url']}

REQUIREMENTS:
1. 1200–1500 words
2. SEO: use the focus keyword naturally 6–8 times
3. AEO: include 4–6 FAQ Q&A pairs that directly answer buyer questions
4. GEO: reference Indian context, INR pricing, India-specific use cases
5. Output only clean inner-body HTML — NO <html>, <head>, or <body> tags
6. HTML structure:
   <h1> blog title
   <h2> for each section
   <h3> for sub-sections and FAQ questions
   Sections: Introduction → Key Features (<ul>) → Pros & Cons (two <ul>) →
   Who Should Buy → Market Comparison → FAQ (<div class="faq-item">) →
   Conclusion → CTA
7. Include affiliate link 2–3 times as:
   <a href="{product['url']}" rel="nofollow sponsored" target="_blank">Buy Now</a>
8. End with a short affiliate disclaimer paragraph in <em> tags

After the HTML block, on its own line, write exactly:
---META---
BLOG_TITLE: [55–60 character title with focus keyword]
META_DESC: [150–160 character meta description]
FOCUS_KEYWORD: [main keyword phrase]
TAGS: [tag1, tag2, tag3, tag4, tag5]
"""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization":  f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":   "application/json",
            "HTTP-Referer":   WP_SITE_URL,
            "X-Title":        "Affiliate Blog Bot",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {
                    "role":    "user",
                    "content": prompt,
                }
            ],
            "temperature": 0.7,
            "max_tokens":  4096,
        },
        timeout=120,
    )
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"]

    # Split HTML from metadata block
    if "---META---" in raw:
        html_part, meta_part = raw.split("---META---", 1)
    else:
        html_part, meta_part = raw, ""

    def _meta(key: str, default: str) -> str:
        for line in meta_part.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith(key.upper() + ":"):
                return stripped[len(key) + 1:].strip()
        return default

    blog = {
        "html_content":  html_part.strip(),
        "blog_title":    _meta("BLOG_TITLE",    product["title"][:60]),
        "meta_desc":     _meta("META_DESC",     f"Review of {product['title']}. {product['price']}."),
        "focus_keyword": _meta("FOCUS_KEYWORD", product["title"]),
        "tags":          [t.strip() for t in _meta("TAGS", "review,affiliate,india").split(",")],
    }
    log.info(f"OpenRouter: blog ready — '{blog['blog_title']}'")
    return blog

# ───────────────────────────────────────────────────────────────
#  STEP 4 — UPLOAD IMAGE TO WORDPRESS
# ───────────────────────────────────────────────────────────────

def upload_image(image_url: str, alt_text: str):
    """
    Download image → compress with Pillow → upload to WP media library.
    Returns WordPress media ID, or None on any failure (non-fatal).
    """
    if not image_url:
        log.info("Image: no URL provided — skipping.")
        return None

    try:
        raw = requests.get(
            image_url,
            headers=SCRAPE_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        raw.raise_for_status()

        # Compress: max 1200px wide, JPEG quality 82
        img = Image.open(BytesIO(raw.content)).convert("RGB")
        if img.width > 1200:
            ratio = 1200 / img.width
            img   = img.resize((1200, int(img.height * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        img_bytes = buf.getvalue()
        log.info(f"Image: {len(raw.content)//1024}KB → {len(img_bytes)//1024}KB compressed")

        filename = f"product-{int(time.time())}.jpg"
        upload   = requests.post(
            f"{WP_SITE_URL}/wp-json/wp/v2/media",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type":        "image/jpeg",
            },
            data=img_bytes,
            timeout=60,
        )
        upload.raise_for_status()
        media_id = upload.json()["id"]

        # Set alt text (best-effort)
        requests.post(
            f"{WP_SITE_URL}/wp-json/wp/v2/media/{media_id}",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            json={"alt_text": alt_text[:125]},
            timeout=20,
        )
        log.info(f"Image: uploaded — media ID {media_id}")
        return media_id

    except Exception as e:
        log.warning(f"Image upload failed (non-fatal, post will continue): {e}")
        return None

# ───────────────────────────────────────────────────────────────
#  STEP 5 — PUBLISH TO WORDPRESS
# ───────────────────────────────────────────────────────────────

def _get_or_create_tags(names: list) -> list:
    """Look up tag IDs by name; create tags that don't yet exist."""
    ids = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        try:
            r = requests.get(
                f"{WP_SITE_URL}/wp-json/wp/v2/tags",
                auth=(WP_USERNAME, WP_APP_PASSWORD),
                params={"search": name, "per_page": 5},
                timeout=20,
            )
            match = next(
                (t for t in (r.json() if r.ok else []) if t["name"].lower() == name.lower()),
                None,
            )
            if match:
                ids.append(match["id"])
            else:
                cr = requests.post(
                    f"{WP_SITE_URL}/wp-json/wp/v2/tags",
                    auth=(WP_USERNAME, WP_APP_PASSWORD),
                    json={"name": name},
                    timeout=20,
                )
                if cr.ok:
                    ids.append(cr.json()["id"])
                else:
                    log.warning(f"Could not create tag '{name}': {cr.status_code}")
        except Exception as e:
            log.warning(f"Tag handling error for '{name}': {e}")
    return ids


def publish_to_wordpress(blog: dict, media_id, retries: int = 2) -> dict:
    """Publish post with retry logic. Returns WP post response dict."""
    log.info("WordPress: publishing post…")
    tag_ids = _get_or_create_tags(blog["tags"])

    payload = {
        "title":          blog["blog_title"],
        "content":        blog["html_content"],
        "excerpt":        blog["meta_desc"],
        "status":         "publish",
        "categories":     [WP_CATEGORY_ID],
        "tags":           tag_ids,
        "comment_status": "open",
        "meta": {
            "_yoast_wpseo_metadesc":   blog["meta_desc"],
            "_yoast_wpseo_focuskw":    blog["focus_keyword"],
            "rank_math_description":   blog["meta_desc"],
            "rank_math_focus_keyword": blog["focus_keyword"],
        },
    }
    if media_id:
        payload["featured_media"] = media_id

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            log.info(f"WP publish attempt {attempt}/{retries}…")
            resp = requests.post(
                f"{WP_SITE_URL}/wp-json/wp/v2/posts",
                auth=(WP_USERNAME, WP_APP_PASSWORD),
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            post = resp.json()
            log.info(f"WordPress: published → ID {post['id']} | {post.get('link', '')}")
            return post
        except Exception as e:
            log.warning(f"WP publish attempt {attempt} failed: {e}")
            last_exc = e
            if attempt < retries:
                time.sleep(5)

    raise RuntimeError("WordPress publish failed after all retries.") from last_exc

# ───────────────────────────────────────────────────────────────
#  STEP 6 — EMAIL VIA GMAIL SMTP
# ───────────────────────────────────────────────────────────────

def _send_smtp(subject: str, html_body: str):
    """Send HTML email via Gmail SMTP (port 587, STARTTLS). Non-fatal."""
    try:
        msg              = MIMEMultipart("alternative")
        msg["Subject"]   = subject
        msg["From"]      = EMAIL_USER
        msg["To"]        = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, NOTIFY_EMAIL, msg.as_string())

        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed (non-fatal): {e}")


def email_success(title: str, post_url: str):
    _send_smtp(
        f"✅ Blog Published: {title[:50]}",
        f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
        <h2 style="color:#2e7d32;border-bottom:2px solid #2e7d32;padding-bottom:8px">
          🎉 Post Published Successfully
        </h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:8px;color:#555"><b>Product:</b></td>
              <td style="padding:8px">{title}</td></tr>
          <tr style="background:#f5f5f5">
              <td style="padding:8px;color:#555"><b>Post URL:</b></td>
              <td style="padding:8px"><a href="{post_url}" style="color:#1565c0">{post_url}</a></td></tr>
          <tr><td style="padding:8px;color:#555"><b>Time:</b></td>
              <td style="padding:8px">{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}</td></tr>
        </table>
        </body></html>""",
    )


def email_error(context: str, error: str):
    _send_smtp(
        "❌ Blog Bot Error",
        f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
        <h2 style="color:#c62828;border-bottom:2px solid #c62828;padding-bottom:8px">
          ⚠️ Automation Error
        </h2>
        <p><b>Step failed:</b> {context}</p>
        <pre style="background:#fce4ec;padding:14px;border-radius:6px;
                    font-size:12px;overflow:auto;white-space:pre-wrap">{error}</pre>
        <p style="color:#555">
          Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}<br>
          The row has <b>NOT</b> been deleted from Google Sheets.
        </p>
        </body></html>""",
    )


def email_empty_sheet():
    _send_smtp(
        "⚠️ Blog Bot — Sheet Empty",
        f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
        <h2 style="color:#e65100;border-bottom:2px solid #e65100;padding-bottom:8px">
          📋 No Products to Process
        </h2>
        <p>The bot ran at <b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}</b>
           but found no product URLs in the Google Sheet.</p>
        <p>Please add new affiliate links in column A (starting from row 2) to continue.</p>
        </body></html>""",
    )

# ───────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ───────────────────────────────────────────────────────────────

def process_one(sheet) -> bool:
    """
    Process a single product from row 2.
    Returns True on success, False on failure (row is NOT deleted on failure).
    """
    row_data = read_first_row(sheet)
    if row_data is None:
        return None     # sentinel: sheet is empty

    row_number  = row_data["row_number"]
    product_url = row_data["url"]

    # ── 2. Scrape ──────────────────────────────────────────────
    try:
        product = scrape_product(product_url)
    except Exception:
        err = traceback.format_exc()
        log.error(f"Scraping failed:\n{err}")
        email_error("Product Scraping", err)
        return False    # row NOT deleted

    time.sleep(1.5)

    # ── 3. Generate blog ───────────────────────────────────────
    try:
        blog = generate_blog(product)
    except Exception:
        err = traceback.format_exc()
        log.error(f"Blog generation failed:\n{err}")
        email_error("OpenRouter Blog Generation", err)
        return False

    time.sleep(1)

    # ── 4. Upload image (non-fatal) ────────────────────────────
    media_id = upload_image(product["image"], alt_text=product["title"])

    # ── 5. Publish to WordPress ────────────────────────────────
    try:
        wp_post = publish_to_wordpress(blog, media_id)
    except Exception:
        err = traceback.format_exc()
        log.error(f"WordPress publish failed:\n{err}")
        email_error("WordPress Publishing", err)
        return False

    # ── 6. Delete row — ONLY after confirmed publish ───────────
    try:
        delete_row(sheet, row_number)
    except Exception as e:
        log.warning(f"Row deletion failed (post is already live): {e}")

    # ── 7. Notify success ──────────────────────────────────────
    email_success(product["title"], wp_post.get("link", WP_SITE_URL))
    return True


def main():
    separator = "━" * 60
    log.info(separator)
    log.info("  Affiliate Blog Bot — starting run")
    log.info(f"  Max products this run: {MAX_PRODUCTS}")
    log.info(separator)

    sheet     = get_sheet()
    processed = 0
    errors    = 0

    for i in range(MAX_PRODUCTS):
        log.info(f"\n{'─' * 40}")
        log.info(f"  Product {i + 1} of {MAX_PRODUCTS}")
        log.info(f"{'─' * 40}")

        result = process_one(sheet)

        if result is None:
            # Sheet is empty
            if processed == 0:
                email_empty_sheet()
            log.info("No more products in sheet — stopping early.")
            break

        if result:
            processed += 1
        else:
            errors += 1

        # Delay between products (except after the last one)
        if i < MAX_PRODUCTS - 1:
            time.sleep(2)

    log.info(separator)
    log.info(f"  Run complete — {processed} published, {errors} failed")
    log.info(separator)

    if errors > 0:
        sys.exit(1)     # non-zero exit makes the GitHub Actions step red


if __name__ == "__main__":
    main()
