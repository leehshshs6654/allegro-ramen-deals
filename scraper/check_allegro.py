import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
OFFERS_PATH = BASE_DIR / "public" / "offers.json"
SEEN_PATH = BASE_DIR / "seen_hotdeals.json"


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def should_run_now(config):
    timezone = config.get("timezone", "Europe/Budapest")
    run_hours = set(config.get("run_hours", [3, 9, 15, 21]))
    now = datetime.now(ZoneInfo(timezone))
    if len(sys.argv) > 1 and sys.argv[1] == "--force":
        return True, now
    return now.hour in run_hours, now


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def extract_price_huf(text):
    text = str(text).replace("\xa0", " ")
    matches = re.findall(r"(\d[\d\s]{1,10})\s*(?:Ft|HUF)", text, re.IGNORECASE)
    prices = []
    for match in matches:
        number = match.replace(" ", "")
        try:
            value = int(number)
            if 100 <= value <= 200000:
                prices.append(value)
        except ValueError:
            continue
    if not prices:
        return None
    return min(prices)


def extract_pack_count(text):
    text = normalize_text(text)
    patterns = [
        r"(\d+)\s*x\s*\d+",
        r"(\d+)\s*×\s*\d+",
        r"(\d+)\s*-\s*pack",
        r"(\d+)\s*pack",
        r"(\d+)\s*pcs",
        r"(\d+)\s*pieces",
        r"(\d+)\s*db",
        r"(\d+)\s*darab",
        r"(\d+)\s*csomag",
        r"(\d+)\s*tasak",
        r"(\d+)\s*zacskó",
        r"(\d+)\s*bags",
        r"(\d+)\s*bag",
        r"carton\s*(\d+)",
        r"box\s*of\s*(\d+)",
        r"karton\s*(\d+)",
        r"teljes\s*karton\s*(\d+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            count = int(match.group(1))
            if 1 <= count <= 100:
                return count
    return 1


def matches_product(text, product):
    text = normalize_text(text)
    for word in product.get("must_include", []):
        if normalize_text(word) not in text:
            return False
    for word in product.get("exclude", []):
        if normalize_text(word) in text:
            return False
    return True


def clean_title(text):
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    bad_words = [
        "hozzáadás", "összehasonlításhoz", "szállítás", "tanúsítvány",
        "szuper eladó", "információk", "már ennyiért", "kosárba",
        "értékelés", "delivery", "compare", "add to"
    ]
    candidates = []
    for line in lines:
        lower = line.lower()
        if any(bad in lower for bad in bad_words):
            continue
        if "ft" in lower or "huf" in lower:
            continue
        if len(line) < 5:
            continue
        candidates.append(line)
    if candidates:
        return candidates[0][:220]
    if lines:
        return lines[0][:220]
    return "Unknown product"


def make_full_url(href):
    href = str(href)
    if href.startswith("http"):
        return href.split("?")[0]
    if href.startswith("/"):
        return "https://allegro.hu" + href.split("?")[0]
    return href.split("?")[0]


def get_offer_text_from_anchor(anchor):
    js = """
    element => {
        let node = element;
        let best = element.innerText || element.textContent || "";
        for (let i = 0; i < 8; i++) {
            if (!node.parentElement) break;
            node = node.parentElement;
            const txt = node.innerText || node.textContent || "";
            if (txt.includes("Ft") || txt.includes("HUF")) {
                return txt;
            }
            if (txt.length > best.length && txt.length < 2000) {
                best = txt;
            }
        }
        return best;
    }
    """
    try:
        return anchor.evaluate(js)
    except Exception:
        try:
            return anchor.inner_text(timeout=2000)
        except Exception:
            return ""


def try_load_search_page(page, query):
    encoded = quote_plus(query)
    urls = [
        f"https://allegro.hu/kereses?string={encoded}",
        f"https://allegro.hu/listing?string={encoded}",
        f"https://allegro.hu/kategoria/keszetelek-levesek-112612?string={encoded}"
    ]
    for url in urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # 사람처럼 랜덤 딜레이
            time.sleep(3 + (hash(query) % 3))
            return url
        except Exception as error:
            print(f"Page load failed: {url} / {error}")
    return None


def save_debug_files(page, query):
    debug_dir = BASE_DIR / "debug"
    debug_dir.mkdir(exist_ok=True)
    safe_query = re.sub(r"[^a-zA-Z0-9_-]+", "_", query)
    html_path = debug_dir / f"{safe_query}.html"
    png_path = debug_dir / f"{safe_query}.png"
    html = page.content()
    html_path.write_text(html, encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=True)
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""
    print("Body text sample:")
    print(body_text[:1200])
    print(f"Saved debug HTML: {html_path}")
    print(f"Saved debug screenshot: {png_path}")


def scrape_query(page, query, product):
    loaded_url = try_load_search_page(page, query)
    if not loaded_url:
        print(f"Search failed completely: {query}")
        return []

    print(f"Loaded URL: {loaded_url}")

    try:
        title = page.title()
        print(f"Page title: {title}")
    except Exception:
        pass

    offers = []
    anchors = page.locator('a[href*="/termek/"]').all()
    print(f"Found product links: {len(anchors)}")

    if len(anchors) == 0:
        try:
            save_debug_files(page, query)
        except Exception as error:
            print(f"Could not save debug files: {error}")
        return []

    seen_urls = set()

    for anchor in anchors[:80]:
        try:
            href = anchor.get_attribute("href")
            if not href or "/termek/" not in href:
                continue
            url = make_full_url(href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            raw_text = get_offer_text_from_anchor(anchor)
            if not raw_text:
                continue
            if not matches_product(raw_text, product):
                continue

            price = extract_price_huf(raw_text)
            if price is None:
                continue

            pack_count = extract_pack_count(raw_text)
            unit_price = round(price / pack_count)
            title = clean_title(raw_text)

            offers.append({
                "product": product["name"],
                "title": title,
                "price": price,
                "pack_count": pack_count,
                "unit_price": unit_price,
                "url": url
            })

        except Exception as error:
            print(f"Product link parse failed: {error}")
            continue

    print(f"Matched offers: {len(offers)}")
    return offers


def deduplicate_offers(offers):
    seen = set()
    unique = []
    for offer in sorted(offers, key=lambda x: x["unit_price"]):
        key = offer["url"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)
    return unique


def send_telegram_hotdeal(offer, chat_id, bot_token):
    if not bot_token or not chat_id:
        return
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = (
        "🔥 라면 핫딜 발견\n\n"
        f"{offer['product']}\n"
        f"{offer['title']}\n\n"
        f"총 가격: {offer['price']} Ft\n"
        f"수량 추정: {offer['pack_count']}개\n"
        f"개당가: {offer['unit_price']} Ft\n\n"
        f"{offer['url']}"
    )
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False
    }
    response = requests.post(api_url, json=payload, timeout=20)
    response.raise_for_status()


def main():
    config = load_json(CONFIG_PATH, {})
    should_run, now = should_run_now(config)

    if not should_run:
        print(f"Skip. Budapest time now: {now.isoformat()}")
        return

    display_max = config.get("display_max_price", 500)
    alert_max = config.get("alert_max_price", 300)

    all_offers = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1366,768",
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="hu-HU",
            timezone_id="Europe/Budapest",
            # 실제 브라우저처럼 보이게
            java_script_enabled=True,
            accept_downloads=False,
            extra_http_headers={
                "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            }
        )

        page = context.new_page()

        # 핵심: stealth 적용
        stealth_sync(page)

        # webdriver 속성 숨기기
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['hu-HU', 'hu', 'en'] });
            window.chrome = { runtime: {} };
        """)

        for product in config.get("products", []):
            for query in product.get("queries", []):
                print(f"Searching: {product['name']} / {query}")
                try:
                    offers = scrape_query(page, query, product)
                    all_offers.extend(offers)
                except Exception as error:
                    print(f"Search failed: {query} / {error}")

        browser.close()

    all_offers = deduplicate_offers(all_offers)

    visible_offers = [
        offer for offer in all_offers
        if offer["unit_price"] < display_max
    ]

    checked_at = now.strftime("%Y-%m-%d %H:%M Europe/Budapest")

    for offer in visible_offers:
        offer["checked_at"] = checked_at
        offer["status"] = "hot" if offer["unit_price"] < alert_max else "show"

    save_json(OFFERS_PATH, visible_offers)

    seen_hotdeals = set(load_json(SEEN_PATH, []))
    new_seen_hotdeals = set(seen_hotdeals)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    for offer in visible_offers:
        if offer["unit_price"] >= alert_max:
            continue
        hotdeal_key = f"{offer['url']}|{offer['unit_price']}"
        if hotdeal_key in seen_hotdeals:
            continue
        try:
            send_telegram_hotdeal(offer, chat_id, bot_token)
            new_seen_hotdeals.add(hotdeal_key)
            print(f"Hotdeal sent: {offer['title']}")
        except Exception as error:
            print(f"Telegram send failed: {error}")

    save_json(SEEN_PATH, sorted(new_seen_hotdeals))

    print(f"Total scraped offers: {len(all_offers)}")
    print(f"Done. Visible offers: {len(visible_offers)}")


if __name__ == "__main__":
    main()
