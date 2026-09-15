"""Step 1: scrape LinkedIn + Indeed with JobSpy for all configured search terms."""

import math
import re
import time
from datetime import datetime
from difflib import SequenceMatcher

import pandas as pd
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn as _LinkedInScraper

from jobbot import config

COLUMNS = ["title", "company", "location", "description", "job_url", "date_posted"]

# JobSpy's LinkedIn date parser only checks for class "job-search-card__listdate", but when a
# date-range filter is active (hours_old, which we always set) LinkedIn renders the same info
# under "job-search-card__listdate--new" instead - confirmed by comparing raw HTML with and
# without hours_old set: the <time> element is present either way, just under a different class,
# so this is a real, fixable gap in JobSpy's selector rather than data LinkedIn doesn't provide.
# Patched once at import time so every scrape_jobs() call through this module benefits.
_original_process_job = _LinkedInScraper._process_job


def _patched_process_job(self, job_card, job_id, full_descr):
    result = _original_process_job(self, job_card, job_id, full_descr)
    if result is not None and result.date_posted is None:
        time_tag = job_card.find("time", class_="job-search-card__listdate--new")
        if time_tag and "datetime" in time_tag.attrs:
            try:
                # .date(): the model field is `date`, not `datetime` (a plain strptime result
                # would trigger a Pydantic serialization type-mismatch warning on every job).
                result.date_posted = datetime.strptime(time_tag["datetime"], "%Y-%m-%d").date()
            except ValueError:
                pass
    return result


_LinkedInScraper._process_job = _patched_process_job

# JobSpy returns descriptions as Markdown, which backslash-escapes punctuation - e.g.
# "1-3 Years" becomes "1\-3 Years". Left alone this breaks regex matching (a "1\-3" range
# doesn't match our "-" pattern) and shows literal backslashes on the dashboard.
_MARKDOWN_ESCAPE_PATTERN = re.compile(r"\\([\\`*_{}\[\]()#+\-.!])")


def _unescape_markdown(text) -> str:
    if not isinstance(text, str):
        return text
    return _MARKDOWN_ESCAPE_PATTERN.sub(r"\1", text)


# LinkedIn and Indeed each assign their own job_url to the same real posting, so URL-based dedup
# alone lets the identical job through twice. Description content is a much safer same-job signal
# than title+company - a company can genuinely have many different openings sharing a generic
# title (e.g. several distinct "Software Engineer" reqs), but two truly different postings won't
# share near-identical description text. An exact match (even after stripping punctuation) is
# still too fragile though - confirmed the same NVIDIA posting differs between sites in two ways:
# "doing**" vs "doing:**" (Markdown conversion handles the source HTML slightly differently) AND
# LinkedIn's copy has an extra trailing job requisition id ("jr2025271") Indeed's doesn't. Neither
# is a real content difference, so fuzzy-match with a high similarity threshold instead of exact
# equality. \w is Unicode-aware, so Hebrew text is preserved through normalization.
_NON_WORD_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SIMILARITY_THRESHOLD = 0.97

# Prefer keeping LinkedIn's row when the same posting appears on both sites, since that's the
# platform actually browsed day to day.
_SITE_PREFERENCE = {"linkedin": 0, "indeed": 1}


def _dedup_key(text) -> str:
    if not isinstance(text, str):
        return ""
    text = _NON_WORD_PATTERN.sub("", text)
    return _WHITESPACE_PATTERN.sub(" ", text).strip().lower()


# Indeed's date filter works in day-level buckets, not hours - anything under 24h reliably
# returns zero results even though the API accepts an hours_old value in hours (confirmed: 6-23h
# all return 0, 24h also returns 0, only jumping to real counts at 48h). LinkedIn's hour-level
# filtering works fine at any window. So each site needs its own hours_old, per-site scrape calls
# instead of one combined call for both sites.
SITE_HOURS_OLD = {
    "linkedin": lambda base: base,
    "indeed": lambda base: max(base, 48),
}


