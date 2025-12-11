import time
import json
import re
from urllib.parse import urlparse
import os
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------
# Gemini setup
# ---------------------------------------------------------
from google.genai import Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = Client(api_key=GEMINI_API_KEY)
MODEL = "models/gemini-2.5-flash"

ALLOWED_STYLE_TAGS = [
    "#streetwear",
    "#athleisure",
    "#vintageStreet",
    "#techwear",
    "#minimalist",
    "#loudGraphic",
    "#hypebeast",
    "#y2k",
    "#skater",
]

# =========================================================
# BRAND / PRICE HELPERS
# =========================================================

def extract_brand_from_jsonld(html: str):
    """Try to read brand from JSON-LD blocks."""
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            try:
                data = json.loads(block.strip())
            except Exception:
                continue

        # dict
        if isinstance(data, dict):
            brand = data.get("brand")
            if brand:
                if isinstance(brand, dict):
                    return brand.get("name")
                return brand

        # list of dicts
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "brand" in item:
                    brand = item["brand"]
                    if isinstance(brand, dict):
                        return brand.get("name")
                    return brand
    return None


def detect_brand(url: str, html: str) -> str:
    """Brand from JSON-LD first, then from domain."""
    brand = extract_brand_from_jsonld(html)
    if brand:
        return brand.strip()

    domain = urlparse(url).hostname or ""
    domain = domain.replace("www.", "")
    first = domain.split(".")[0]
    # strip common geo / tld fragments
    first = (
        first.replace("uk", "")
        .replace("us", "")
        .replace("eu", "")
        .replace("co", "")
        .replace("shop", "")
    )
    return first.strip().title()


def extract_price_from_scripts(html: str):
    """Try multiple patterns + JSON-LD offers to find price."""

    patterns = [
        r'"price"\s*:\s*"([^"]+)"',
        r'"price":\s*([0-9]+\.[0-9]+)',
        r'"nowPrice"\s*:\s*"([^"]+)"',
        r'"salePrice"\s*:\s*"([^"]+)"',
        r'"unitPrice"\s*:\s*"([^"]+)"',
        r'"currentPrice"\s*:\s*"([^"]+)"',
        r'"regularPrice"\s*:\s*"([^"]+)"',
        r'"value"\s*:\s*"([£$€][0-9\.,]+)"',
    ]

    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)

    # JSON-LD offers
    ld_json = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    for block in ld_json:
        try:
            data = json.loads(block)
        except Exception:
            try:
                data = json.loads(block.strip())
            except Exception:
                continue

        def read_price(obj):
            if not isinstance(obj, dict):
                return None
            offers = obj.get("offers")
            if isinstance(offers, dict) and "price" in offers:
                return str(offers["price"])
            return None

        if isinstance(data, dict):
            price = read_price(data)
            if price:
                return price
        elif isinstance(data, list):
            for item in data:
                price = read_price(item)
                if price:
                    return price

    return None


# =========================================================
# IMAGE EXTRACTOR (UNIVERSAL, HEURISTIC)
# =========================================================

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")

IMAGE_BLOCKLIST = [
    # payments / trust
    "visa", "mastercard", "maestro", "amex", "americanexpress",
    "paypal", "klarna", "afterpay", "clearpay", "trustpilot",
    "applepay", "googlepay", "payment", "secure",
    # social
    "facebook", "instagram", "youtube", "tiktok", "twitter", "x-logo",
    # ui / branding
    "logo", "sprite", "icon", "favicon", "placeholder", "noimage",
    "search", "menu", "hamburger", "cart", "basket", "bag", "close",
    "arrow", "chevron", "caret", "scroll", "slider",
    # layout
    "banner", "promo", "offer", "ads", "advert",
    "footer", "header",
    # flags
    "flag", "country-",
]

PRODUCT_KEYWORDS = [
    "product", "pdp", "gallery", "image", "main", "hero",
    "shoe", "trainer", "sneaker", "boot",
    "jacket", "coat", "hoodie", "tee", "t-shirt", "shirt",
    "dress", "skirt", "trouser", "shorts", "jeans", "pant",
    "bag", "cap", "hat", "backpack",
]


