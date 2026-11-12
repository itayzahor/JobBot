"""Draft a resume for one job: the model judges, Python enforces the rules.

Same split as the job filter (where `fit` is derived in code, never asked of the model):
- The model makes one structured call. It quotes the job's requirements first and marks each
  one have / partial / missing against the profile (quote-first, like the filter and the Gmail
  classifier). Then it picks 2 projects and *ranks* their bullets by number, builds the skills
  sidebar, writes About Me, and may suggest (rarely) a rewording of a bullet.
- Python turns that into a selection and enforces what the model can't be trusted with:
  - bullets are copied from blocks.json by number, so approved wording can't drift, and always
    keep their approved order - the user wrote them to tell the project's story in sequence. The
    model's ranking is used only to decide which bullet goes first when space runs out;
  - rewordings are never applied - they come back as suggestions the user can accept in the editor;
  - unknown projects and skills outside the pool are dropped; "C" stays only if the job names C;
    Java is always the first programming language (the user's interview language);
  - About Me is checked against the profile's rules (no "passionate", no years of experience);
  - About Me length: both versions are measured in rendered lines; one longer than
    render.MAX_ABOUT_LINES gets a focused "shorten this to ~N characters" call (up to 2 tries);
  - page fit: render, and while it overflows, drop the lowest-ranked bullet from the project with
    the most bullets left, or the last sidebar skill, and render again.

The model sees only the verified parts of the profile - "Pending review" (unverified facts) and
the private interview stories are cut out before the call.
"""

import re
import tempfile

import httpcore
import httpx
import time
from pathlib import Path
from typing import Literal

from google.genai import errors, types
from pydantic import BaseModel, Field

from jobbot import paths
from jobbot.filters import gemini_filter as gf
from jobbot.resume import content, render

# Drafting runs once per click, so a stronger model than the filter's flash-lite is affordable.
MODEL = "gemini-3.8-flash"
MAX_PROJECTS = 2
# Always on the sidebar, whatever the job: the user's interview language, main building language,
# SQL, Git/GitHub and Claude Code (the user: "the most important tool in the industry"). Python
# enforces them; _fit() never drops them.
CORE_SKILLS = ("Java", "Python", "SQL", "Git", "GitHub", "Claude Code")
MAX_EXTRAS = 10  # extras fill leftover sidebar space; _fit() trims them from the least relevant end
MAX_ROLE_SKILLS = 3          # "typical for this role, not named in the posting" - kept few
COURSEWORK_GROUP = "Relevant Coursework"
MAX_COURSES = 4
# Profile parts the model never gets:
# - unverified / private: Pending review, Interview stories, Personal;
# - "## Resume": the human-readable version of the rules - SYSTEM_INSTRUCTION is the agent's single
#   rulebook (sending both cost ~1.2k tokens and the two had already drifted apart);
# - "## What I'm looking for": decides which jobs to apply to, not how to write the resume.
_PROFILE_SECTIONS_LEFT_OUT = ("## Interview stories", "## Pending review", "## Personal", "## Resume",
                              "## What I'm looking for")
# Basics bullets code fills in itself (contact, links) or that must never reach a resume's wording
# (military - copied verbatim into its own section, banned from About Me).
_PROFILE_BULLETS_LEFT_OUT = ("- **Military:**", "- **Links:**", "- **Contact:**", "- **Location:**")


class Requirement(BaseModel):
    quote: str = Field(description="The requirement, quoted (shortened) from the posting")
    must_have: bool = Field(description="True if the posting requires it, False if it's a nice-to-have / advantage")
    match: Literal["have", "partial", "missing"]
    evidence: str = Field(description="Which project / skill in the profile shows it; empty if missing")


class ProjectPick(BaseModel):
    id: str
    bullets: list[int] = Field(description="ALL of that project's bullet numbers, ranked most relevant first")
    why: str = Field(description="One short sentence: why this project for this job")


class Reword(BaseModel):
    project_id: str
    bullet: int = Field(description="The bullet's number in that project's list")
    text: str = Field(description="The suggested wording - same facts, about the same length")
    why: str = Field(description="What it highlights for this job")


class SkillItem(BaseModel):
    name: str = Field(description="Exact spelling from the skill pool")
    layer: Literal["asked", "related", "core", "role", "extra"]
    because: str = Field(description="asked/related: the posting's words, quoted briefly; role: why this kind of role uses it; core: empty")