def scrape_all(hours_old_override: float | None = None) -> pd.DataFrame:
    """Run one JobSpy scrape per (search term, site) pair and combine + dedup the results.

    hours_old_override, when given, replaces config.HOURS_OLD as the base window (still subject
    to Indeed's 48h floor above) - pipeline.py uses this to widen the window after a long gap
    since the last scrape instead of missing everything posted during it. JobSpy's hours_old is
    int-only, so a fractional override is rounded UP (never down - undershooting could miss
    something posted right at the edge of the gap).
    """
    base_hours_old = math.ceil(hours_old_override) if hours_old_override is not None else config.HOURS_OLD
    frames = []
    for term in config.SEARCH_TERMS:
        for site in config.SITE_NAMES:
            hours_old = SITE_HOURS_OLD[site](base_hours_old)
            print(f"Scraping '{term}' on {site} (hours_old={hours_old})...")
            try:
                jobs = scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    location=config.LOCATION,
                    distance=config.DISTANCE_MILES,
                    country_indeed=config.COUNTRY_INDEED,
                    linkedin_fetch_description=True,
                    results_wanted=config.RESULTS_WANTED_PER_TERM,
                    hours_old=hours_old,
                )
            except Exception as e:
                print(f"  failed: {e}")
                continue
            print(f"  got {len(jobs)} results")
            jobs["search_term"] = term
            frames.append(jobs)
            time.sleep(2)  # be polite between searches

    if not frames:
        return pd.DataFrame(columns=[*COLUMNS, "search_terms"])

    combined = pd.concat(frames, ignore_index=True)

    # A job can surface under multiple search terms - collect all of them per URL before
    # dropping duplicate rows, instead of silently losing that provenance to drop_duplicates.
    terms_by_url = combined.groupby("job_url")["search_term"].agg(lambda s: ", ".join(sorted(set(s))))
    combined = combined.drop_duplicates(subset="job_url").copy()
    combined["search_terms"] = combined["job_url"].map(terms_by_url)
    combined = combined.drop(columns=["search_term"])

    combined["description"] = combined["description"].apply(_unescape_markdown)
    combined = _dedup_cross_site(combined)
    return combined


def _dedup_cross_site(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the same posting appearing on both LinkedIn and Indeed under different URLs.

    Union-find over fuzzy-similar descriptions, restricted to same-(company, title) pairs. Company
    alone isn't a tight enough pre-filter - a recruiting agency like "Gotfriends" posts dozens of
    genuinely distinct roles, so grouping only by company means comparing every pair within it for
    no reason. Title is part of the actual posting content (not site-generated metadata) so it's
    safe to require exactly, unlike location - Indeed returns city/region names in the site's local
    script (e.g. Hebrew for Israeli listings) regardless of the request's locale headers, so a
    LinkedIn "Tel Aviv-Yafo" would never string-match Indeed's "תל אביב -יפו" for the same job.
    """
    df = df.reset_index(drop=True)
    keys = df["description"].apply(_dedup_key)
    companies = df["company"].fillna("").str.lower()
    titles = df["title"].apply(_dedup_key)

    parent = list(range(len(df)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for _, idxs in df.groupby([companies, titles]).groups.items():
        idxs = list(idxs)
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                a, b = idxs[i], idxs[j]
                if abs(len(keys[a]) - len(keys[b])) > 50:  # cheap skip before the real comparison
                    continue
                if SequenceMatcher(None, keys[a], keys[b]).ratio() >= _SIMILARITY_THRESHOLD:
                    union(a, b)

    clusters: dict[int, list[int]] = {}
    for i in range(len(df)):
        clusters.setdefault(find(i), []).append(i)

    rows = []
    for idxs in clusters.values():
        group = df.iloc[idxs]
        if len(group) == 1:
            rows.append(group.iloc[0])
            continue
        all_terms = {t.strip() for terms in group["search_terms"] for t in terms.split(",")}
        order = group["site"].map(_SITE_PREFERENCE).fillna(99)
        best = group.loc[[order.idxmin()]].iloc[0].copy()
        best["search_terms"] = ", ".join(sorted(all_terms))
        rows.append(best)

    return pd.DataFrame(rows).reset_index(drop=True)


if __name__ == "__main__":
    from jobbot.paths import OUTPUT_DIR

    df = scrape_all()
    print(f"\nTotal unique jobs: {len(df)}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "scraped_jobs.csv"
    df.to_csv(out_path, index=False)
    print(f"Saved to {out_path}")