def extract_images_from_page(page, url: str, title: str = ""):
    """
    UNIVERSAL product image extractor.

    Covers:
      - <img> (src, data-src, currentSrc)
      - <source srcset> inside <picture>
      - Elements with CSS background-image: url(...)
    Scores and returns the most likely product images.
    """

    img_info = page.evaluate(
        """
        () => {
          const results = [];

          const elements = Array.from(
            document.querySelectorAll('img, picture source, [style*="background-image"]')
          );

          for (const el of elements) {
            const rect = el.getBoundingClientRect();
            let src = "";

            if (el.tagName === "IMG") {
              src =
                el.currentSrc ||
                el.src ||
                el.getAttribute('data-src') ||
                el.getAttribute('data-original') ||
                el.getAttribute('data-zoom-image') ||
                el.getAttribute('data-lazy') ||
                "";
            } else if (el.tagName === "SOURCE") {
              const srcset = el.srcset || el.getAttribute("srcset") || "";
              if (srcset) {
                // take last (usually largest) candidate from srcset
                const parts = srcset.split(",").map(s => s.trim()).filter(Boolean);
                if (parts.length > 0) {
                  src = parts[parts.length - 1].split(" ")[0];
                }
              }
            } else {
              // background-image
              const style = window.getComputedStyle(el);
              const bg = style.backgroundImage || el.style.backgroundImage || "";
              const match = bg.match(/url\\(["']?(.*?)["']?\\)/i);
              if (match && match[1]) {
                src = match[1];
              }
            }

            const naturalWidth = (el.naturalWidth || 0);
            const naturalHeight = (el.naturalHeight || 0);
            const layoutWidth = rect.width || 0;
            const layoutHeight = rect.height || 0;

            const width = naturalWidth || layoutWidth;
            const height = naturalHeight || layoutHeight;

            results.push({
              src,
              width,
              height,
              alt: el.alt || "",
              className: el.className || "",
              inPicture: !!el.closest && !!el.closest("picture"),
              top: rect.top || 0,
            });
          }

          return results;
        }
        """
    )

    slug_tokens = set(re.findall(r"[a-z0-9]+", url.split("?")[0].split("/")[-1].lower()))
    title_tokens = set(re.findall(r"[a-z0-9]+", (title or "").lower()))

    candidates = []
    seen = set()

    for info in img_info:
        src = info.get("src") or ""
        if not src:
            continue

        # normalise protocol-relative URLs
        if src.startswith("//"):
            src = "https:" + src

        if not src.startswith("http"):
            continue

        src_l = src.lower()

        # hard block UI/logo/payment etc.
        if any(b in src_l for b in IMAGE_BLOCKLIST):
            continue

        # must be an image-like URL (either endswith common ext OR has ext in query)
        if not src_l.endswith(IMAGE_EXTS) and not any(ext in src_l for ext in IMAGE_EXTS):
            # still allow extension-less CDN URLs if they look like assets
            # but if the path clearly looks like css/js, drop it
            if any(ext in src_l for ext in [".css", ".js", ".json", ".svg"]):
                continue

        # avoid duplicates
        if src in seen:
            continue
        seen.add(src)

        w = info.get("width") or 0
        h = info.get("height") or 0
        area = w * h

        alt = (info.get("alt") or "").lower()
        cls = (info.get("className") or "").lower()
        in_picture = bool(info.get("inPicture"))
        top = info.get("top") or 0

        # ---------- scoring ----------
        score = 0

        # big images = more likely product
        if area >= 800 * 800:
            score += 8
        elif area >= 600 * 600:
            score += 6
        elif area >= 400 * 400:
            score += 4
        elif area >= 200 * 200:
            score += 2
        else:
            # still allow very small but with good semantic hints
            if any(k in alt for k in PRODUCT_KEYWORDS) or any(
                k in cls for k in PRODUCT_KEYWORDS
            ):
                score += 1

        # hero / gallery images often in <picture>
        if in_picture:
            score += 3

        # alt / class hints
        if any(k in alt for k in PRODUCT_KEYWORDS):
            score += 2
        if any(k in cls for k in PRODUCT_KEYWORDS):
            score += 2

        # top half of page more likely main gallery than footer ads
        if -200 <= top <= 1600:
            score += 1

        # filename overlaps with slug / title tokens
        name_tokens = set(re.findall(r"[a-z0-9]+", src_l))
        overlap_slug = len(name_tokens & slug_tokens)
        overlap_title = len(name_tokens & title_tokens)
        if overlap_slug >= 2:
            score += 2
        if overlap_title >= 1:
            score += 1

        candidates.append((score, area, src))

    # sort by score then by area (desc)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if not candidates:
        return []

    # keep those with positive score; if none, just return top 3 by area
    positive = [src for score, area, src in candidates if score > 0]
    if positive:
        return positive[:10]

    fallback = [src for score, area, src in candidates]
    return fallback[:5]