class SkillGroup(BaseModel):
    group: str
    items: list[SkillItem]


class Shortened(BaseModel):
    text: str


class TailoredDraft(BaseModel):
    requirements: list[Requirement]
    projects: list[ProjectPick]
    skills: list[SkillGroup]
    about: str = Field(description="About Me, strengths version")
    soft_skills_asked: list[str] = Field(description="Soft skills / personal traits the posting asks for, each quoted briefly; empty if none")
    traits_used: list[str] = Field(description="The candidate traits (from the list) used in about_personal")
    about_personal: str = Field(description="About Me, personal version - same first sentence")
    about_why: str = Field(description="One short sentence: what the About Me is aimed at")
    rewordings: list[Reword] = Field(description="Usually empty - see the instructions")
    extras: list[SkillItem] = Field(description="More pool skills ranked most relevant first, to fill leftover sidebar space (layer 'extra')")


SYSTEM_INSTRUCTION = """You tailor a candidate's one-page resume to a specific job posting.

You get, each in its own tag: <profile> (the truth about the candidate - only claim what it
shows), <resume_blocks> (the approved projects with numbered bullets, the skills each used and
which of those the candidate is comfortable using as fillers, and the skill pool), <main_about>
(their main About Me - the style and length to match), <length_limit>, and <job_posting>.

Work in this order:
1. requirements: list the posting's real requirements (skills, technologies, kinds of experience,
   education), each quoted briefly from the posting. Mark must_have vs nice-to-have. For each,
   decide have / partial / missing ONLY from the profile, and name the evidence (project or skill).
   Don't count something as "have" because it sounds related - be honest; a missing requirement is
   useful information for the candidate. Ignore boilerplate (benefits, "team player", company blurb).
   - "partial" means real evidence of the SAME thing, only less of it - not something adjacent
     (notebooks on Google Colab are not cloud-platform experience).
   - A number the candidate doesn't meet (minimum GPA, years of experience) is "missing", never
     "partial" - it is usually a hard screening filter, and the candidate needs to see that.
   - Evidence must respect the profile's skill levels: a language marked "built with AI" or
     "coursework" is never described as mastered or fluent.
2. projects: choose exactly 2 projects (by id) that best prove the requirements. Rough guide,
   unless the posting clearly points elsewhere: AI engineer / AI solutions -> jobbot + scanmate;
   software engineer -> jobbot + scanmate (mobile roles: video_platform); data roles -> jobbot +
   cancer_atlas. jobbot is the strongest and most recent project. Order the 2 projects by
   relevance to the job. For each, rank ALL its bullet numbers from most to least relevant to this
   job. The ranking does NOT change the order on the resume - the bullets always appear in their
   approved order, which tells the project's story; it only decides which bullet is removed first
   if the page runs out of space.
   rewordings: leave this EMPTY unless a small phrasing change to a bullet would clearly help a
   recruiter see something this posting asks for (e.g. the posting's name for the same thing).
   Same facts, nothing added, about the same length. Never more than 2. Most drafts need none.
3. skills: build the sidebar from the skill pool ONLY, using the exact item spellings. There is no
   length target - every item must be justified, and a short sidebar is better than filler. Give
   each item a layer:
   - "asked": the posting names it (or plainly the same thing). because = the posting's words.
   - "related": a close substitute for something the posting asks for that the candidate lacks or
     that the posting names more broadly (Kotlin -> Java, PostgreSQL -> MySQL, "REST APIs" ->
     FastAPI). because = the posting's words it answers.
   - "core": Java, Python, SQL, Git, GitHub, Claude Code - always included, whatever the job.
     Nothing else is core.
   - "role": typical for this kind of role even though the posting doesn't name it (e.g. React for
     a full-stack role). Only skills from the chosen projects' "fillers" lists. At most 3 in
     total; because = why. Leave everything else out - e.g. don't
     list several databases or front-end frameworks the posting gives no reason for.
   Also add a group named exactly "Relevant Coursework" with 2-4 courses from that pool group, the
   ones most relevant to this job (e.g. cyber -> Secure Programming, Computer Networks; AI ->
   Machine Learning, Artificial Intelligence). Courses go only in that group.
   Order ALL groups - including Relevant Coursework, AI-Assisted Development and Development Tools -
   from most to least relevant to THIS job; the least relevant group is cut first if space runs
   out.
   extras: then rank up to 10 MORE skills (not already used), most relevant to this kind of job
   first, layer "extra", because = what they show. ONLY skills in the "fillers" lists of the 2
   projects you chose - the ones the candidate is comfortable talking about from a project on this
   resume. Their other skills appear only when the posting asks for them. Code adds them to fill
   leftover sidebar space and cuts them first, so a job that asks for little still gets a full
   sidebar.
   Prefer fewer, fuller groups: avoid a group with a single item (Claude Code alone under
   AI-Assisted Development is fine). Never invent combined group names - use the pool's group
   names. Never name the programming-language group "Languages". Never add a skill the candidate
   lacks, even if the posting requires it - that belongs in requirements as "missing".
4. about: a short positioning statement in the style of <main_about>. The candidate presents
   themselves by the title they are applying for, not by their current status - this is
   deliberate, so never write "graduate", "student", "aspiring", "junior" or "entry-level". The
   title is "Software Engineer", or "AI Engineer" when the role is clearly AI engineering.
   - Sentence 1: "<Title> who builds <1-2 broad areas>", optionally followed by the purpose
     ("that solve real-world problems"). Areas ONLY from this list - each is backed by the
     candidate's projects: AI-powered applications, backend systems, data-driven applications,
     automation tools, end-to-end products, computer vision applications, mobile apps. Pick the
     areas closest to this job. For a role the candidate doesn't closely fit, pick the closest
     GENERAL area - never a technique, an architecture term or a niche overlap with the posting
     (never "multi-stage processing pipelines"), and never anything you marked "missing" or
     "partial" in requirements. Two areas sharing a noun are combined ("data-driven and
     AI-powered applications", not "data-driven applications and AI-powered applications").
   - Sentence 2: "Strong foundation in ..." + 3-4 DISCIPLINES (not tasks) that the profile backs
     and that matter for this job: system design, system architecture, backend development, API
     design, databases, data analysis, machine learning, LLM integration, evaluation and testing,
     algorithms, problem solving, technical decision-making, end-to-end product development.
     Never narrow tasks ("latency analysis", "dynamic SQL query design") and never words that mean
     something else in this posting's field ("performance evaluation" for a hardware role).
   about_personal: a second version for the candidate to choose from. The SAME sentence 1; then
   how the candidate works as a person and teammate, aimed at what THIS posting asks for:
   - First fill soft_skills_asked: quote the soft skills / personal traits the posting asks for
     ("team player", "excellent communication skills", "ownership", ...). Empty if none.
   - Then answer them with the 2-3 best-fitting traits from this list only (backed by the
     profile): ownership (builds things end to end, builds tools they actually use); clear
     communication (keeps people updated, raises problems early, writes trade-offs down openly);
     teamwork (owns a clear part of a shared project and delivers it); adapting to change
     (re-plans and still delivers when a partner drops out or an approach stops working); creative
     problem solving (combines imperfect pieces into something that works); persistence and a
     can-do attitude toward hard, open-ended problems; deciding from data and testing (measures
     results, checks AI output instead of trusting it blindly); learning new tools quickly.
   - Only if the posting asks for no soft skills, fall back by role type: AI roles - creative
     problem solving, can-do attitude, deciding from data and testing; data roles - adapting to
     change plus ownership or communication; other roles - ownership plus communication.
   - Write sentence 2 led by VERBS, as one natural sentence - e.g. "Takes full ownership from
     design to delivery, learns new tools quickly, and adapts as requirements change." Never the
     "Brings X, Y, and Z" pattern (it turns traits into a stiff list of nouns).
   - Put the chosen traits in traits_used.
   Both versions: no tool, library, framework or language names (they're in Projects and the
   sidebar), no project names, no metrics, no "experienced in", no "passionate", no years of
   experience, never military service or reserve duty (it has its own section), no stock phrases
   ("team player", "self-starter", "go-getter", "detail-oriented", "results-driven",
   "fast-paced"). Don't claim anything the profile doesn't show (e.g. production deployment,
   leading teams).
"""


