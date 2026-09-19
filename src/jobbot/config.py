"""Central configuration for the job scraping pipeline."""

# Each entry is one JobSpy search_term - results across all terms get combined and deduped.
# Broad terms are used because searching "software engineer" on LinkedIn already surfaces
# backend/frontend/full stack/mobile/web roles.
SEARCH_TERMS = [
    "software engineer",
    "data scientist",
    "data engineer",
    "machine learning engineer",
    "ai engineer",
    "product manager",
    "technical product manager",
    "technical program manager",
    "forward deployed engineer",
]

SITE_NAMES = ["linkedin", "indeed"]
LOCATION = "Tel Aviv, Israel"
COUNTRY_INDEED = "Israel"

# Radius (miles) around LOCATION - covers roughly a 30 min drive: Ra'anana (north),
# Holon/Rishon LeZion (south), Yehud/Petach Tikva (east). LinkedIn only supports fixed
# steps (5/10/25/50/75/100), so 25 is the smallest step that covers Ra'anana (~13 mi out).
DISTANCE_MILES = 25

RESULTS_WANTED_PER_TERM = 200  # ceiling, not a target - JobSpy stops once a term is exhausted,
# so this only costs more when there's genuinely more to fetch, which is when you'd want it anyway.

# The real production window is computed dynamically per run from scrape_state.py (time since the
# last successful scrape, so a long gap - computer asleep/off - widens the window to cover it
# instead of missing what was posted during it). HOURS_OLD is just the fallback for a fresh
# checkout with no recorded prior scrape yet, and what qa/build_review.py uses (no dynamic state
# there - it's testing against a frozen dataset, not tracking real elapsed time).
HOURS_OLD = 24

# pipeline.py self-throttles to roughly this often when invoked via Task Scheduler every 15 min -
# see scrape_state.py and pipeline.run().
SCRAPE_INTERVAL_HOURS = 6

# Step 2 filtering criterion: accept jobs requiring at most this many years of experience.
MAX_YEARS_EXPERIENCE = 1

# Only used by the flexibility-aware schema (gemini_filter.classify(judge_flexibility=True)): when a
# posting itself says its stated years number isn't strict ("flexible for exceptional builders"), a
# requirement up to this many years is still shown. A cap, not a rule of thumb the model applies: a
# 15-year posting carrying generic "exceptional candidates considered" boilerplate must stay hidden.
MAX_YEARS_WITH_FLEXIBLE_REQUIREMENT = 4

# Ground-truth collection mode for the prompt-engineering evaluation project (see qa/prompt_eval.py).
# While True: filter_job() skips the Gemini call so every job that clears title/location/language
# filtering reaches the dashboard as 'new', and db.cleanup_old_rows() skips the new/deleted/rejected
# retention rules - real apply/delete decisions on that wider pool become human-labeled ground truth.
# Turned OFF (2026-09-19) when quote_first_background_v3 was promoted to the production prompt: Gemini
# now gates automatically again and retention resumes. Labels are safe from that retention because
# qa/collect_ground_truth.py merges into qa/data/ground_truth.json - run it now and then to keep
# banking new applied/deleted labels. Flip back to True to collect an unfiltered pool again.
COLLECTING_GROUND_TRUTH = False
