"""
extractor.py
Turns raw meeting notes text into a list of structured task dicts.

Three extraction tiers, tried in order:
  1. LLM extraction via the Claude API (real production path; used when
     ANTHROPIC_API_KEY is set).
  2. Cached extraction (demo mode) - for the sample meeting notes shipped
     with this prototype, a pre-generated extraction (produced by Claude,
     read from cache/<meeting_id>.json) is used so the demo runs end to
     end with no API key required.
  3. Rule-based fallback - a simple regex/heuristic extractor used as a
     last resort for meeting notes that are neither run through the API
     nor cached. Much lower quality, but keeps the tool functional
     offline.

Every extracted task uses this schema (matches the tracker columns):
  workstream, task, description, owner, status, priority,
  due_date, blocker, follow_up_needed, notes
`last_updated` is NOT part of the raw extraction - it's stamped on by the
pipeline using the meeting date, since that's a property of the run, not
something the model needs to infer.
"""

import hashlib
import json
import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # picks up a .env file in the project root if present, e.g. ANTHROPIC_API_KEY=sk-ant-...
except ImportError:
    pass  # python-dotenv is optional - `export ANTHROPIC_API_KEY=...` in your shell still works fine without it

CACHE_DIR = Path(__file__).parent / "cache"

VALID_STATUSES = {"Not Started", "In Progress", "Pending Review", "Blocked", "Completed", "Pending"}

TASK_SCHEMA_FIELDS = [
    "workstream", "task", "description", "owner", "status",
    "priority", "due_date", "blocker", "follow_up_needed", "notes",
]

EXTRACTION_SYSTEM_PROMPT = """You are a project-management assistant. You read raw meeting notes \
(Gemini/Fireflies-style summaries, transcripts, or manual notes) and extract a clean list of \
project tasks from them.

Rules:
- Extract only real, actionable tasks or clearly-stated work items - not small talk, scheduling \
chit-chat, or vague mentions with no action.
- Group each task under the most specific workstream/project it belongs to (e.g. "AI Visibility \
Tool - Registration", "Catalog Agents - Metering"), not just a generic company name.
- owner: the person's full name if stated. If no owner is stated or ownership is ambiguous \
(e.g. "the group", "someone", multiple people arguing about who owns it), use "TBD" and explain \
the ambiguity in notes.
- status: one of Not Started, In Progress, Pending Review, Blocked, Completed. If the note does \
not clearly state a status, use "Not Started" and say why in notes (e.g. "no status given").
- priority: High, Medium, or Low - infer from urgency language ("today", "ASAP", "blocking a \
demo") if not stated explicitly.
- due_date: ISO date (YYYY-MM-DD) if stated or clearly inferable (e.g. "by end of day" -> the \
meeting date), else leave blank.
- blocker: a short phrase describing what's blocking the task (e.g. "waiting on API key from \
Yatharth", "unclear ownership", "duplicate work with another task"), or blank if none.
- follow_up_needed: "Yes" if the task has a blocker, no clear owner, no clear status, or has \
gone quiet with no update; otherwise "No".
- notes: any useful context a PM would want - caveats, dependencies, who else is involved.
- description: a 1-2 sentence expansion of the task using details from the notes.

Return ONLY a JSON array of task objects, no markdown fences, no commentary. Each object must \
have exactly these keys: workstream, task, description, owner, status, priority, due_date, \
blocker, follow_up_needed, notes."""


def _meeting_id_from_path(path: str) -> str:
    return Path(path).stem


def _call_claude_api(meeting_text: str) -> list[dict]:
    """Real production extraction path. Requires ANTHROPIC_API_KEY."""
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Meeting notes:\n\n{meeting_text}"}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


def _load_cached_extraction(meeting_id: str) -> list[dict] | None:
    cache_path = CACHE_DIR / f"{meeting_id}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return None


def _rule_based_extraction(meeting_text: str) -> list[dict]:
    """
    Last-resort fallback with no LLM involved at all. Looks for the
    "[Owner] Verb Phrase: description." pattern Gemini notes commonly use
    in their "Next steps" section, plus a generic "Name will/is working on X"
    pattern. Deliberately conservative - low recall, but what it does
    extract is reasonably precise, and it's cheap and always available.
    """
    tasks = []
    bracket_pattern = re.compile(r"\[([^\]]+)\]\s*([^:]+):\s*([^\n]+)")
    for m in bracket_pattern.finditer(meeting_text):
        owner, title, desc = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if owner.lower() in ("the group",):
            owner = "TBD"
        tasks.append({
            "workstream": "Unsorted / Needs Review",
            "task": title,
            "description": desc,
            "owner": owner,
            "status": "Not Started",
            "priority": "Medium",
            "due_date": "",
            "blocker": "",
            "follow_up_needed": "Yes" if owner == "TBD" else "No",
            "notes": "Extracted by rule-based fallback (no LLM available) - "
                      "verify workstream, status, and priority manually.",
        })

    verb_pattern = re.compile(
        r"([A-Z][a-z]+ [A-Z][a-z]+) (?:will|is currently|is)\s+(work(?:ing)? on|continu(?:e|ing) to|"
        r"lead(?:ing)?|handl(?:e|ing))\s+([^\n.]+)\.",
    )
    for m in verb_pattern.finditer(meeting_text):
        owner, _verb, desc = m.group(1).strip(), m.group(2), m.group(3).strip()
        tasks.append({
            "workstream": "Unsorted / Needs Review",
            "task": desc[:80],
            "description": desc,
            "owner": owner,
            "status": "In Progress",
            "priority": "Medium",
            "due_date": "",
            "blocker": "",
            "follow_up_needed": "No",
            "notes": "Extracted by rule-based fallback (no LLM available) - "
                      "verify workstream, status, and priority manually.",
        })

    return tasks


def extract_tasks(meeting_text: str, meeting_path: str | None = None) -> tuple[list[dict], str]:
    """
    Returns (tasks, method) where method is one of "llm-api", "cached-demo",
    or "rule-based-fallback", so callers/reviewers know how much to trust
    the output.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _call_claude_api(meeting_text), "llm-api"
        except Exception as e:
            print(f"  [!] Live Claude API extraction failed ({e}); falling back.")

    if meeting_path:
        cached = _load_cached_extraction(_meeting_id_from_path(meeting_path))
        if cached is not None:
            return cached, "cached-demo"

    return _rule_based_extraction(meeting_text), "rule-based-fallback"
