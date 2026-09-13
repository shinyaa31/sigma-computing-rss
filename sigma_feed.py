#!/usr/bin/env python3
"""sitemap.xmlからSigma ComputingブログのRSSフィードを生成します。

公式RSSが存在しないため、sitemap.xmlをURLの発見源として使い、
新規に見つかったページだけを取得してOGPメタ情報からRSSアイテムを組み立てます。

使い方:
    python sigma_blog_feed.py --seed --bootstrap 20   # 初回: 既存URLを既知扱いにし、直近20件だけ記事化
    python sigma_blog_feed.py                          # 2回目以降: 新着だけを追記
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- 設定

SITEMAP_URL = "https://www.sigmacomputing.com/sitemap.xml"
SITE_BASE = "https://www.sigmacomputing.com"

# フィードに含めたいパスのプレフィックス。
# 例: ("/blog/", "/resources/announcements/") のように増やせます。
PATH_PREFIXES = (
    "/blog/",
    "/resources/announcements/",           # プレスリリース
    "/resources/webinars-and-events/",     # ウェビナー・イベント
    "/product-launch/",                    # 製品リリース
)

FEED_TITLE = "Sigma Computing Blog (unofficial)"
FEED_DESCRIPTION = "sitemap.xmlから生成した非公式RSSフィードです。"
FEED_LINK = f"{SITE_BASE}/blog"
# GitHub Pagesなどで公開する場合の自URL。atom:link rel=selfに使います。
FEED_SELF_URL = os.environ.get("FEED_SELF_URL", "https://example.github.io/sigma-blog-rss/feed.xml")

MAX_ITEMS = 50            # フィードに残す最大件数
MAX_FETCH_PER_RUN = 25    # 1回の実行で本文取得する最大件数(暴走防止)
FETCH_INTERVAL = 1.0      # 取得間隔(秒)
USER_AGENT = "sigma-blog-rss/1.0 (+personal feed generator)"
TIMEOUT = 30

MONTHS = (
    "January February March April May June July "
    "August September October November December"
).split()
DATE_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})\b")

# ---------------------------------------------------------------- ユーティリティ


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def http_get(url: str) -> str:
    res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    res.raise_for_status()
    res.encoding = res.encoding or "utf-8"
    return res.text


def load_sitemap(source: str) -> list[dict]:
    """sitemapを読み、対象プレフィックスのURLだけを返します。"""
    local = Path(source)
    xml = local.read_text(encoding="utf-8") if local.exists() else http_get(source)

    soup = BeautifulSoup(xml, "xml")
    entries = []
    for node in soup.find_all("url"):
        loc_node = node.find("loc")
        if loc_node is None:
            continue
        loc = loc_node.get_text(strip=True)
        path = loc.replace(SITE_BASE, "").replace("https://sigmacomputing.com", "")
        if not path.startswith(PATH_PREFIXES):
            continue
        # 一覧ページ自体は除外
        if path.rstrip("/") in {"/blog"}:
            continue
        lastmod_node = node.find("lastmod")
        entries.append(
            {
                "url": normalize_url(loc),
                "lastmod": lastmod_node.get_text(strip=True) if lastmod_node else None,
            }
        )
    return entries


def normalize_url(url: str) -> str:
    """sitemapにwwwあり/なしが混在しうるので揃えます。"""
    return url.replace("https://sigmacomputing.com", SITE_BASE).rstrip("/")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def slug_to_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize()


# ---------------------------------------------------------------- ページ解析


def parse_page(html: str, url: str) -> dict:
    """記事ページからRSSアイテムに必要な情報を抜き出します。"""
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    title = meta("og:title")
    if not title and soup.title:
        title = soup.title.get_text(strip=True).removesuffix(" | Sigma").strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else slug_to_title(url)

    description = meta("og:description") or meta("description") or ""
    image = meta("og:image")

    # 本文中の最初の "September 9, 2026" 形式を公開日とみなします。
    # 記事ヘッダの日付が関連記事より前に現れるため、最初のマッチで概ね正しくなります。
    published = None
    body = soup.find("main") or soup.body or soup
    match = DATE_RE.search(body.get_text(" ", strip=True))
    if match:
        month = MONTHS.index(match.group(1)) + 1
        published = datetime(int(match.group(3)), month, int(match.group(2)), tzinfo=timezone.utc)

    # h1の直前に出るカテゴリラベル(Product / Fundamentals など)を拾います。
    category = None
    h1 = soup.find("h1")
    if h1:
        for prev in h1.find_all_previous(string=True, limit=40):
            text = prev.strip()
            if 2 < len(text) <= 30 and not DATE_RE.search(text) and text != title:
                category = text
                break

    return {
        "title": title,
        "description": description,
        "image": image,
        "published": published.isoformat() if published else None,
        "category": category,
    }


def build_item(entry: dict, fetch: bool) -> dict:
    """1URL分のRSSアイテムを組み立てます。取得失敗時もURLだけで成立させます。"""
    url = entry["url"]
    now = datetime.now(timezone.utc)
    item = {
        "url": url,
        "title": slug_to_title(url),
        "description": "",
        "image": None,
        "category": None,
        "first_seen": now.isoformat(),
    }

    if fetch:
        try:
            item.update(parse_page(http_get(url), url))
        except Exception as exc:  # 取得失敗は握りつぶしてURLだけで出す
            log(f"  ! 取得失敗: {url} ({exc})")

    # 公開日の決定順: ページ内の日付 > sitemapのlastmod > 初回検出時刻
    published = (
        parse_iso(item.get("published"))
        or parse_iso(entry.get("lastmod"))
        or now
    )
    # 未来日付はフィードリーダーが嫌うので丸めます
    item["pub_date"] = min(published, now).isoformat()
    item.pop("published", None)
    return item


# ---------------------------------------------------------------- RSS出力


def render_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{escape(FEED_TITLE)}</title>",
        f"    <link>{escape(FEED_LINK)}</link>",
        f"    <description>{escape(FEED_DESCRIPTION)}</description>",
        "    <language>en</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        f'    <atom:link href="{escape(FEED_SELF_URL)}" rel="self" type="application/rss+xml"/>',
    ]

    for item in items:
        pub = parse_iso(item["pub_date"]) or datetime.now(timezone.utc)
        lines += [
            "    <item>",
            f"      <title>{escape(item['title'])}</title>",
            f"      <link>{escape(item['url'])}</link>",
            f"      <guid isPermaLink=\"true\">{escape(item['url'])}</guid>",
            f"      <pubDate>{format_datetime(pub)}</pubDate>",
        ]
        if item.get("description"):
            lines.append(f"      <description>{escape(item['description'])}</description>")
        if item.get("category"):
            lines.append(f"      <category>{escape(item['category'])}</category>")
        if item.get("image"):
            lines.append(
                f'      <enclosure url="{escape(item["image"])}" type="image/png" length="0"/>'
            )
        lines.append("    </item>")

    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- メイン


def main() -> int:
    parser = argparse.ArgumentParser(description="sitemap.xmlからRSSを生成します")
    parser.add_argument("--sitemap", default=SITEMAP_URL, help="sitemapのURLまたはローカルパス")
    parser.add_argument("--state", default="state.json", help="既知URLを記録するJSON")
    parser.add_argument("--out", default="feed.xml", help="出力するRSSファイル")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="初回用。既存URLを全て既知扱いにし、--bootstrap件だけ記事化します",
    )
    parser.add_argument("--bootstrap", type=int, default=20, help="--seed時に記事化する件数")
    parser.add_argument("--no-fetch", action="store_true", help="本文を取得せずURLだけで生成")
    args = parser.parse_args()

    state_path = Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    items: dict[str, dict] = state.get("items", {})
    known: set[str] = set(state.get("known", [])) | set(items)

    entries = load_sitemap(args.sitemap)
    log(f"sitemap: 対象 {len(entries)} 件 / 既知 {len(known)} 件")

    new_entries = [e for e in entries if e["url"] not in known]
    log(f"新規: {len(new_entries)} 件")

    if args.seed:
        # lastmodが新しい順に--bootstrap件だけ記事化し、残りは既知として登録
        new_entries.sort(key=lambda e: e.get("lastmod") or "", reverse=True)
        targets = new_entries[: args.bootstrap]
    else:
        targets = new_entries[:MAX_FETCH_PER_RUN]
        if len(new_entries) > MAX_FETCH_PER_RUN:
            log(f"  (今回は {MAX_FETCH_PER_RUN} 件までに制限します)")

    for index, entry in enumerate(targets):
        log(f"  + [{index + 1}/{len(targets)}] {entry['url']}")
        items[entry["url"]] = build_item(entry, fetch=not args.no_fetch)
        if index < len(targets) - 1:
            time.sleep(FETCH_INTERVAL)

    # 記事化しなかった新規URLも既知として記録(次回以降フィードに流れない)
    known |= {e["url"] for e in entries}

    ordered = sorted(items.values(), key=lambda i: i["pub_date"], reverse=True)[:MAX_ITEMS]
    items = {i["url"]: i for i in ordered}

    Path(args.out).write_text(render_rss(ordered), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {"known": sorted(known), "items": items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"出力: {args.out} ({len(ordered)} items) / {args.state} (known {len(known)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