def _profile_for_prompt() -> str:
    """The profile without the sections and bullets the model doesn't need (see above)."""
    text = paths.PROFILE_PATH.read_text(encoding="utf-8")
    for header in _PROFILE_SECTIONS_LEFT_OUT:
        start = text.find(header)
        if start == -1:
            continue
        nxt = text.find("\n## ", start + len(header))
        text = text[:start] + (text[nxt + 1:] if nxt != -1 else "")
    kept, skipping = [], False
    for line in text.split("\n"):
        if line.startswith(_PROFILE_BULLETS_LEFT_OUT):
            skipping = True          # drop the bullet and its indented continuation lines
            continue
        if skipping and line.startswith("  "):
            continue
        skipping = False
        kept.append(line)
    return "\n".join(kept).strip()


def _blocks_for_prompt(blocks: dict) -> str:
    lines = ["PROJECTS (id, title, numbered bullets):"]
    for p in blocks["projects"]:
        if p.get("unavailable"):  # e.g. no public link yet - never offered to the model
            continue
        lines.append(f"- id: {p['id']} | {p['title']}")
        lines += [f"    {i}. {b}" for i, b in enumerate(p["bullets"], 1)]
        if p.get("skills"):
            lines.append(f"    skills this project used: {', '.join(p['skills'])}")
            lines.append(f"    fillers (comfortable talking about): {', '.join(p.get('fillers', [])) or 'none'}")
    lines.append("\nSKILL POOL (group: items):")
    lines += [f"- {g['group']}: {', '.join(g['items'])}" for g in blocks["skill_pool"]]
    return "\n".join(lines)


