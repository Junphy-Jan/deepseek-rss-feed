#!/usr/bin/env python3
"""
Generate unofficial RSS feeds from DeepSeek's public News pages.

Features:
- Chinese and English feeds
- Playwright-based scraping
- No dependency on generated CSS class names
- URL deduplication
- Stable GUIDs based on article URLs
- Language-aware browser contexts
- Fail-closed behavior: never overwrite an existing feed with empty data
- Atomic XML writes
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

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
        "locale": "zh-CN",
    },
    "en": {
        "source_url": f"{BASE_URL}/en/news/",
        "output": "deepseek_news_en_rss.xml",
        "title": "DeepSeek Research & News",
        "description": "Research and news from the official DeepSeek website.",
        "language": "en",
        "locale": "en-US",
    },
}


ZH_DATE_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

ISO_DATE_RE = re.compile(
    r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"
)


EN_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published: datetime
    description: str


def canonicalize_url(url: str) -> str:
    """
    Normalize an article URL.

    Removes:
    - fragments
    - query parameters
    - trailing slash
    """

    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )


def parse_date(text: str) -> datetime | None:
    """Parse Chinese, ISO, or English dates."""

    text = text.strip()

    # Chinese:
    # 2026 年 9 月 10 日
    match = ZH_DATE_RE.search(text)

    if match:
        year, month, day = map(int, match.groups())

        return datetime(
            year,
            month,
            day,
            tzinfo=timezone.utc,
        )

    # ISO:
    # 2026-09-10
    # 2026/09/10
    match = ISO_DATE_RE.search(text)

    if match:
        year, month, day = map(int, match.groups())

        return datetime(
            year,
            month,
            day,
            tzinfo=timezone.utc,
        )

    # English:
    # September 10, 2026
    if EN_DATE_RE.search(text):

        try:
            value = date_parser.parse(
                text,
                fuzzy=True,
            )

            if 2020 <= value.year <= datetime.now(
                timezone.utc
            ).year + 1:

                return value.replace(
                    tzinfo=timezone.utc
                )

        except (
            ValueError,
            OverflowError,
        ):
            pass

    return None


def clean_lines(text: str) -> list[str]:
    """Normalize visible text into non-empty lines."""

    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]


def pick_title_and_description(
    lines: list[str],
    date_line: str | None,
) -> tuple[str, str]:
    """
    Extract title and description from a news card.

    DeepSeek's frontend may contain labels such as "News" or "动态".
    These are ignored.
    """

    excluded = {
        "动态",
        "新闻",
        "News",
        "Product",
        "Research",
        "查看全部",
        "View all",
    }

    candidates = [
        line
        for line in lines
        if line != date_line
        and line not in excluded
    ]

    if not candidates:
        return "", ""

    title = candidates[0]

    description = " ".join(
        candidates[1:]
    ).strip()

    return (
        title,
        description or title,
    )


def is_article_url(
    url: str,
    source_url: str,
) -> bool:
    """Check whether a URL looks like a DeepSeek news article."""

    normalized = canonicalize_url(url)
    source = canonicalize_url(source_url)

    if normalized == source:
        return False

    parsed = urlparse(normalized)

    if parsed.netloc not in {
        "www.deepseek.com",
        "deepseek.com",
    }:
        return False

    return "/news/" in parsed.path


def looks_chinese(text: str) -> bool:
    """Return True if text contains a meaningful amount of CJK characters."""

    cjk = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff]",
        text,
    )

    return len(cjk) >= 2


def looks_english(text: str) -> bool:
    """Return True if text contains enough Latin alphabet characters."""

    latin = re.findall(
        r"[A-Za-z]",
        text,
    )

    return len(latin) >= 8


async def scrape(
    page: Page,
    source_url: str,
    language: str,
) -> list[Article]:
    """Scrape articles from a DeepSeek news index."""

    print()
    print(f"Fetching {source_url}")
    print(f"Expected language: {language}")

    await page.goto(
        source_url,
        wait_until="networkidle",
        timeout=60_000,
    )

    # Allow client-side rendering to settle.
    await page.wait_for_timeout(1000)

    articles: list[Article] = []
    seen_urls: set[str] = set()

    links = await page.locator(
        'a[href*="/news/"]'
    ).all()

    for link in links:

        try:
            href = await link.get_attribute(
                "href"
            )

            if not href:
                continue

            url = canonicalize_url(
                urljoin(
                    BASE_URL,
                    href,
                )
            )

            if not is_article_url(
                url,
                source_url,
            ):
                continue

            if url in seen_urls:
                continue

            text = (
                await link.inner_text()
            ).strip()

            if not text:
                continue

            lines = clean_lines(text)

            if not lines:
                continue

            date_line = next(
                (
                    line
                    for line in lines
                    if parse_date(line)
                ),
                None,
            )

            if not date_line:
                continue

            published = parse_date(
                date_line
            )

            if published is None:
                continue

            title, description = (
                pick_title_and_description(
                    lines,
                    date_line,
                )
            )

            if not title:
                continue

            seen_urls.add(url)

            articles.append(
                Article(
                    title=title,
                    url=url,
                    published=published,
                    description=description,
                )
            )

        except Exception as exc:
            print(
                f"Warning: failed to process link: {exc}"
            )

    # Deduplicate by URL.
    unique = {
        article.url: article
        for article in articles
    }

    result = sorted(
        unique.values(),
        key=lambda article: article.published,
        reverse=True,
    )[:MAX_ITEMS]

    if not result:
        raise RuntimeError(
            f"No articles found at {source_url}"
        )

    # Language validation.
    #
    # DeepSeek may sometimes return localized content
    # depending on browser language, cookies, or deployment.
    sample_text = " ".join(
        f"{article.title} {article.description}"
        for article in result[:5]
    )

    if language == "zh":
        if not looks_chinese(sample_text):
            raise RuntimeError(
                "The Chinese feed did not contain enough "
                "Chinese text. DeepSeek may have returned "
                "the English version."
            )

    elif language == "en":
        if not looks_english(sample_text):
            raise RuntimeError(
                "The English feed did not contain enough "
                "English text."
            )

    print(
        f"Found {len(result)} articles"
    )

    for article in result:
        print(
            f"  {article.published.date()}  "
            f"{article.title}"
        )

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
    """Generate RSS 2.0 XML."""

    feed = FeedGenerator()

    feed.id(self_url)

    feed.title(title)

    feed.link(
        href=source_url,
        rel="alternate",
    )

    feed.link(
        href=self_url,
        rel="self",
    )

    feed.description(description)

    feed.language(language)

    feed.generator(
        "deepseek-rss-feed"
    )

    for article in articles:

        entry = feed.add_entry()

        # feedgen's FeedEntry.id() does NOT accept
        # permalink=True in current releases.
        #
        # The article URL itself is a perfectly stable GUID.
        entry.id(article.url)

        entry.title(article.title)

        entry.link(
            href=article.url
        )

        entry.pubDate(
            article.published
        )

        entry.description(
            article.description
        )

    return feed.rss_str(
        pretty=True
    )


def atomic_write(
    path: Path,
    data: bytes,
) -> None:
    """
    Atomically replace a feed file.

    If the process crashes during writing,
    the previous valid RSS file remains intact.
    """

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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

    os.replace(
        temp_name,
        path,
    )


async def create_context(
    browser: Browser,
    locale: str,
):
    """
    Create an isolated browser context.

    A separate context prevents cookies or locale state from
    leaking between the Chinese and English feeds.
    """

    return await browser.new_context(
        locale=locale,
        timezone_id="UTC",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": (
                f"{locale},"
                f"{locale.split('-')[0]};q=0.9,"
                "en;q=0.7"
            )
        },
    )


async def run() -> None:
    """Main entry point."""

    repo = os.environ.get(
        "GITHUB_REPOSITORY",
        "YOUR_USERNAME/deepseek-rss-feed",
    )

    branch = os.environ.get(
        "GITHUB_REF_NAME",
        "main",
    )

    raw_base = os.environ.get(
        "RAW_BASE",
        (
            "https://raw.githubusercontent.com/"
            f"{repo}/{branch}"
        ),
    )

    print()
    print("=" * 60)
    print("DeepSeek RSS Generator")
    print("=" * 60)
    print()

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        try:

            # ---------------------------------------------------------
            # Chinese feed
            # ---------------------------------------------------------

            zh_context = await create_context(
                browser,
                FEEDS["zh"]["locale"],
            )

            try:

                zh_page = await zh_context.new_page()

                try:

                    zh_articles = await scrape(
                        zh_page,
                        FEEDS["zh"]["source_url"],
                        "zh",
                    )

                finally:
                    await zh_page.close()

            finally:
                await zh_context.close()

            zh_self_url = (
                f"{raw_base}/"
                f"{FEEDS['zh']['output']}"
            )

            zh_rss = generate_feed(
                zh_articles,
                title=FEEDS["zh"]["title"],
                description=FEEDS["zh"]["description"],
                language=FEEDS["zh"]["language"],
                source_url=FEEDS["zh"]["source_url"],
                self_url=zh_self_url,
            )

            atomic_write(
                Path(
                    FEEDS["zh"]["output"]
                ),
                zh_rss,
            )

            print(
                f"Wrote {FEEDS['zh']['output']}"
            )

            # ---------------------------------------------------------
            # English feed
            # ---------------------------------------------------------

            en_context = await create_context(
                browser,
                FEEDS["en"]["locale"],
            )

            try:

                en_page = await en_context.new_page()

                try:

                    en_articles = await scrape(
                        en_page,
                        FEEDS["en"]["source_url"],
                        "en",
                    )

                finally:
                    await en_page.close()

            finally:
                await en_context.close()

            en_self_url = (
                f"{raw_base}/"
                f"{FEEDS['en']['output']}"
            )

            en_rss = generate_feed(
                en_articles,
                title=FEEDS["en"]["title"],
                description=FEEDS["en"]["description"],
                language=FEEDS["en"]["language"],
                source_url=FEEDS["en"]["source_url"],
                self_url=en_self_url,
            )

            atomic_write(
                Path(
                    FEEDS["en"]["output"]
                ),
                en_rss,
            )

            print(
                f"Wrote {FEEDS['en']['output']}"
            )

        finally:
            await browser.close()

    print()
    print("=" * 60)
    print("RSS generation completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
