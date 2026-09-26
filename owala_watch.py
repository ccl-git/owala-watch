#!/usr/bin/env python3
"""
Restock watcher for Finders (Shopline) product pages.

Reads the product JSON embedded in the page (app.value('product', JSON.parse('...')))
and checks the stock of one color variation. Sends a push notification via ntfy
when the variation changes from sold out to available.

Usage:
  python3 owala_watch.py --once            # single check (for cron / systemd timer)
  python3 owala_watch.py --interval 120    # loop forever, check every ~120 s
  python3 owala_watch.py --list            # list all variations and their stock
  python3 owala_watch.py --test-notify     # send a test push notification
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request

DEFAULT_URL = "https://www.finders.com.tw/products/owala-freesip-tritan-25oz"
DEFAULT_COLOR = "無人島"  # zh-hant name; English name is "Open Air"
STATE_FILE = os.path.expanduser(os.environ.get("STATE_FILE", "~/.cache/owala_watch_state.json"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

PRODUCT_RE = re.compile(r"app\.value\('product',\s*JSON\.parse\('(.*?)'\)\);", re.S)


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_product(html):
    m = PRODUCT_RE.search(html)
    if not m:
        raise ValueError("Embedded product JSON not found (page layout may have changed)")
    raw = m.group(1).replace("\\'", "'")
    # The payload is a JS single-quoted string literal; decode it as a JSON string first.
    text = json.loads('"' + raw + '"')
    return json.loads(text)


def variation_name(v):
    ft = v.get("fields_translations") or {}
    names = []
    for lang in ("zh-hant", "en"):
        if ft.get(lang):
            names.append("/".join(ft[lang]))
    return " | ".join(names) or v.get("key", "?")


def is_available(product, v):
    if product.get("unlimited_quantity"):
        return True
    if product.get("out_of_stock_orderable"):
        return True  # store allows ordering while out of stock (preorder)
    return (v.get("quantity") or 0) > 0


def find_variation(product, color):
    c = color.strip().lower()
    for v in product.get("variations", []):
        ft = v.get("fields_translations") or {}
        candidates = [v.get("key", "")]
        for vals in ft.values():
            candidates.extend(vals)
        if any(c == str(x).strip().lower() for x in candidates):
            return v
    return None


def notify(topic, title, message, click_url=None, priority="urgent"):
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": "droplet,shopping_cart",
    }
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    d = os.path.dirname(STATE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def check(args):
    product = parse_product(fetch_html(args.url))
    v = find_variation(product, args.color)
    if v is None:
        names = ", ".join(variation_name(x) for x in product.get("variations", []))
        raise ValueError(f"Color '{args.color}' not found. Available: {names}")

    avail = is_available(product, v)
    qty = v.get("quantity")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {variation_name(v)}: quantity={qty} available={avail}", flush=True)

    state = load_state()
    key = f"{args.url}#{v.get('key')}"
    was_avail = state.get(key, False)
    if avail and not was_avail:
        title = "Owala restocked!"
        msg = f"{variation_name(v)} is in stock (qty {qty}). Go buy it now!"
        notify(args.topic, title, msg, click_url=args.url)
        print(f"[{ts}] Notification sent to ntfy topic '{args.topic}'", flush=True)
    elif not avail and was_avail and args.notify_soldout:
        notify(args.topic, "Owala sold out again", f"{variation_name(v)} is sold out.",
               click_url=args.url, priority="default")
    state[key] = avail
    save_state(state)
    return avail


def main():
    ap = argparse.ArgumentParser(description="Watch a Finders (Shopline) product color for restock.")
    ap.add_argument("--url", default=DEFAULT_URL, help="Product page URL")
    ap.add_argument("--color", default=DEFAULT_COLOR, help="Color name (zh-hant or en) or variation key")
    ap.add_argument("--topic", default=os.environ.get("NTFY_TOPIC"),
                    help="ntfy topic name (or set NTFY_TOPIC env var)")
    ap.add_argument("--interval", type=int, default=120, help="Seconds between checks in loop mode")
    ap.add_argument("--once", action="store_true", help="Check once and exit")
    ap.add_argument("--list", action="store_true", help="List all variations with stock and exit")
    ap.add_argument("--test-notify", action="store_true", help="Send a test notification and exit")
    ap.add_argument("--notify-soldout", action="store_true", help="Also notify when it sells out again")
    args = ap.parse_args()

    if args.list:
        product = parse_product(fetch_html(args.url))
        for v in product.get("variations", []):
            print(f"{variation_name(v):30s} qty={v.get('quantity')}  key={v.get('key')}")
        return

    if not args.topic:
        sys.exit("Error: set --topic or NTFY_TOPIC (use a long random name, e.g. owala-3f9k2x7q)")

    if args.test_notify:
        notify(args.topic, "Owala watcher test", "Test notification OK", click_url=args.url,
               priority="default")
        print("Test notification sent.")
        return

    if args.once:
        # Retry a few times so a single network hiccup does not fail the run
        for attempt in range(3):
            try:
                check(args)
                return
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}", file=sys.stderr, flush=True)
                time.sleep(10)
        sys.exit(1)

    while True:
        try:
            check(args)
        except Exception as e:  # keep looping on network / parse errors
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr, flush=True)
        # Add jitter so requests are not perfectly periodic
        time.sleep(args.interval + random.randint(0, max(1, args.interval // 4)))


if __name__ == "__main__":
    main()