def _call_model(prompt: str, schema=None, system: str | None = None):
    """One structured call, with the project's shared throttle and retry policy.
    Returns (parsed response, usage)."""
    client = gf._get_client()
    for attempt in range(gf.MAX_RETRIES + 1):
        gf._throttle()
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system or SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=schema or TailoredDraft,
                    temperature=0,
                    # "low" halves drafting time (~17s vs 30-55s) with the same picks - compared
                    # on the 3 test jobs 2026-11-12: same projects, near-identical sidebar, same
                    # requirement calls, equivalent About Me.
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            if response.parsed is None:
                raise RuntimeError("The model returned no usable draft")
            return response.parsed, response.usage_metadata
        except errors.APIError as e:
            if (e.code != 429 and e.code < 500) or attempt == gf.MAX_RETRIES:
                raise
            delay = gf._extract_retry_delay(e) if e.code == 429 else gf.DEFAULT_RETRY_DELAY_SECONDS
            time.sleep(delay)
        except (httpx.TransportError, httpcore.RemoteProtocolError):
            # A dropped connection ("Server disconnected without sending a response") - transient.
            if attempt == gf.MAX_RETRIES:
                raise
            time.sleep(gf.DEFAULT_RETRY_DELAY_SECONDS)
    raise RuntimeError("unreachable")


_C_RE = re.compile(r"(?<![\w+#.])C(?![\w+#-])")  # the language C, not C++ / C# / "C-level"
_ABOUT_RED_FLAGS = [(re.compile(r"\bpassionate\b", re.I), 'uses "passionate"'),
                    (re.compile(r"\b\d+\+?\s*(years?|yrs)\b", re.I), "mentions years of experience"),
                    (re.compile(r"\b(graduate|student|aspiring|junior|entry[- ]level)\b", re.I),
                     "describes you by status, not by title"),
                    (re.compile(r"\bexperienced in\b", re.I), 'says "experienced in"'),
                    (re.compile(r"\b(military|reserv\w*|army|IDF|combat|soldier)\b", re.I),
                     "mentions the military - it has its own section"),
                    # The model kept writing this for a hardware role despite the prompt: there it
                    # means modeling chip throughput/power, which the candidate hasn't done.
                    (re.compile(r"\bperformance (analysis|evaluation|modell?ing)\b", re.I),
                     'says "performance analysis/evaluation" - in hardware/systems postings that '
                     "means something you haven't done")]
_CLICHE_RE = re.compile(r"\b(team player|self[- ]starter|go[- ]getter|detail[- ]oriented|results[- ]driven|fast[- ]paced)\b", re.I)
# Domain words are fine in About Me; tool names are not (they're in Projects and the sidebar).
_ABOUT_OK_POOL_TERMS = {"Computer Vision", "AI Agents", "LLM APIs", "Prompt Engineering"}


