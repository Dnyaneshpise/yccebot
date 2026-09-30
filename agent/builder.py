"""Browser automation for AWS Builder Center.

All DOM selectors here are best-effort. The live Builder Center markup changes
without notice, so every lookup falls back to plain-text parsing and finally to
``None`` rather than raising or guessing. Nothing in this module publishes,
likes, or comments on anything - it only reads.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from .rewards import parse_badge_count

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, BrowserContext, Page

DEFAULT_TIMEOUT_MS = 45000
NAV_TIMEOUT_MS = 60000
# Body text longer than this is treated as an app shell, not a dashboard.
MIN_USEFUL_TEXT = 120


class AuthExpiredError(RuntimeError):
    """Raised when the persisted Builder Center session is no longer valid."""


class BuilderCenter:
    """Read-only client for the authenticated AWS Builder Center UI."""

    def __init__(
        self,
        base_url: str = "https://builder.aws.com/",
        headless: bool = True,
        user_agent: str | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.headless = headless
        self.user_agent = user_agent
        self._playwright: Any = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def initialize(self, storage_state_path: str | None = None) -> None:
        """Launch Chromium, restoring cookies from a Playwright storage state.

        The state file is only loaded when it exists on disk; a missing file is
        reported clearly instead of silently running logged-out.
        """
        from playwright.async_api import async_playwright

        state_file = Path(storage_state_path) if storage_state_path else None
        if state_file is not None and not state_file.exists():
            raise AuthExpiredError(
                f"Storage state file not found: {state_file}. "
                "Run scripts/save_login_state.py locally, then scripts/encode_state.py."
            )

        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context_options: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
        }
        if state_file is not None:
            context_options["storage_state"] = str(state_file)
        if self.user_agent:
            context_options["user_agent"] = self.user_agent

        self.context = await self.browser.new_context(**context_options)
        self.context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        self.page = await self.context.new_page()

    async def close(self) -> None:
        """Close the browser and stop Playwright, ignoring teardown errors."""
        for closer in (
            getattr(self.context, "close", None),
            getattr(self.browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None

    async def __aenter__(self) -> "BuilderCenter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    async def goto(self, url: str, wait_for: str | None = None) -> bool:
        """Navigate to an absolute or site-relative URL."""
        if self.page is None:
            return False
        target = url if url.startswith("http") else urljoin(self.base_url, url)
        try:
            await self.page.goto(target, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"Navigation to {target} failed: {type(exc).__name__}")
            return False

        if wait_for:
            try:
                await self.page.wait_for_selector(wait_for, timeout=20000)
            except Exception:
                pass
        await self._settle()
        return True

    async def _settle(self, ms: int = 2000) -> None:
        """Give client-side rendering a moment before reading the DOM."""
        if self.page is None:
            return
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        try:
            await self.page.wait_for_timeout(ms)
        except Exception:
            pass

    async def is_authenticated(self) -> bool:
        """Return True when the page shows an authenticated Builder Center UI.

        Checks for logout controls / avatar affordances, and treats visible
        "sign in" prompts as logged-out. Falls back to cookie heuristics.
        """
        if self.page is None:
            return False

        await self._settle(1500)

        logged_out_selectors = (
            "text=Sign in >> nth=0",
            "button:has-text('Sign in')",
            "a:has-text('Sign in')",
        )
        for selector in logged_out_selectors:
            try:
                if await self.page.locator(selector).count() > 0:
                    return False
            except Exception:
                continue

        logged_in_selectors = (
            "text=Log out",
            "text=Sign out",
            "text=My profile",
            "a[href*='/profile']",
            "a[href*='/me']",
            "button[aria-label*='account' i]",
            "img[alt*='avatar' i]",
            "[data-testid*='user' i]",
            "nav >> text=/account/i",
        )
        for selector in logged_in_selectors:
            try:
                if await self.page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue

        return await self._has_session_cookies()

    async def _has_session_cookies(self) -> bool:
        """Heuristic cookie check used only when no UI affordance was found."""
        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies()
        except Exception:
            return False

        interesting = ("aws", "amazon", "builder", "sso", "saml", "idp")
        for cookie in cookies:
            name = str(cookie.get("name", "")).lower()
            domain = str(cookie.get("domain", "")).lower()
            if any(token in name or token in domain for token in interesting):
                if cookie.get("value"):
                    return True
        return False

    # ------------------------------------------------------------------
    # Badge progress
    # ------------------------------------------------------------------
    async def get_badge_count(self) -> int | None:
        """Read the current badge count.

        Prefers the badge progress API (authoritative), then falls back to DOM
        selectors and finally to page-text parsing. Returns ``None`` when the
        count cannot be determined - callers must treat ``None`` as "unknown"
        and must not overwrite previously stored data.
        """
        if self.page is None:
            return None

        rows = await self.fetch_badge_progress()
        if rows is not None:
            count = self.granted_badge_count(rows)
            if count >= 0:
                return count

        print("Falling back to DOM scraping for the badge count")
        await self._settle()

        for selector in self.BADGE_SELECTORS:
            try:
                locator = self.page.locator(selector)
                if await locator.count() == 0:
                    continue
                text = (await locator.first.text_content()) or ""
                parsed = parse_badge_count(text)
                if parsed is not None:
                    return parsed
            except Exception:
                continue

        return await self._badge_count_from_body()

    async def _badge_count_from_body(self) -> int | None:
        """Final fallback: parse the rendered page text."""
        if self.page is None:
            return None
        try:
            body = await self.page.inner_text("body")
        except Exception:
            return None
        return parse_badge_count(body)

    async def get_user_info(self) -> dict[str, str]:
        """Best-effort profile details (never used for publishing decisions)."""
        info: dict[str, str] = {}
        if self.page is None:
            return info

        for key, selectors in self.USER_INFO_SELECTORS.items():
            for selector in selectors:
                try:
                    locator = self.page.locator(selector)
                    if await locator.count() == 0:
                        continue
                    text = (await locator.first.text_content() or "").strip()
                    if text and len(text) < 120:
                        info.setdefault(key, text)
                        break
                except Exception:
                    continue
        return info

    def get_current_timestamp(self) -> str:
        """Current UTC time in ISO-8601."""
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # ------------------------------------------------------------------
    # Activity discovery (read-only)
    # ------------------------------------------------------------------
    async def discover_activities(self, limit: int = 20) -> list[dict[str, str]]:
        """Collect recent activity entries with title, URL and timestamp.

        Returns an empty list on any failure - discovery is best-effort and
        must never abort the daily run.
        """
        if self.page is None:
            return []

        activities: list[dict[str, str]] = []
        seen: set[str] = set()

        for selector in self.ACTIVITY_CONTAINER_SELECTORS:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
            except Exception:
                continue

            for index in range(min(count, 60)):
                try:
                    entry = await self._read_activity_entry(locator.nth(index))
                except Exception:
                    continue
                if not entry:
                    continue
                key = entry["url"] or entry["title"].lower()
                if key in seen:
                    continue
                seen.add(key)
                activities.append(entry)
                if len(activities) >= limit:
                    return activities

        return activities[:limit]

    async def _read_activity_entry(self, element: Any) -> dict[str, str] | None:
        """Extract ``{title, url, timestamp, content}`` from one DOM element."""
        title = ""
        url = ""

        for title_selector in self.ACTIVITY_TITLE_SELECTORS:
            try:
                node = element.locator(title_selector).first
                if await node.count() == 0:
                    continue
                title = ((await node.text_content()) or "").strip()
                if title:
                    try:
                        href = await node.get_attribute("href")
                    except Exception:
                        href = None
                    if href:
                        url = urljoin(self.base_url, href)
                    break
            except Exception:
                continue

        if not title:
            try:
                title = ((await element.inner_text()) or "").strip().splitlines()[0]
            except Exception:
                title = ""
        if not title or not url:
            return None

        timestamp = ""
        for time_selector in self.ACTIVITY_TIME_SELECTORS:
            try:
                node = element.locator(time_selector).first
                if await node.count() == 0:
                    continue
                timestamp = (
                    await node.get_attribute("datetime")
                    or (await node.text_content())
                    or ""
                ).strip()
                if timestamp:
                    break
            except Exception:
                continue

        content = ""
        try:
            snippet = (await element.inner_text() or "").strip()
            content = snippet[:800] if len(snippet) > MIN_USEFUL_TEXT else ""
        except Exception:
            content = ""

        return {
            "title": title[:300],
            "url": url,
            "timestamp": timestamp[:80],
            "content": content,
        }

    async def scrape_activity_content(self, url: str, max_chars: int = 8000) -> str:
        """Open an activity page and return its main article text.

        Returns ``""`` when the page cannot be read. Callers must treat an empty
        result as "insufficient evidence" instead of assuming anything.
        """
        if self.page is None or not url:
            return ""
        if not await self.goto(url):
            return ""

        for selector in self.ARTICLE_CONTENT_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = ((await locator.inner_text()) or "").strip()
                if len(text) > MIN_USEFUL_TEXT:
                    return text[:max_chars]
            except Exception:
                continue

        try:
            body = ((await self.page.inner_text("body")) or "").strip()
        except Exception:
            return ""
        return body[:max_chars] if len(body) > MIN_USEFUL_TEXT else ""
    # ------------------------------------------------------------------
    # Selectors - all best-effort, ordered from most to least specific.
    # The live Builder Center DOM has not been verified, so every path has a
    # text-parsing fallback and every failure path returns None / [].
    # ------------------------------------------------------------------
    BADGE_SELECTORS: tuple[str, ...] = (
        "[data-testid='badge-count']",
        "[class*='badge-count']",
        "[class*='badgeCount']",
        "[class*='progress'] >> text=/\\d+\\s*\\/\\s*\\d+/",
        "text=/^\\d+\\s*\\/\\s*\\d+$/",
        "text=/\\d+\\s*\\/?\\s*21\\s*badges?/i",
        "[class*='badge'] >> text=/\\d+/",
        "a[href*='/badges'] >> text=/\\d+/",
    )

    USER_INFO_SELECTORS: dict[str, tuple[str, ...]] = {
        "name": (
            "[data-testid='user-name']",
            "[class*='user-name']",
            "[class*='displayName']",
            "a[href*='/profile'] >> text=.",
        ),
        "link": ("a[href*='/profile']",),
    }

    ACTIVITY_CONTAINER_SELECTORS: tuple[str, ...] = (
        "article",
        "[class*='post-item']",
        "[class*='activity-item']",
        "[class*='feed-item']",
        "li[class*='item']",
        "a[href*='/content/']",
        "a[href*='/discussion/']",
    )

    ACTIVITY_TITLE_SELECTORS: tuple[str, ...] = (
        "h1",
        "h2",
        "h3",
        "[class*='title']",
        "a[href]",
    )

    ACTIVITY_TIME_SELECTORS: tuple[str, ...] = (
        "time",
        "[class*='timestamp']",
        "[class*='date']",
        "[class*='relative-time']",
    )

    ARTICLE_CONTENT_SELECTORS: tuple[str, ...] = (
        "article",
        "[class*='article-content']",
        "[class*='post-content']",
        "[class*='content-body']",
        "main",
    )

    @staticmethod
    def is_same_host(url: str, base_url: str) -> bool:
        """Guard against following links off the Builder Center host."""
        from urllib.parse import urlparse

        try:
            a = urlparse(url)
            b = urlparse(base_url)
        except ValueError:
            return False
        if a.scheme not in ("http", "https"):
            return False
        if not a.netloc:
            return False
        base_host = b.netloc.lower()
        host = a.netloc.lower()
        return host == base_host or host.endswith("." + base_host) or a.netloc.lower().endswith("aws.com")
    # ------------------------------------------------------------------
    # Badge progress API
    #
    # The Builder Center profile page reads badges from a POST endpoint:
    #   https://api.builder.aws.com/rms/badges/progress
    # body: {"locale":"en","pageSize":50}  + header: x-csrf-token
    # The csrf token is minted per page load, so we harvest it from the
    # site's own request rather than trying to guess it. Responses are
    # paginated via nextToken.
    # ------------------------------------------------------------------
    BADGE_PROGRESS_URL = "https://api.builder.aws.com/rms/badges/progress"
    PROFILE_PATH = "/profile"

    async def _capture_csrf(self) -> str | None:
        """Load the profile page and capture the csrf token it sends."""
        assert self.page is not None
        captured: dict[str, str] = {}

        async def on_request(request: Any) -> None:
            if "rms/badges" not in request.url:
                return
            try:
                headers = await request.all_headers()
            except Exception:
                return
            token = headers.get("x-csrf-token")
            if token:
                captured["token"] = token

        self.page.on("request", on_request)
        try:
            await self.goto(urljoin(self.base_url, self.PROFILE_PATH))
            for _ in range(40):  # up to ~8s waiting for the first XHR
                if "token" in captured:
                    return captured["token"]
                await asyncio.sleep(0.2)
        finally:
            try:
                self.page.remove_listener("request", on_request)
            except Exception:
                pass
        return captured.get("token")

    async def fetch_badge_progress(self) -> list[dict[str, Any]] | None:
        """Fetch every badge progress row from the Builder Center API.

        Returns ``None`` when the API cannot be read, so callers can fall back
        to DOM text scraping rather than trusting a partial list.
        """
        if self.page is None:
            return None

        csrf = await self._capture_csrf()
        if not csrf:
            print("Could not capture the Builder Center csrf token")
            return None

        rows: dict[str, dict[str, Any]] = {}
        token = ""
        for _ in range(20):  # hard cap so a pagination bug cannot hang a run
            payload: dict[str, Any] = {"locale": "en", "pageSize": 50}
            if token:
                payload["nextToken"] = token
            try:
                response = await self.page.request.post(
                    self.BADGE_PROGRESS_URL,
                    data=json.dumps(payload),
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json",
                        "x-csrf-token": csrf,
                        "referer": self.base_url,
                    },
                )
            except Exception as exc:
                print(f"Badge API request failed: {type(exc).__name__}")
                return None

            if response.status != 200:
                print(f"Badge API returned HTTP {response.status}")
                return None

            try:
                data = await response.json()
            except Exception:
                print("Badge API returned a non-JSON body")
                return None

            for row in data.get("badgeProgressList") or []:
                badge_id = (row.get("baseBadge") or {}).get("badgeId")
                if badge_id:
                    rows[badge_id] = row

            token = data.get("nextToken") or ""
            if not token:
                break

        return list(rows.values()) if rows else None
    @staticmethod
    def granted_badge_count(rows: list[dict[str, Any]]) -> int:
        """Count badges whose status is GRANTED."""
        return sum(1 for row in rows if str(row.get("status", "")).upper() == "GRANTED")

    @staticmethod
    def summarize_badge_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Turn raw API rows into a compact, storable summary.

        Returns granted/total counts plus every badge's progress, so the
        notification can show what is still outstanding.
        """
        badges: list[dict[str, Any]] = []
        categories: dict[str, dict[str, int]] = {}

        for row in rows:
            base = row.get("baseBadge") or {}
            status = str(row.get("status", "")).upper()
            count = row.get("progressCount")
            threshold = row.get("threshold")
            category = base.get("category") or "Other"

            badges.append(
                {
                    "id": base.get("badgeId"),
                    "name": base.get("displayName"),
                    "description": base.get("description"),
                    "category": category,
                    "status": status,
                    "count": count if isinstance(count, int) else None,
                    "threshold": threshold if isinstance(threshold, int) else None,
                    "unit": base.get("unit"),
                }
            )

            bucket = categories.setdefault(category, {"granted": 0, "total": 0})
            bucket["total"] += 1
            if status == "GRANTED":
                bucket["granted"] += 1

        badges.sort(key=lambda b: (b["status"] != "GRANTED", b["category"] or "", b["name"] or ""))
        return {
            "granted": BuilderCenter.granted_badge_count(rows),
            "total": len(rows),
            "categories": categories,
            "badges": badges,
        }

    async def get_badge_progress(self) -> dict[str, Any] | None:
        """Return a full badge progress summary, or ``None`` if unavailable."""
        rows = await self.fetch_badge_progress()
        if rows is None:
            return None
        return self.summarize_badge_rows(rows)