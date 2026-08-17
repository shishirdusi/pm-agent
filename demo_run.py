"""
demo_run.py
Runs the full PM Agent pipeline across three real, consecutive standups
(Jul 29, Jul 30, Aug 2 2026) so you can see:
  - new tasks being added
  - the SAME task being correctly recognized and updated across meetings
    (not duplicated) as its status changes (In Progress -> Pending Review
    -> Completed)
  - the auto-matcher occasionally proposing a WRONG merge (two different
    people's different work, or a different task that just reads
    similarly), and the human reviewer catching and correcting it via
    [s]plit-as-new or [e]dit
  - a group-level, no-owner "task" that a human reviewer judges isn't
    actually a trackable task and [i]gnores

Review decisions here are scripted (see review.simulate_review) rather
than typed live, so this can run non-interactively, but they exercise the
exact same code path as a live human sitting at the CLI - see
run_pipeline.review_proposals for the interactive version.

Run: python demo_run.py
"""

from pathlib import Path

from review import ReviewDecision
from run_pipeline import run
import os
_DISABLED_KEY = os.environ.pop("ANTHROPIC_API_KEY", None)
if _DISABLED_KEY:
    print("[demo_run] Note: temporarily ignoring your ANTHROPIC_API_KEY for this demo "
          "script, since it replays a fixed script against the sample meetings. "
          "Use run_pipeline.py for live extraction on your own notes.\n")

TRACKER = Path(__file__).parent / "output" / "task_tracker.csv"
LOG_PATH = Path(__file__).parent / "output" / "demo_run_log.txt"

MEETINGS = [
    {
        "notes": "sample_meetings/2026-07-29_sanio-daily-standup.txt",
        "date": "2026-07-29",
        # 7 proposals, all "add" (tracker starts empty) - approve all,
        # with one edit to fill in a due date the reviewer knows from
        # context (Vamsi asked for this to be "presented at the next demo",
        # i.e. the Jul 30 standup).
        "decisions": [
            ReviewDecision(0, "approve"),
            ReviewDecision(1, "approve"),
            ReviewDecision(2, "approve"),
            ReviewDecision(3, "approve"),
            ReviewDecision(4, "edit", {"due_date": "2026-07-30"}),
            ReviewDecision(5, "approve"),
            ReviewDecision(6, "approve"),
        ],
    },
    {
        "notes": "sample_meetings/2026-07-30_sanio-daily-standup.txt",
        "date": "2026-07-30",
        # proposal 0 correctly auto-matches the metering task from meeting 1
        # (In Progress -> Pending Review) - approve.
        # proposal 5 ("switch standups to live demos") has no real owner
        # and isn't a trackable deliverable - a human reviewer ignores it.
        "decisions": [
            ReviewDecision(0, "approve"),   # metering: In Progress -> Pending Review (real update)
            ReviewDecision(1, "approve"),   # new: registration workflow (Yatharth's piece)
            ReviewDecision(2, "approve"),   # new: multi-URL integration
            ReviewDecision(3, "approve"),   # new: 5am IST data bug
            ReviewDecision(4, "approve"),   # new: send env file
            ReviewDecision(5, "ignore"),    # team-process decision, not a trackable task
            ReviewDecision(6, "approve"),   # new: procure Claude Pro accounts
        ],
    },
    {
        "notes": "sample_meetings/2026-08-02_sanio-daily-standup.txt",
        "date": "2026-08-02",
        # proposal 0 correctly auto-matches the registration task
        # (In Progress -> Completed) - approve, this is real dedupe working.
        # proposal 1 gets auto-matched to Yatharth's "multi-URL" task from
        # meeting 2, but it's actually Bindu's different sub-task (the
        # 5-product *input* piece vs Yatharth's *score consolidation*
        # piece) - the title similarity alone can't tell them apart.
        # Reviewer catches it and splits it into its own row.
        # proposal 5 gets auto-matched to Saumya's "review metering module
        # + share docs" task from meeting 1, but it's actually Sandeep
        # being asked to go READ that documentation - a different task
        # for a different person. Reviewer splits this one too.
        "decisions": [
            ReviewDecision(0, "approve"),   # registration: In Progress -> Completed (real update)
            ReviewDecision(1, "split"),     # false-positive match: Bindu's task, not Yatharth's - split
            ReviewDecision(2, "approve"),   # new: Tavi threshold bug
            ReviewDecision(3, "approve"),   # new: eval/golden-dataset approach
            ReviewDecision(4, "approve"),   # new: share rejected output with Lakshmanan
            ReviewDecision(5, "split"),     # false-positive match: Sandeep reading docs != Saumya's task - split
        ],
    },
]


def main():
    if TRACKER.exists():
        TRACKER.unlink()  # start clean so the demo is reproducible

    log_lines = []
    import io, contextlib

    for m in MEETINGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print(f"\n\n################ MEETING: {m['notes']} ({m['date']}) ################")
            run(Path(m["notes"]), m["date"], tracker_path=TRACKER,
                interactive=False, scripted_decisions=m["decisions"])
        text = buf.getvalue()
        print(text)
        log_lines.append(text)

    LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"\n\nFull run transcript saved to {LOG_PATH}")
    print(f"Final tracker saved to {TRACKER}")


if __name__ == "__main__":
    main()