def _to_selection(draft: TailoredDraft, blocks: dict, job: dict, notes: list[str],
                  reasons: dict) -> tuple[dict, dict]:
    """Model output -> (a valid selection, {project_id: bullets in drop order}). `reasons` is
    filled with {skill: {"layer", "because"}} for every sidebar item.

    Bullets always come out in their approved story order; the model's ranking only becomes the
    drop order used by _fit(). Every rule Python had to enforce is recorded in `notes`."""
    projects = content.projects_by_id(blocks)
    chosen, drop_order, seen = [], {}, set()
    for pick in draft.projects:
        if pick.id not in projects:
            notes.append(f"Dropped unknown project '{pick.id}'")
            continue
        if projects[pick.id].get("unavailable"):
            notes.append(f"Dropped '{pick.id}' - {projects[pick.id]['unavailable']}")
            continue
        if pick.id in seen:
            continue
        seen.add(pick.id)
        approved = projects[pick.id]["bullets"]
        ranked = []
        for n in pick.bullets:
            if 1 <= n <= len(approved) and n not in ranked:
                ranked.append(n)
        # Bullets the model left out are kept too (the story needs them) - ranked last.
        ranked += [n for n in range(1, len(approved) + 1) if n not in ranked]
        chosen.append({"id": pick.id, "bullets": list(approved)})          # approved order
        drop_order[pick.id] = [approved[n - 1] for n in reversed(ranked)]  # least relevant first
    if len(chosen) > MAX_PROJECTS:
        notes.append(f"Kept the first {MAX_PROJECTS} of {len(chosen)} projects")
        chosen = chosen[:MAX_PROJECTS]

    pool = {item for g in blocks["skill_pool"] for item in g["items"]}
    courses = next((set(g["items"]) for g in blocks["skill_pool"] if g["group"] == COURSEWORK_GROUP), set())
    pool_group = {item: g["group"] for g in blocks["skill_pool"] for item in g["items"]}
    # Unescape Markdown first: scraped descriptions write C# as "C\#", which would read as plain C.
    # Fillers: per project, the skills the user is comfortable talking about (blocks.json
    # "fillers") - only from the projects on this resume. Any other skill needs the posting to ask
    # for it (asked/related); "role" and "extra" items must be fillers.
    fillers = {s for c in chosen for s in projects[c["id"]].get("fillers", [])}
    names_c = bool(_C_RE.search(f"{job.get('title', '')}\n{job.get('description', '')}".replace("\\", "")))
    skills, used, picked_courses, role_count = [], set(), [], 0
    for g in draft.skills:
        name = g.group.strip()
        if name.lower() in ("languages", "language"):
            name = "Programming"
        items = []
        for it in g.items:
            item = it.name
            if item not in pool:
                notes.append(f"Dropped '{item}' from the sidebar - not in your skill pool")
            elif item in used:
                continue
            elif item in courses:  # courses live only in the coursework group (collected below)
                if len(picked_courses) < MAX_COURSES:
                    picked_courses.append(item)
                    reasons[item] = {"layer": it.layer if it.layer != "core" else "role", "because": it.because}
                used.add(item)
            elif item == "C" and not names_c:
                notes.append("Dropped 'C' from the sidebar - the job doesn't ask for C")
            elif it.layer == "core" and item not in CORE_SKILLS:
                notes.append(f"Dropped '{item}' - the posting gives no reason for it")
            elif it.layer == "role" and item not in fillers and item not in CORE_SKILLS:
                notes.append(f"Dropped '{item}' - the posting doesn't ask for it, and it's an only-if-asked skill")
            elif it.layer == "role" and role_count >= MAX_ROLE_SKILLS:
                notes.append(f"Dropped '{item}' - already {MAX_ROLE_SKILLS} skills the posting doesn't name")
            else:
                role_count += it.layer == "role"
                items.append(item)
                used.add(item)
                reasons[item] = {"layer": "core" if item in CORE_SKILLS else it.layer, "because": it.because}
        if name == COURSEWORK_GROUP:
            skills.append({"group": COURSEWORK_GROUP, "items": None})  # placeholder keeps its position
        elif items:
            skills.append({"group": name, "items": items})
    # Courses: one group, at the position the model gave it (or last if it forgot the group).
    if picked_courses:
        slot = next((g for g in skills if g["group"] == COURSEWORK_GROUP), None)
        if slot is None:
            skills.append(slot := {"group": COURSEWORK_GROUP, "items": None})
        slot["items"] = picked_courses
    skills = [g for g in skills if g["items"]]
    # Core skills are always on the sidebar - add any the model left out, into their usual group.
    for item in CORE_SKILLS:
        if item in used or item not in pool:
            continue
        target = next((g for g in skills if g["group"] == pool_group[item]), None)
        if target is None:
            target = {"group": pool_group[item], "items": []}
            skills.append(target)
        target["items"].append(item)
        reasons[item] = {"layer": "core", "because": ""}
        used.add(item)
    # Extras: fill leftover space, most relevant first. Each joins its usual group (or a new group
    # at the end); _fit() removes them, least relevant first, before touching anything else.
    extras_added = []
    for it in draft.extras[:MAX_EXTRAS]:
        item = it.name
        if item not in pool or item in used or item in courses or (item == "C" and not names_c):
            continue
        if item not in fillers:
            continue
        target = next((g for g in skills if g["group"] == pool_group[item]), None)
        if target is None:
            target = {"group": pool_group[item], "items": []}
            skills.append(target)
        target["items"].append(item)
        used.add(item)
        extras_added.append(item)
        reasons[item] = {"layer": "extra", "because": it.because}
    extras_set = set(extras_added)
    _fix_lonely_groups(skills, blocks, fillers, extras_set, reasons)
    extras_added += [i for i in extras_set if i not in extras_added]  # top-ups are trimmed first too
    reasons["_extras_order"] = extras_added
    for g in skills:
        if "Java" in g["items"]:  # the user's interview language - always the first one listed
            g["items"].remove("Java")
            g["items"].insert(0, "Java")

    for label, text in (("About Me", draft.about), ("About Me (personal)", draft.about_personal)):
        for pattern, problem in _ABOUT_RED_FLAGS:
            if pattern.search(text):
                notes.append(f"{label} {problem} - against your resume rules, check it")
        tools = [item for item in pool - _ABOUT_OK_POOL_TERMS
                 if re.search(rf"(?<![\w+#]){re.escape(item)}(?![\w+#])", text)]
        if tools:
            notes.append(f"{label} names tools ({', '.join(sorted(tools))}) - those belong in the sidebar, check it")
        cliches = _CLICHE_RE.findall(text)
        if cliches:
            notes.append(f"{label} uses stock phrases ({', '.join(cliches)}) - check it")
        if label.endswith("(personal)") and re.search(r"\bBrings\b", text):
            notes.append(f'{label} uses the "Brings X, Y, and Z" list - a verb-led sentence reads better')
    return {"about": draft.about.strip(), "projects": chosen, "skills": skills}, drop_order