# =========================================================
# SIZE DETECTION
# =========================================================

VALID_SIZE_WORDS = {
    "XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL",
    "3XL", "4XL", "5XL",
    "ONE SIZE", "ONESIZE", "OS",
    "S/M", "M/L", "L/XL",
}


def is_valid_size_token(token: str) -> bool:
    t = token.strip().upper()
    if not t:
        return False

    # exact word match (with/without spaces)
    if t in VALID_SIZE_WORDS:
        return True
    if t.replace(" ", "") in VALID_SIZE_WORDS:
        return True

    # 2-digit numeric sizes like 28, 30, 32 ... 60 (jeans, waist)
    if re.fullmatch(r"\d{2}", t):
        val = int(t)
        if 20 <= val <= 60:
            return True

    return False


# =========================================================
# MAIN SCRAPER (Playwright)
# =========================================================

def extract_raw_product_data(url: str):
    with sync_playwright() as p:
        iphone = p.devices["iPhone 13"]
        browser = p.chromium.launch(headless=False)

        page = browser.new_page(
            user_agent=iphone["user_agent"],
            viewport=iphone["viewport"],
            device_scale_factor=iphone["device_scale_factor"],
            has_touch=iphone["has_touch"],
            is_mobile=iphone["is_mobile"],
        )

        page.set_extra_http_headers(
            {
                "User-Agent": iphone["user_agent"],
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        print("Loading page...")  # DEBUG PRINT

        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
            print("Page loaded!")
        except Exception as e:
            print("⚠ Page load failed or blocked:", e)


        # give scripts / lazy images some time
        time.sleep(3)

        # scroll to trigger lazy loading
        for _ in range(3):
            try:
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                time.sleep(1)
            except Exception:
                break

        html = page.content()

        # ---- Title ----
        title = None
        for sel in ["h1", ".product-name", ".pdp-title", "[itemprop='name']"]:
            el = page.query_selector(sel)
            if el:
                title = el.inner_text().strip()
                break

        # ---- Brand ----
        brand = detect_brand(url, html)

        # ---- Price ----
        price = None
        for sel in ["[itemprop='price']", ".price", ".product-price"]:
            el = page.query_selector(sel)
            if el:
                price = el.inner_text().strip()
                break
        if not price:
            price = extract_price_from_scripts(html)

        # ---- Description ----
        desc = None
        for sel in [".description", ".product-description", ".pdp-description"]:
            el = page.query_selector(sel)
            if el:
                desc = el.inner_text().strip()
                break

        # ---- Images ----
        images = extract_images_from_page(page, url, title or "")

        # ---- Sizes ----
        sizes = set()

        # 1) Buttons with visible text
        for btn in page.query_selector_all(
            "button, .size, .size-button, .swatch__option"
        ):
            t = (btn.inner_text() or "").strip()
            if is_valid_size_token(t):
                sizes.add(t.strip().upper())

        # 2) aria-label sizes
        for btn in page.query_selector_all("button[aria-label]"):
            v = btn.get_attribute("aria-label")
            if v and is_valid_size_token(v):
                sizes.add(v.strip().upper())

        # 3) Radio / input attributes
        for inp in page.query_selector_all("input[type='radio'], input[type='button']"):
            for attr in ["value", "data-value", "data-option", "aria-label"]:
                v = inp.get_attribute(attr)
                if v and is_valid_size_token(v):
                    sizes.add(v.strip().upper())

        # 4) Dropdown options
        for opt in page.query_selector_all("select option"):
            t = (opt.inner_text() or "").strip()
            if is_valid_size_token(t):
                sizes.add(t.strip().upper())

        browser.close()

    return {
        "raw_title": title,
        "raw_brand": brand,
        "raw_price": price,
        "raw_description": desc,
        "raw_images": images,
        "raw_sizes": sorted(list(sizes)),
        "url": url,
    }


# =========================================================
# GEMINI CLEANER (fill only missing fields)
# =========================================================

def clean_product_data(raw: dict):
    # Build base product JSON from scraped fields
    product = {
        "product_name": raw.get("raw_title") or "",
        "brand": raw.get("raw_brand") or "",
        "description": raw.get("raw_description") or "",
        "price": raw.get("raw_price") or "",
        "currency": "",
        "category": "",
        "gender_target": "",
        "style_tags": [],
        "image_url": (raw.get("raw_images")[0] if raw.get("raw_images") else ""),
        "sizes_available": raw.get("raw_sizes") or [],
        "product_url": raw.get("url") or "",
    }

    prompt = f"""
You are cleaning and enriching fashion e-commerce product data.

You are given:
1) SCRAPED_RAW_DATA which may be incomplete or messy.
2) CURRENT_PRODUCT_JSON which already has some fields filled in.

RULES:
- You MUST return ONLY a single valid JSON object. No markdown, no backticks.
- For any field in CURRENT_PRODUCT_JSON that is NON-EMPTY (non-empty string, non-empty list),
  you MUST keep the value exactly as it is.
- For any field that is empty (""), null, or an empty list:
    - description: write a natural 1–3 sentence product description
      based on title, brand, any text, and URL.
    - currency: detect from price or URL. If unclear and the site looks UK/European,
      use "GBP" as a default.
    - category: one of exactly ["top", "bottom", "outerwear", "footwear", "full_body", "accessory"].
    - gender_target: one of exactly ["masculine", "feminine", "unisex"].
    - style_tags: a list of 1–4 tags chosen ONLY from:
      {ALLOWED_STYLE_TAGS}.

IMPORTANT:
- Do NOT change product_name, brand, price, image_url, sizes_available, or product_url
  if they are already filled in.
- Do NOT invent an image_url if it is empty; leave it as an empty string.
- Use SCRAPED_RAW_DATA as context, but the final keys MUST match CURRENT_PRODUCT_JSON.

SCRAPED_RAW_DATA:
{json.dumps(raw, ensure_ascii=False)}

CURRENT_PRODUCT_JSON:
{json.dumps(product, ensure_ascii=False)}
"""


    resp = client.models.generate_content(model=MODEL, contents=prompt)
    text = resp.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        cleaned = json.loads(text)
    except Exception:
        print("RAW GEMINI OUTPUT COULD NOT BE PARSED:")
        print(text)
        return None

    # Hard enforce scraped values (these are ground truth)
    if raw.get("raw_title"):
        cleaned["product_name"] = raw["raw_title"]
    if raw.get("raw_brand"):
        cleaned["brand"] = raw["raw_brand"]
    if raw.get("raw_price"):
        cleaned["price"] = raw["raw_price"]
    if raw.get("url"):
        cleaned["product_url"] = raw["url"]

    # Image: use scraped first image if available
    if raw.get("raw_images"):
        cleaned["image_url"] = raw["raw_images"][0]

    # Sizes: use scraped clean sizes
    if raw.get("raw_sizes"):
        cleaned["sizes_available"] = raw["raw_sizes"]

    # Currency: try to infer from price string; else default GBP
    if not cleaned.get("currency"):
        price_str = (raw.get("raw_price") or "") + ""
        if "€" in price_str:
            cleaned["currency"] = "EUR"
        elif "$" in price_str:
            cleaned["currency"] = "USD"
        elif "£" in price_str:
            cleaned["currency"] = "GBP"
        else:
            host = urlparse(raw.get("url") or "").hostname or ""
            if ".co.uk" in host or host.endswith(".uk"):
                cleaned["currency"] = "GBP"
            elif any(tld in host for tld in [".de", ".fr", ".es", ".it"]):
                cleaned["currency"] = "EUR"
            else:
                cleaned["currency"] = "GBP"

    # Filter style_tags to allowed set
    tags = cleaned.get("style_tags") or []
    if isinstance(tags, list):
        filtered = []
        for t in tags:
            if not isinstance(t, str):
                continue
            t = t.strip()
            for allowed in ALLOWED_STYLE_TAGS:
                if t.lower() == allowed.lower():
                    if allowed not in filtered:
                        filtered.append(allowed)
        cleaned["style_tags"] = filtered

    return cleaned


# =========================================================
# Orchestrator
# =========================================================

def scrape_product(url: str):
    print("\nSCRAPING:", url)
    raw = extract_raw_product_data(url)
    print("\nRAW DATA:", raw)
    print("\nCLEANING WITH GEMINI...\n")
    cleaned = clean_product_data(raw)
    return cleaned


if __name__ == "__main__":
    test_url = "https://www.size.co.uk/product/beige-new-balance-204l/19708453/"
    result = scrape_product(test_url)
    print("\nFINAL CLEANED DATA:\n", json.dumps(result, indent=2))
