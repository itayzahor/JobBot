"""Resume content: the approved building blocks, and resumes as selections from them.

`blocks.json` is the single source of approved resume content:
- `fixed`: name, education, military, contact, languages - copied onto every resume unchanged;
- `about_versions`: approved About Me texts (the main resume and drafts start from these);
- `projects`: every project with its approved bullets, title and link;
- `skill_pool`: every sidebar skill that may appear, grouped.

A resume - the main one or a per-job one - is a *selection*:
    {"about": str,
     "projects": [{"id": "<project id>", "bullets": [str, ...]}, ...],
     "skills": [{"group": str, "items": [str, ...]}, ...]}
`assemble()` merges the fixed blocks and project titles/links in, producing the full resume dict
`render.render()` takes.

`check()` compares a selection against the blocks. Hard problems (unknown project, skill outside
the pool) make the resume invalid. Soft ones are warnings shown to the user: a bullet whose wording
isn't approved is allowed when the user edited it on purpose.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from jobbot import paths


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)    # resume must not be used as is
    warnings: list[str] = field(default_factory=list)  # allowed, but the user should know

    @property
    def ok(self) -> bool:
        return not self.errors


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_blocks() -> dict:
    return load_json(paths.RESUME_BLOCKS_PATH)


def load_main() -> dict:
    return load_json(paths.RESUME_MAIN_PATH)


def projects_by_id(blocks: dict) -> dict[str, dict]:
    return {p["id"]: p for p in blocks["projects"]}


def check(selection: dict, blocks: dict) -> CheckResult:
    """Compare a selection with the approved blocks."""
    result = CheckResult()
    projects = projects_by_id(blocks)
    pool = {item for g in blocks["skill_pool"] for item in g["items"]}

    if not selection.get("about", "").strip():
        result.errors.append("About Me is empty")
    if not selection.get("projects"):
        result.errors.append("no projects selected")

    seen = set()
    for p in selection.get("projects", []):
        project = projects.get(p["id"])
        if project is None:
            result.errors.append(f"unknown project '{p['id']}'")
            continue
        if p["id"] in seen:
            result.errors.append(f"project '{project['title']}' is selected twice")
        seen.add(p["id"])
        if not p["bullets"]:
            result.errors.append(f"project '{project['title']}' has no bullets")
        for bullet in p["bullets"]:
            if bullet not in project["bullets"]:
                result.warnings.append(f"not approved wording ({project['title']}): {bullet}")

    for g in selection.get("skills", []):
        for item in g["items"]:
            if item not in pool:
                result.errors.append(f"skill '{item}' is not in the skill pool")
        if not g["items"]:
            result.errors.append(f"skill group '{g['group']}' is empty")
    return result


def assemble(selection: dict, blocks: dict) -> dict:
    """Full resume dict for render.render(): fixed blocks + the selection."""
    projects = projects_by_id(blocks)
    fixed = blocks["fixed"]
    return {
        "name": fixed["name"],
        "about": selection["about"],
        "education": fixed["education"],
        "projects": [
            {"title": projects[p["id"]]["title"], "link": projects[p["id"]].get("link"), "bullets": p["bullets"]}
            for p in selection["projects"]
        ],
        "military": fixed["military"],
        "contact": fixed["contact"],
        "skills": selection["skills"],
        "languages": fixed["languages"],
    }