_SHORTEN_INSTRUCTION = """You shorten a resume's About Me paragraph so it fits its space. Keep the
first sentence's title and focus, keep the same meaning and style, cut words rather than ideas
where possible, and add nothing new - no new claims, tools or traits."""


def _fit_about(selection: dict, blocks: dict, options: dict, notes: list[str]) -> None:
    """Make every About Me version fit in render.MAX_ABOUT_LINES lines (measured, not guessed).
    `options` is {label: text}, edited in place; the strengths version is the one on the resume."""
    resume = content.assemble(selection, blocks)
    for attempt in range(3):
        labels = list(options)
        counts = render.about_line_counts(resume, [options[k] for k in labels])
        too_long = {k: n for k, n in zip(labels, counts) if n > render.MAX_ABOUT_LINES}
        if not too_long:
            break
        if attempt == 2:
            for k, n in too_long.items():
                notes.append(f"About Me ({k}) is still {n} lines after shortening - max {render.MAX_ABOUT_LINES}, shorten it by hand")
            break
        for k, n in too_long.items():
            text = options[k]
            budget = int(len(text) * render.MAX_ABOUT_LINES / n * 0.93)  # characters per line x 4, a bit under
            short, _ = _call_model(f"Shorten to at most {budget} characters:\n\n{text}",
                                   schema=Shortened, system=_SHORTEN_INSTRUCTION)
            notes.append(f"Shortened the {k} About Me from {n} lines to fit {render.MAX_ABOUT_LINES}")
            options[k] = short.text.strip()
    selection["about"] = options["Strengths"]


