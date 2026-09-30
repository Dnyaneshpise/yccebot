"""Activity discovery, relevance filtering and draft generation.

This module never publishes anything. It reads public activity metadata,
scores relevance, and produces review-ready drafts for the user.
"""

from __future__ import annotations

import re

# Terms that make an item worth a look for AWS engineering discussion.
RELEVANCE_TERMS: tuple[str, ...] = (
    "aws", "amazon web services", "lambda", "dynamodb", "s3", "ecs", "eks",
    "ec2", "bedrock", "sagemaker", "cloudformation", "cdk", "terraform",
    "iam", "vpc", "api gateway", "step functions", "sns", "sqs", "kinesis",
    "glue", "athena", "opensearch", "cloudwatch", "x-ray", "appconfig",
    "eventbridge", "fargate", "amplify", "cognito", "rds", "aurora",
    "serverless", "container", "kubernetes", "microservice", "devops",
    "security", "observability", "architecture", "latency", "throughput",
    "benchmark", "cost optimization", "well-architected", "generative ai",
    "agent", "rag", "prompt", "java", "python", "typescript", "rust", "go",
    "postmortem", "migration", "tutorial", "hands-on", "lab", "workshop",
    "how-to", "troubleshoot", "debug", "integration", "pipeline",
    "best practice", "anti-pattern", "trade-off", "tradeoff", "challenge",
    "lesson learned", "deep dive", "walkthrough",
)

# Low-value noise: engagement bait, pure self-promotion, giveaways.
NOISE_TERMS: tuple[str, ...] = (
    "follow me", "follow back", "like and follow", "upvote", "upvote for",
    "please like", "share my post", "giveaway", "raffle", "win a",
    "referral", "affiliate", "link in bio", "mutual follow",
    "support my channel", "subscribe to",
)

WORD_RE = re.compile(r"[a-z0-9+#.]{3,}")


def extract_keywords(text: str, limit: int = 12) -> list[str]:
    """Pull meaningful lowercase keywords out of a title."""
    return [word for word in WORD_RE.findall((text or "").lower())][:limit]


def relevance_score(title: str, content: str = "") -> int:
    """Score an item's likely technical usefulness for engagement.

    Positive signals come from AWS/engineering vocabulary; engagement-bait
    language is penalised so noise never outranks a real technical post.
    """
    haystack = f"{title} {content}".lower()
    score = 0
    for term in RELEVANCE_TERMS:
        if term in haystack:
            score += 2 if " " in term or len(term) > 7 else 1
    for term in NOISE_TERMS:
        if term in haystack:
            score -= 10
    return score

def is_relevant(title: str, content: str = "", min_score: int = 2) -> bool:
    """Cheap pre-filter run before any LLM call is spent."""
    return relevance_score(title, content) >= min_score


def rank_activities(
    activities: list[dict],
    seen_titles: set[str] | None = None,
    min_score: int = 1,
) -> list[dict]:
    """Filter out noise/seen items and sort by descending relevance.

    ``min_score`` defaults to 1 (any real AWS signal) because the caller decides
    how strict to be; a hardcoded floor here would silently override it.
    """
    seen = {normalize_title(t) for t in (seen_titles or set())}
    scored: list[tuple[int, dict]] = []
    for activity in activities:
        title = activity.get("title", "")
        key = normalize_title(title)
        if not title or key in seen:
            continue
        # Collapse duplicates within this batch as well as across runs.
        seen.add(key)
        score = relevance_score(title, activity.get("content", ""))
        if score < min_score:
            continue
        enriched = dict(activity)
        enriched["relevance"] = score
        scored.append((score, enriched))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored]


def normalize_title(title: str) -> str:
    """Normalize a title so near-duplicate activity entries collapse."""
    return " ".join((title or "").lower().split())


def pick_candidates(
    activities: list[dict],
    limit: int = 3,
    min_score: int = 4,
) -> list[dict]:
    """Select the top-scoring activities worth spending LLM calls on."""
    return [a for a in rank_activities(activities) if a["relevance"] >= min_score][:limit]
async def find_opportunities(
    builder,
    ai,
    max_items: int = 3,
    min_score: int = 4,
    seen_titles: set[str] | None = None,
) -> list[dict]:
    """Return review-ready engagement opportunities (never published).

    Each returned item is a dict with ``activity``, ``summary``, ``topic``,
    ``why`` and ``comment``. Steps that fail are skipped rather than aborting
    the whole run, so one flaky page cannot break the daily notification.
    """
    drafts: list[dict] = []

    activities = await builder.discover_activities(limit=max_items * 6)
    ranked = rank_activities(activities, seen_titles, min_score=min_score)
    candidates = ranked[:max_items]

    if not candidates:
        print(f"No new relevant activities found (scanned {len(activities)}, min_score={min_score})")
        return drafts

    for activity in candidates:
        title = activity.get("title", "")
        url = activity.get("url", "")
        draft: dict = {"activity": activity, "summary": "", "topic": "", "why": "", "comment": ""}

        content = ""
        if url and builder.page is not None:
            try:
                content = await builder.scrape_activity_content(url)
            except Exception as exc:
                print(f"Content scrape failed for {url}: {type(exc).__name__}")

        if not ai.is_configured():
            drafts.append(draft)
            continue

        try:
            draft["summary"] = ai.summarize_activity(title, content)
            draft["topic"] = ai.classify_activity(title, content)
        except Exception as exc:
            print(f"Summary step skipped for '{title}': {exc}")

        try:
            if not ai.can_contribute(draft["summary"] or title, title):
                print(f"No genuine value possible for '{title}'; skipping draft")
                continue
            draft["comment"] = ai.draft_comment(title, draft["summary"], content)
            draft["why"] = (
                f"Relevance score {activity.get('relevance')}"
                + (f"; topic {draft['topic']}" if draft["topic"] else "")
                + ". Draft is review-only - nothing has been posted."
            )
        except Exception as exc:
            print(f"Draft step skipped for '{title}': {exc}")

        drafts.append(draft)

    return drafts