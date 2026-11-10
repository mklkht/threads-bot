"""A foreign observation used only as a nerve to retell in Ukrainian.

Titles come from public Reddit listings in other places. They are not quoted
in the post and are not news headlines.
"""

from __future__ import annotations

import logging
import random
import re

import httpx

logger = logging.getLogger("sparks")

# (subreddit, where it is from). Life talk, not news desks.
PLACES: tuple[tuple[str, str], ...] = (
    ("CasualUK", "Британія"),
    ("brasil", "Бразилія"),
    ("india", "Індія"),
    ("Philippines", "Філіппіни"),
    ("korea", "Корея"),
    ("mexico", "Мексика"),
    ("australia", "Австралія"),
    ("japanlife", "Японія"),
    ("AskEurope", "Європа"),
    ("CasualConversation", "чужа кухня"),
    ("unpopularopinion", "чужа сварка"),
    ("Showerthoughts", "чужа думка"),
)

_SKIP = re.compile(
    r"https?://|\[removed\]|\[deleted\]|\b(breaking|killed|shooting|war|election)\b",
    re.IGNORECASE,
)


def titles_from_listing(payload: object) -> list[str]:
    """Pull ordinary post titles out of a Reddit listing."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    if not isinstance(children, list):
        return []
    titles: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        post = child.get("data")
        if not isinstance(post, dict):
            continue
        if post.get("stickied") or post.get("over_18"):
            continue
        title = " ".join(str(post.get("title") or "").split())
        if _usable(title):
            titles.append(title)
    return titles


def _usable(title: str) -> bool:
    if len(title) < 40 or len(title) > 180:
        return False
    return _SKIP.search(title) is None


async def fetch_spark(client: httpx.AsyncClient | None = None) -> str | None:
    """One title from a random far-away listing, or None if the listing does not answer."""
    sub, place = random.choice(PLACES)
    url = f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=25"
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0))
    try:
        response = await http.get(url, headers={"User-Agent": "threads-bot/1.0"})
        if response.status_code >= 400:
            logger.info("spark listing %s status=%s", sub, response.status_code)
            return None
        titles = titles_from_listing(response.json())
    except (httpx.HTTPError, ValueError):
        logger.info("spark listing %s failed", sub)
        return None
    finally:
        if owns_client:
            await http.aclose()
    if not titles:
        return None
    return f"{place}: {random.choice(titles)}"