# A one-item group is allowed for these; any other lone item gets company from its own pool group
# (fillers of the projects on this resume) - groups are never merged under joined titles (user preference).
_LONE_OK = ("AI-Assisted Development", COURSEWORK_GROUP)


def _fix_lonely_groups(skills: list[dict], blocks: dict, fillers: set[str], extras: set[str],
                       reasons: dict | None = None, may_add: bool = True) -> None:
    """Fewer one-line sidebar groups (user preference). A lone item's group is topped up with more
    items from the same pool group that are on the main sidebar (marked as extras) when `may_add`;
    if it is still alone and it is only an extra, it's left off. A lone asked/related/core item
    keeps its line - it earned it."""
    pool_group = {item: g["group"] for g in blocks["skill_pool"] for item in g["items"]}
    on_sidebar = {i for g in skills for i in g["items"]}
    for g in list(skills):
        if len(g["items"]) != 1 or g["group"] in _LONE_OK:
            continue
        home = pool_group.get(g["items"][0])
        if may_add:
            top_ups = [i for grp in blocks["skill_pool"] if grp["group"] == home for i in grp["items"]
                       if i in fillers and i not in on_sidebar]
            for item in top_ups[:2]:
                g["items"].append(item)
                on_sidebar.add(item)
                extras.add(item)
                if reasons is not None:
                    reasons[item] = {"layer": "extra", "because": "on your main resume"}
        if len(g["items"]) == 1 and g["items"][0] in extras:
            skills.remove(g)


def _rewordings(draft: TailoredDraft, blocks: dict) -> list[dict]:
    """The model's rewording suggestions, tied to the exact approved bullet they replace.
    Never applied here - the editor shows them with a "Use this wording" button."""
    projects = content.projects_by_id(blocks)
    out = []
    for r in draft.rewordings[:2]:
        bullets = projects.get(r.project_id, {}).get("bullets", [])
        if 1 <= r.bullet <= len(bullets) and r.text.strip() and r.text.strip() != bullets[r.bullet - 1]:
            out.append({"project": r.project_id, "original": bullets[r.bullet - 1],
                        "text": r.text.strip(), "why": r.why})
    return out


def _fit(selection: dict, drop_order: dict, blocks: dict, notes: list[str], drop_extras: list[str] = ()) -> dict:
    """Drop the least relevant content until the page fits: the lowest-ranked bullet of the
    project with the most bullets left (the others keep their story order), or the last non-core
    item of the least relevant (last) sidebar group."""
    titles = {p["id"]: p["title"].split(" – ")[0] for p in blocks["projects"]}
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "draft.pdf"
        for _ in range(30):
            try:
                render.render(content.assemble(selection, blocks), pdf)
                return selection
            except render.ResumeCheckError as e:
                msg = str(e)
            if "sidebar overflows" in msg:
                # Least relevant extra first; then the last (least relevant) group; never a core skill.
                extras = [i for i in reversed(drop_extras) if any(i in g["items"] for g in selection["skills"])]
                if extras:
                    victim = next((g, extras[0]) for g in selection["skills"] if extras[0] in g["items"])
                else:
                    victim = next(((g, i) for g in reversed(selection["skills"]) for i in reversed(g["items"])
                                   if i not in CORE_SKILLS), None)
                if victim is None:
                    raise RuntimeError("The sidebar doesn't fit even with only the core skills")
                group, dropped = victim
                group["items"].remove(dropped)
                if not group["items"]:
                    selection["skills"].remove(group)
                if dropped not in drop_extras:  # trimming extras is the plan, not news
                    notes.append(f"Dropped '{dropped}' from the sidebar to fit the page")
            elif "main column overflows" in msg or "links missing" in msg:
                fullest = max(selection["projects"], key=lambda p: len(p["bullets"]))
                if len(fullest["bullets"]) <= 1:
                    raise RuntimeError("The draft doesn't fit on one page even with one bullet per project")
                victim = next(b for b in drop_order[fullest["id"]] if b in fullest["bullets"])
                fullest["bullets"].remove(victim)
                notes.append(f'Dropped the least relevant {titles[fullest["id"]]} bullet to fit the page: "{victim[:60]}…"')
            else:
                raise RuntimeError(f"Render check failed: {msg}")
    raise RuntimeError("Couldn't make the draft fit on one page")


