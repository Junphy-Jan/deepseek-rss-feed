#!/usr/bin/env python3
"""Generate unofficial RSS feeds from DeepSeek's public News pages.

The scraper deliberately relies on semantic URL/date/title patterns instead of
DeepSeek's generated CSS class names, which makes it more tolerant of frontend
refactors.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator
from playwright.async_api import Browser, Page, async_playwright


BASE_URL = "https://www.deepseek.com"
MAX_ITEMS = 50

FEEDS = {
    "zh": {
        "source_url": f"{BASE_URL}/news/",
        "output": "deepseek_news_zh_rss.xml",
        "title": "DeepSeek 研究与动态",
        "description": "DeepSeek 官方网站的研究与动态。",
        "language": "zh-CN",
    },
    "en": {
        "source_url": f"{BASE_URL}/en/news/",
        "output": "deepseek_news_en_rss.xml",
        "title": "DeepSeek Research & News",
        "description": "Research and news from the official DeepSeek website.",
        "language": "en",
    },
}

# DeepSeek currently exposes dates in formats such as:
#   2026 年 9 月 10 日
#   September 10, 2026
ZH_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ISO_DATE_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published: datetime
    description: str


def parse_date(text: str) -> datetime | None:
    """Parse a date from Chinese, ISO, or English text."""
    match = ZH_DATE_RE.search(text)
    if match:
        year, month, day = map(int, match.groups())
        return datetime(year, month, day, tzinfo=timezone.utc)

    match = ISO_DATE_RE.search(text)
    if match:
        year, month, day = map(int, match.groups())
        return datetime(year, month, day, tzinfo=timezone.utc)

    # English dates are handled by dateutil, but only after requiring a
    # recognizable month name to avoid interpreting arbitrary text as dates.
    if re.search(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\b",
        text,
        re.IGNORECASE,
    ):
        try:
            value = date_parser.parse(text, fuzzy=True)
            if 2020 <= value.year <= datetime.now(timezone.utc).year + 1:
                return value.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass

    return None


def clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def pick_title_and_description(lines: list[str], date_line: str | None) -> tuple[str, str]:
    """Extract title and description from a news-card's text."""
    excluded = {
        "动态", "新闻", "News", "Product", "Research",
        "查看全部", "View all",
    }

    candidates = [
        line for line in lines
        if line != date_line and line not in excluded
    ]

    if not candidates:
        return "", ""

    # On the current DeepSeek page, the first candidate is the card title and
    # the following candidate(s) are its summary. Keep the summary compact.
    title = candidates[0]
    description = " ".join(candidates[1:]).strip()
    return title, description


async def scrape(page: Page, source_url: str) -> list[Article]:
    print(f"Fetching {source_url}")
    await page.goto(source_url, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(800)

    articles: list[Article] = []
    seen: set[str] = set()

    # Use the URL shape as the primary selector. This avoids generated class
    # names while excluding external research-index links.
    links = await page.locator('a[href*="/news/"]').all()

    for link in links:
        href = await link.get_attribute("href")
        if not href:
            continue

        url = urljoin(BASE_URL, href).split("#", 1)[0]

        # Accept article pages, but not the index itself.
        normalized = url.rstrip("/")
        if normalized in {
            f"{BASE_URL}/news",
            f"{BASE_URL}/en/news",
        }:
            continue

        if url in seen:
            continue

        text = (await link.inner_text()).strip()
        lines = clean_lines(text)
        if not lines:
            continue

        date_line = next((line for line in lines if parse_date(line)), None)
        if not date_line:
            continue

        published = parse_date(date_line)
        if published is None:
            continue

        title, description = pick_title_and_description(lines, date_line)
        if not title:
            continue

        seen.add(url)
        articles.append(
            Article(
                title=title,
                url=url,
                published=published,
                description=description or title,
            )
        )

    # Deduplicate by URL, sort newest first, and cap feed size.
    unique: dict[str, Article] = {article.url: article for article in articles}
    result = sorted(
        unique.values(),
        key=lambda article: article.published,
        reverse=True,
    )[:MAX_ITEMS]

    print(f"Found {len(result)} articles")
    for article in result:
        print(f"  {article.published.date()}  {article.title}")

    return result


def generate_feed(
    articles: list[Article],
    *,
    title: str,
    description: str,
    language: str,
    source_url: str,
    self_url: str,
) -> bytes:
    fg = FeedGenerator()
    fg.id(self_url)
    fg.title(title)
    fg.link(href=source_url, rel="alternate")
    fg.link(href=self_url, rel="self")
    fg.description(description)
    fg.language(language)
    fg.generator("deepseek-rss-feed")

    for article in articles:
        entry = fg.add_entry()
        entry.id(article.url, permalink=True)
        entry.title(article.title)
        entry.link(href=article.url)
        entry.pubDate(article.published)
        entry.description(article.description)

    return fg.rss_str(pretty=True)


def atomic_write(path: Path, data: bytes) -> None:
    """Write a file atomically so a failed run cannot leave truncated XML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name

    os.replace(temp_name, path)


async def run() -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "YOUR_USERNAME/deepseek-rss-feed")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    raw_base = os.environ.get(
        "RAW_BASE",
        f"https://raw.githubusercontent.com/{repo}/{branch}",
    )

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            ),
        )

        try:
            for config in FEEDS.values():
                page = await context.new_page()
                try:
                    articles = await scrape(page, config["source_url"])
                finally:
                    await page.close()

                # Fail closed: never overwrite a previously good feed with an
                # empty result caused by a site outage or DOM change.
                if not articles:
                    raise RuntimeError(
                        f"No articles found at {config['source_url']}. "
                        "The site may be unavailable or its HTML structure changed."
                    )

                self_url = f"{raw_base}/{config['output']}"
                rss = generate_feed(
                    articles,
                    title=config["title"],
                    description=config["description"],
                    language=config["language"],
                    source_url=config["source_url"],
                    self_url=self_url,
                )

                atomic_write(Path(config["output"]), rss)
                print(f"Wrote {config['output']}")
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
