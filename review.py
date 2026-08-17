"""
review.py
The human-in-the-loop gate. Nothing extracted by extractor.py or merged by
tracker.py ever reaches the tracker CSV without passing through here.

For each proposed change (new task or update to an existing task) the
reviewer sees:
  - the action (ADD a new row / UPDATE an existing row)
  - the full proposed row
  - for updates, exactly which fields changed and from what to what
and chooses one of:
  [a] approve as-is
  [e] edit a field, then approve
  [i] ignore (skip - not written to the tracker at all)

`review_proposals()` is the real interactive CLI loop (used by
run_pipeline.py when run normally).

`simulate_review()` runs the exact same approve/edit/ignore logic but
takes the human's decisions as a pre-recorded list instead of reading
stdin, so the prototype can be demoed / tested non-interactively while
still exercising the real review code path end to end.
"""

from dataclasses import dataclass


@dataclass
class ReviewDecision:
    proposal_index: int
    action: str  # "approve" | "edit" | "ignore" | "split"
    edits: dict | None = None  # field -> new value, only used for "edit"

# "split" is for the case where the agent auto-matched a new task to an
# existing tracker row (workstream + title similarity), but the reviewer
# can see they're actually two different pieces of work (e.g. different
# owners, different specifics) that just happen to read similarly. It
# tells apply_decisions() to add the row as NEW instead of overwriting the
# matched row, no matter what the proposal's "action" said.


def _print_proposal(i: int, p: dict) -> None:
    action = p["action"].upper()
    print(f"\n--- [{i+1}] {action}: {p['row']['workstream']} / {p['row']['task']} ---")
    if p["action"] == "update":
        print(f"    Matched existing row. Changes: {p['diff']}")
    print(f"    Owner: {p['row']['owner']}   Status: {p['row']['status']}   "
          f"Priority: {p['row']['priority']}   Follow-up needed: {p['row']['follow_up_needed']}")
    if p["row"]["blocker"]:
        print(f"    Blocker: {p['row']['blocker']}")
    if p["row"]["notes"]:
        print(f"    Notes: {p['row']['notes']}")


def review_proposals(proposals: list[dict]) -> list[ReviewDecision]:
    """Interactive CLI review loop."""
    decisions = []
    print(f"\n===== Human review: {len(proposals)} proposed change(s) =====")
    for i, p in enumerate(proposals):
        _print_proposal(i, p)
        if p["action"] == "update":
            print("    (this was auto-matched to an existing row - type 's' if it's actually a different task)")
        while True:
            choice = input("    [a]pprove / [e]dit / [i]gnore" +
                            (" / [s]plit-as-new" if p["action"] == "update" else "") + " ? ").strip().lower()
            if choice in ("a", "approve"):
                decisions.append(ReviewDecision(i, "approve"))
                break
            elif choice in ("i", "ignore"):
                decisions.append(ReviewDecision(i, "ignore"))
                break
            elif choice in ("s", "split") and p["action"] == "update":
                decisions.append(ReviewDecision(i, "split"))
                break
            elif choice in ("e", "edit"):
                edits = {}
                print("    Enter field=value pairs, one per line. Blank line to finish.")
                print(f"    Editable fields: {', '.join(k for k in p['row'] if k != 'last_updated')}")
                while True:
                    line = input("    > ").strip()
                    if not line:
                        break
                    if "=" not in line:
                        print("    (format is field=value, try again)")
                        continue
                    field, value = line.split("=", 1)
                    edits[field.strip()] = value.strip()
                decisions.append(ReviewDecision(i, "edit", edits))
                break
            else:
                print("    Please type a, e, or i.")
    return decisions


def simulate_review(proposals: list[dict], scripted: list[ReviewDecision]) -> list[ReviewDecision]:
    """
    Non-interactive version of review_proposals for demos/tests. `scripted`
    must have one ReviewDecision per proposal (same order). Prints the same
    transcript a human reviewer would have seen, plus the scripted decision,
    so the review step is fully auditable even when automated.
    """
    print(f"\n===== Human review (scripted demo run): {len(proposals)} proposed change(s) =====")
    assert len(scripted) == len(proposals), "scripted decisions must cover every proposal"
    for i, p in enumerate(proposals):
        _print_proposal(i, p)
        d = scripted[i]
        if d.action == "approve":
            print("    -> [reviewer] approve")
        elif d.action == "ignore":
            print("    -> [reviewer] ignore")
        elif d.action == "edit":
            print(f"    -> [reviewer] edit: {d.edits}")
        elif d.action == "split":
            print("    -> [reviewer] split: this is actually a different task, not a real match - adding as new")
    return scripted


def apply_decisions(proposals: list[dict], decisions: list[ReviewDecision],
                     existing_rows: list[dict]) -> list[dict]:
    """
    Applies approved/edited decisions to the tracker rows. Ignored proposals
    are dropped entirely - they never touch the tracker.
    """
    rows = list(existing_rows)
    for p, d in zip(proposals, decisions):
        if d.action == "ignore":
            continue
        row = dict(p["new_only_row"]) if d.action == "split" else dict(p["row"])
        if d.action == "edit" and d.edits:
            row.update(d.edits)
        if d.action == "split" or p["action"] == "add":
            rows.append(row)
        else:
            rows[p["index"]] = row
    return rows