def draft_for_job(job: dict) -> dict:
    """Draft a resume for `job` ({title, company, description, ...}).

    Output: {"selection", "requirements", "project_reasons", "about_why", "rewordings", "notes",
             "model", "tokens"} - `selection` is valid (checked) and fits on one page.
    """
    blocks, main = content.load_blocks(), content.load_main()
    # Stable parts first, the job last. Each part is wrapped in a tag: the profile has its own
    # markdown headings, so heading-delimited sections would blur into each other.
    prompt = (
        f"<profile>\n{_profile_for_prompt()}\n</profile>\n\n"
        f"<resume_blocks>\n{_blocks_for_prompt(blocks)}\n</resume_blocks>\n\n"
        f"<main_about>\n{main['about']}\n</main_about>\n\n"
        # The main About Me is exactly the space available, so its length is the budget for both
        # versions - writing to length reads better than shortening afterwards (_fit_about is the net).
        f"<length_limit>\nBoth About Me versions must fit in {render.MAX_ABOUT_LINES} lines on the resume: "
        f"at most {len(main['about'])} characters each, including spaces - <main_about> is exactly that "
        f"long. Use complete, natural sentences, not a list of traits.\n</length_limit>\n\n"
        f"<job_posting>\nTitle: {job.get('title', '')}\nCompany: {job.get('company', '')}\n\n"
        f"{job.get('description') or '(no description)'}\n</job_posting>"
    )
    draft, usage = _call_model(prompt)

    notes: list[str] = []
    skill_reasons: dict = {}
    selection, drop_order = _to_selection(draft, blocks, job, notes, skill_reasons)
    check = content.check(selection, blocks)
    if not check.ok:
        raise RuntimeError("The draft broke a resume rule: " + "; ".join(check.errors))
    abouts = {"Strengths": draft.about.strip(), "Personal": draft.about_personal.strip()}
    _fit_about(selection, blocks, abouts, notes)
    if any("still" in n and "About Me (Strengths)" in n for n in notes):
        selection["about"] = main["about"]  # the resume must still pass the line check
        notes.append("Used your main About Me on the resume until the Strengths version is shortened")
    selection = _fit(selection, drop_order, blocks, notes, skill_reasons.pop("_extras_order", []))
    extras = {k for k, v in skill_reasons.items() if isinstance(v, dict) and v.get("layer") == "extra"}
    # Trimming can leave a one-item group: now only drop lone extras (adding could overflow again).
    _fix_lonely_groups(selection["skills"], blocks, set(), extras, may_add=False)
    on_sidebar = {i for g in selection["skills"] for i in g["items"]}
    skill_reasons = {k: v for k, v in skill_reasons.items() if k in on_sidebar}

    titles = {p["id"]: p["title"].split(" – ")[0] for p in blocks["projects"]}
    return {
        "selection": selection,
        "requirements": [r.model_dump() for r in draft.requirements],
        "project_reasons": {p.id: f"{titles.get(p.id, p.id)}: {p.why}" for p in draft.projects
                            if p.id in {c['id'] for c in selection['projects']}},
        "about_why": draft.about_why,
        "about_options": [{"style": "Strengths", "text": abouts["Strengths"]},
                          {"style": "Personal", "text": abouts["Personal"],
                           "asked": draft.soft_skills_asked, "traits": draft.traits_used}],
        "rewordings": _rewordings(draft, blocks),
        "skill_reasons": skill_reasons,
        "notes": notes,
        "model": MODEL,
        "tokens": getattr(usage, "total_token_count", None),
        # Where the tokens go - input vs. the JSON answer vs. the model's hidden reasoning, and how
        # much of the input was served from Gemini's implicit cache (cheaper).
        "token_breakdown": {k: getattr(usage, f, None) for k, f in (
            ("input", "prompt_token_count"), ("cached_input", "cached_content_token_count"),
            ("output", "candidates_token_count"), ("thinking", "thoughts_token_count"))},
    }
