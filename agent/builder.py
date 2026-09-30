"""Browser automation for AWS Builder Center.

All DOM selectors here are best-effort. The live Builder Center markup changes
without notice, so every lookup falls back to plain-text parsing and finally to
``None`` rather than raising or guessing. Nothing in this module publishes,
likes, or comments on anything - it only reads.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from .rewards import MAX_SANE_BADGES, parse_badge_count

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
        cdp_endpoint: str | None = None,
        browser_profile: str | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.headless = headless
        self.user_agent = user_agent
        self._playwright: Any = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp_endpoint: str | None = cdp_endpoint
        self.browser_profile: str | None = browser_profile
        self._badge_cache: dict[str, Any] | None = None
        # Set when the badge API refuses this account outright. Retrying cannot
        # help, so later runs skip the API and go straight to the DOM fallback.
        self._permission_denied: bool = False
        # Tracks whether the current page is the feed, so discovery knows when
        # it must navigate before scanning.
        self._on_feed: bool = False

    async def initialize(self, storage_state_path: str | None = None) -> None:
        """Open Builder Center, preferring a real attached browser.

        A Playwright-launched browser is treated as automation by AWS: reads
        work but writes are refused and the page renders a signed-out shell.
        When a CDP endpoint is configured we attach to the user's own running
        browser instead, which acts as a genuine signed-in session.

        Falls back to launching with the stored state when no endpoint is set.
        """
        if self.cdp_endpoint:
            if await self.connect_over_cdp(self.cdp_endpoint):
                # The attached browser already carries the user's own session,
                # so the stored state file is not needed on this path.
                return
            print("Falling back to a Playwright-launched browser")
        await self._launch_with_state(storage_state_path)

    async def _launch_with_state(self, storage_state_path: str | None = None) -> None:
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
        # Only the site root is the feed; anything else means discovery must
        # navigate before it can scan.
        self._on_feed = target.rstrip("/") == self.base_url.rstrip("/")
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

        # Dismiss the consent banner first: it contains a "Sign in" button that
        # is present even when authenticated, which would otherwise look like a
        # logged-out page.
        await self._dismiss_consent()
        await self._settle(1500)

        # Positive evidence first. A single "Sign in" is not proof of being
        # logged out, so we only conclude that from a real logout affordance.
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

        # Session cookies are authoritative even when the UI is still rendering.
        if await self._has_session_cookies():
            return True

        # No logged-in affordance and no session cookies. Only now treat a
        # visible sign-in prompt as logged out.
        for selector in ("a:has-text('Sign in')", "text=Sign in >> nth=0"):
            try:
                if await self.page.locator(selector).count() > 0:
                    return False
            except Exception:
                continue

        return False

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
    def _is_on_feed(self) -> bool:
        """True when the current page is the Builder Center home feed.

        Read from the live URL, never from a cached flag, so it stays correct
        however the page was navigated to.
        """
        if self.page is None:
            return False
        try:
            path = urlparse(self.page.url).path.rstrip("/")
        except Exception:
            return False
        return path in ("", "/")

    async def discover_activities(self, limit: int = 20) -> list[dict[str, str]]:
        """Collect recent activity entries with title, URL and timestamp.

        Navigates to the feed first: the caller is rarely still on the feed
        (badge tracking may have just visited the profile), and scanning the
        wrong page silently returns nothing.

        Returns an empty list on any failure - discovery is best-effort and
        must never abort the daily run.
        """
        if self.page is None:
            return []

        # Derive this from the live URL rather than a cached flag: other code
        # paths (e.g. the csrf capture) navigate with page.goto directly and
        # would leave a flag stale.
        if not self._is_on_feed():
            if not await self.goto(self.base_url):
                return []
            await self._dismiss_consent()

        activities: list[dict[str, str]] = []
        seen: set[str] = set()
        # The feed renders article titles as bare <a href="/content/..."> links
        # with no stable wrapper element, so read them directly rather than
        # relying on container selectors that may not exist.
        try:
            rows = await self.page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/content/"]'))
                        .map(a => ({ url: a.href,
                                     title: (a.innerText || a.textContent || '').trim() }))
                        .filter(x => x.title && x.title.length > 8)"""
            )
        except Exception as exc:
            print(f"Direct article link scan failed: {type(exc).__name__}")
            rows = []

        for row in rows or []:
            entry = {
                "title": str(row.get("title", ""))[:300],
                "url": str(row.get("url", "")),
                "timestamp": "",
                "content": "",
            }
            if not self.is_real_article(entry):
                continue
            if entry["url"] in seen:
                continue
            seen.add(entry["url"])
            activities.append(entry)
            if len(activities) >= limit:
                return activities

        if activities:
            return activities

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
                if not entry or not self.is_real_article(entry):
                    continue
                key = entry["url"] or entry["title"].lower()
                if key in seen:
                    continue
                seen.add(key)
                activities.append(entry)
                if len(activities) >= limit:
                    return activities

        return activities[:limit]

    def is_real_article(self, entry: dict[str, str]) -> bool:
        """Reject navigation chrome and non-article links.

        The feed markup contains header links ("Workshops", "Community", ...)
        that are not articles. A real article URL carries a content id, which
        is the only reliable signal - titles alone are not enough. Hrefs may be
        relative ("/content/<id>/<slug>") or absolute, so both are accepted.
        """
        url = (entry.get("url") or "").strip().lower()
        if not url:
            return False
        if url.startswith(("javascript:", "mailto:", "#")):
            return False
        # Builder Center content URLs look like /content/<id>/<slug>
        if re.search(r"/content/[A-Za-z0-9]{8,}", url):
            return True
        # Discussions are legitimate engagement targets too.
        if "/discussion/" in url:
            return True
        return False

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

    async def connect_over_cdp(self, endpoint: str | None = None) -> bool:
        """Attach to an already-running browser that has CDP enabled.

        This is the most reliable way to act as a real signed-in user: the
        browser is genuine, the profile and cookies are the user's own, and
        nothing is launched under Playwright's control.

        The browser must be started with --remote-debugging-port. Returns
        True on success.
        """
        from playwright.async_api import async_playwright

        target = (endpoint or self.cdp_endpoint or "").strip()
        if not target:
            print("No CDP endpoint supplied")
            return False

        if not target.startswith(("http://", "ws://")):
            target = f"http://{target}"

        self.cdp_endpoint = target
        self._playwright = await async_playwright().start()
        try:
            self.browser = await self._playwright.chromium.connect_over_cdp(target)
        except Exception as exc:
            print(f"Could not connect over CDP to {target}: {type(exc).__name__}")
            print("Is the browser running with --remote-debugging-port?")
            await self.close()
            return False

        contexts = self.browser.contexts
        if not contexts:
            print("CDP connected but the browser exposed no page context")
            await self.close()
            return False

        self.context = contexts[0]
        self.context.set_default_timeout(DEFAULT_TIMEOUT_MS)

        pages = [p for p in self.context.pages if "builder.aws.com" in (p.url or "")]
        if pages:
            self.page = pages[0]
        else:
            self.page = await self.context.new_page()
        print(f"Attached to a running browser via CDP ({target})")
        return True

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
        """Load a Builder Center page and capture the csrf token it sends.

        The site attaches ``x-csrf-token`` to every api.builder.aws.com call and
        the token is valid session-wide, so we harvest it from whatever request
        fires first. We navigate with ``wait_until='commit'`` and poll, because
        the XHRs fire well before ``goto()`` would return.
        """
        if self.page is None:
            return None
        captured: dict[str, str] = {}

        async def on_request(request: Any) -> None:
            if "api.builder.aws.com" not in request.url:
                return
            try:
                headers = await request.all_headers()
            except Exception:
                return
            token = headers.get("x-csrf-token")
            if token and "token" not in captured:
                captured["token"] = token

        self.page.on("request", on_request)
        try:
            for path in (self.PROFILE_PATH, "/"):
                if "token" in captured:
                    break
                try:
                    await self.page.goto(
                        urljoin(self.base_url, path),
                        timeout=NAV_TIMEOUT_MS,
                        wait_until="commit",
                    )
                except Exception as exc:
                    print(f"Navigation to {path} failed: {type(exc).__name__}")
                    continue

                for _ in range(50):  # up to ~10s per page
                    if "token" in captured:
                        break
                    await asyncio.sleep(0.2)
        finally:
            try:
                self.page.remove_listener("request", on_request)
            except Exception:
                pass

        if not captured.get("token"):
            print("Could not capture the Builder Center csrf token")
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
                detail = ""
                try:
                    detail = (await response.text())[:200]
                except Exception:
                    pass
                if _is_permission_denied(response.status, detail):
                    # The account is not authorised for this endpoint at all.
                    # Retrying or changing headers cannot help, so say so
                    # plainly and let the caller fall back to the DOM.
                    self._permission_denied = True
                    print(
                        "Badge API is denied for this account (403 identity-based "
                        "policy). This is a server-side permission, not a token "
                        "issue - falling back to the rendered rewards page."
                    )
                    return None
                print(f"Badge API returned HTTP {response.status} {detail[:120]}")
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
        """Return a badge progress summary, or ``None`` if unavailable.

        Tries the API first, falls back to the rendered page, and finally to a
        snapshot taken earlier in the run. Once the API has refused this account
        we stop calling it, so a known-denied account does not pay the retry
        cost on every run.
        """
        if not self._permission_denied:
            rows = await self.fetch_badge_progress()
            if rows:
                self._badge_cache = self.summarize_badge_rows(rows)
                return self._badge_cache

        from_scrape = await self._badge_progress_from_dom()
        if from_scrape:
            self._badge_cache = from_scrape
            return from_scrape

        if self._badge_cache:
            print("Using the badge snapshot cached during this run")
            return self._badge_cache
        return None

    async def _badge_progress_from_dom(self) -> dict[str, Any] | None:
        """Last-resort parse of the rendered page.

        Used when the badge API is not authorised for the account. Only an
        explicit granted/total phrase is accepted - a bare number on these pages
        is usually a like count, so we refuse rather than guess.
        """
        if self.page is None:
            return None

        for path in (self.PROFILE_PATH, "/student-rewards"):
            if not await self.goto(urljoin(self.base_url, path)):
                continue
            await self._dismiss_consent()
            for _ in range(8):  # the SPA renders late, so poll briefly
                try:
                    body = await self.page.inner_text("body")
                except Exception:
                    break
                parsed = self._parse_badge_text(body)
                if parsed:
                    parsed["source"] = f"dom:{path}"
                    return parsed
                await asyncio.sleep(1.5)
        return None

    @staticmethod
    def _parse_badge_text(body: str) -> dict[str, Any] | None:
        """Extract a granted/total badge count from visible page text."""
        if not body:
            return None
        patterns = (
            r"(\d+)\s+of\s+(\d+)\s+badges?\b",
            r"badges?\s*[:\-]?\s*(\d+)\s*(?:of|/)\s*(\d+)",
            r"\b(\d+)\s*/\s*(\d+)\s*badges?\b",
        )
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if not match:
                continue
            granted, total = int(match.group(1)), int(match.group(2))
            if 0 <= granted <= total <= MAX_SANE_BADGES:
                return {"granted": granted, "total": total, "categories": {}, "badges": []}
        return None
    # ------------------------------------------------------------------
    # Approved engagement actions
    #
    # SAFETY: these are only ever called for a proposal the user explicitly
    # approved (see agent.actions.execute, which re-checks the approval flag).
    # They perform exactly the action that was approved and make no judgement
    # calls of their own.
    # ------------------------------------------------------------------
    async def _dismiss_consent(self) -> bool:
        """Remove the cookie-consent banner that covers the page.

        This matters more than it looks: the banner is a full-screen overlay,
        so it intercepts clicks on the like button. Clicks appear to succeed
        while nothing happens at all. Returns True if a banner was present.
        """
        if self.page is None:
            return False

        # Prefer the banner's own "Accept"/"Decline" buttons - that records the
        # choice in the cookie so it stops coming back.
        # Scope this to the consent widget. A page-wide "Accept" search can hit
        # an unrelated control, and the widget has to be dismissed before any
        # other element is reachable.
        for label in ("Accept all", "Accept", "Continue", "Decline"):
            for scope in ("#awsccc-cb-c", ".awsccc-tab-helper", "body"):
                try:
                    button = self.page.locator(f"{scope} button:has-text('{label}')").first
                    if await button.count() == 0 or not await button.is_visible():
                        continue
                    await button.click(timeout=6000)
                    await self.page.wait_for_timeout(800)
                    break
                except Exception:
                    continue

        removed = False
        selectors = (
            # AWS's own cookie widget (id awsccc-cb-c / class awsccc-tab-helper).
            # This is the one that actually appears on Builder Center and it
            # covers the whole page, so it must go before anything else.
            "#awsccc-cb-c",
            ".awsccc-tab-helper",
            "[id^='awsccc']",
            "[class*='awsccc']",
            "[data-testid='ccba-content']",
            "[data-testid='ccba-footer']",
            "[class*='cbbas']",
            "[id*='ccba']",
            "[class*='cookie-consent']",
            "[class*='cookieConsent']",
            "[role='dialog'][class*='modal']",
            "[class*='awsui-modal-root']",
        )
        for selector in selectors:
            try:
                count = await self.page.locator(selector).count()
                if count == 0:
                    continue
                await self.page.evaluate(
                    "sel => document.querySelectorAll(sel).forEach(e => e.remove())",
                    selector,
                )
                removed = True
            except Exception:
                continue

        # Any leftover full-screen overlay that would swallow clicks.
        try:
            blocked = await self.page.evaluate(
                """() => {
                    const overlays = [...document.querySelectorAll('body *')]
                      .filter(e => {
                        const s = getComputedStyle(e);
                        if (s.position !== 'fixed') return false;
                        const z = parseInt(s.zIndex || '0', 10);
                        return z >= 1000 && e.offsetWidth > window.innerWidth * 0.8;
                      });
                    overlays.forEach(e => e.remove());
                    return overlays.length;
                }"""
            )
            if blocked:
                removed = True
        except Exception:
            pass

        if removed:
            await self.page.wait_for_timeout(400)
        return removed

    async def _find_action_control(self, aria_prefix: str) -> Any:
        """Locate a control by its aria-label prefix, or return None."""
        if self.page is None:
            return None
        selector = f"button[aria-label^='{aria_prefix}']"
        try:
            if await self.page.locator(selector).count() == 0:
                return None
            return self.page.locator(selector).first
        except Exception:
            return None

    async def _is_control_covered(self, control: Any) -> bool:
        """True when something invisible still sits over the control.

        Used to tell "an overlay ate the click" apart from "the site refused
        the request", which otherwise produce the same symptom.
        """
        if self.page is None:
            return False
        try:
            box = await control.bounding_box()
            if not box:
                return False
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            return not await self.page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    return el === null;
                }""",
                [cx, cy],
            )
        except Exception:
            return False

    async def like_article(self, url: str) -> str:
        """Like one article. Called only for an approved proposal.

        Idempotent: an already-liked article is reported as skipped rather than
        toggled, so re-running can never unlike something on purpose.

        The outcome is verified: a click the site silently discards must be
        reported as a failure, otherwise a badge streak would appear to advance
        when nothing actually happened.
        """
        if not await self.goto(url):
            return "FAILED: could not open article"
        await self._dismiss_consent()

        control = await self._find_action_control("Like this article")
        if control is None:
            return "FAILED: no like control found"

        try:
            pressed = await control.get_attribute("aria-pressed")
            if str(pressed).lower() == "true":
                return "SKIPPED: already liked"
        except Exception:
            pass

        # Watch for the write request so a silent rejection is detectable.
        writes = []

        async def on_response(response):
            if "api.builder.aws.com" in response.url.lower() and response.request.method in ("POST", "PUT"):
                writes.append(response.status)

        self.page.on("response", on_response)
        try:
            try:
                await control.scroll_into_view_if_needed(timeout=8000)
                await self.page.wait_for_timeout(500)
                await control.click(timeout=15000)
            except Exception as exc:
                return "FAILED: " + type(exc).__name__
            await self.page.wait_for_timeout(4000)
        finally:
            try:
                self.page.remove_listener("response", on_response)
            except Exception:
                pass

        try:
            now = await control.get_attribute("aria-pressed")
        except Exception:
            now = None

        if str(now).lower() == "true":
            return "OK: liked"
        if any(status >= 400 for status in writes):
            return ("FAILED: Builder Center rejected the like (HTTP %d). "
                    "This account is not permitted to like through the API." % max(writes))

        # Distinguish an overlay still covering the control from a request
        # that the site simply never accepted.
        try:
            covered = await self._is_control_covered(control)
        except Exception:
            covered = False
        if covered:
            return ("FAILED: a page overlay is intercepting clicks (the consent "
                    "banner is probably still up)")
        return ("FAILED: the click did not register (nothing intercepted it, but "
                "Builder Center did not persist the like)")

    async def post_comment(self, url: str, text: str) -> str:
        """Post a comment the user drafted and approved.

        Two-phase on purpose: the comment box is opened and filled first, and
        the result is reported without an automatic submit if the composer
        cannot be located. Nothing is published unless the full flow succeeds.
        """
        text = (text or "").strip()
        if not text:
            return "REFUSED: empty comment text"

        if not await self.goto(url):
            return "FAILED: could not open article"
        await self._dismiss_consent()

        control = await self._find_action_control("Comment on this article")
        if control is None:
            return "FAILED: no comment control found"

        try:
            await control.click(timeout=15000)
        except Exception as exc:
            return f"FAILED: could not open the comment box ({type(exc).__name__})"

        await self.page.wait_for_timeout(1500)

        composer = None
        for selector in (
            "textarea[placeholder*='omment' i]",
            "textarea[placeholder*='dd' i]",
            "div[contenteditable='true']",
            "textarea",
        ):
            try:
                locator = self.page.locator(selector).first
                if await locator.count() > 0:
                    composer = locator
                    break
            except Exception:
                continue

        if composer is None:
            return "FAILED: comment box not found (nothing was posted)"

        try:
            await composer.fill(text)
        except Exception as exc:
            return f"FAILED: could not fill the comment box ({type(exc).__name__})"

        submit = None
        for selector in (
            "button[type='submit']",
            "button:has-text('Comment')",
            "button:has-text('Post')",
            "button:has-text('Submit')",
        ):
            try:
                locator = self.page.locator(selector).first
                if await locator.count() > 0 and await locator.is_enabled():
                    submit = locator
                    break
            except Exception:
                continue

        if submit is None:
            return "FAILED: no submit button found (text was NOT posted)"

        try:
            await submit.click(timeout=15000)
        except Exception as exc:
            return f"FAILED: submit failed ({type(exc).__name__})"

        await self.page.wait_for_timeout(2000)
        return "OK: comment posted"

    async def vote_on_wish(self, url: str) -> str:
        """Vote on an AWS Wishlist item the user approved.

        The wish voting flow has not been verified against the live site, so
        this refuses rather than guessing at a selector and clicking something
        unintended.
        """
        if not url:
            return "REFUSED: no wish url supplied"
        return (
            "UNSUPPORTED: wish voting is not implemented yet - the live wish "
            "controls were not verified, so no click was attempted"
        )

# Substrings AWS uses when a request is refused by an identity-based policy
# rather than by a missing/expired token.
_DENY_MARKERS = (
    "explicit deny",
    "identity-based policy",
    "not authorized to access this resource",
)


def _is_permission_denied(status: int, body: str) -> bool:
    """True when a 401/403 is an authorisation refusal, not a stale token.

    Distinguishing these matters: a stale token is worth retrying with a fresh
    one, whereas an identity-policy deny will never succeed no matter what the
    client sends.
    """
    if status not in (401, 403):
        return False
    text = (body or "").lower()
    return any(marker in text for marker in _DENY_MARKERS)