"""Authored manual content for `agents man <command>` — the use-facing manuals.

Kept in-package (ships with pip) and in ONE place so it's updated in the same PR
as the command it documents. It deliberately does NOT restate flags: `man` renders
the synopsis and options live from the argparse parser and the command's
docstring, so those can't drift. This module holds only what the parser can't —
when/why to reach for a command, the conventions, worked examples, and gotchas.

A test asserts every CORE command has an entry here and that no entry names a
command that no longer exists. If you add or change a command, update its manual
in the same change. If a manual is confusing, wrong, or you had to work around the
tool, that's a bug — open an issue (see ISSUE_NUDGE).
"""

REPO = "mekarpeles/agents"
ISSUE_NUDGE = (
    "Confusing, wrong, or did you have to work around the tool? That's worth an "
    f"issue — please file one:\n  gh issue create -R {REPO} "
    '-t "<what surprised you>" -b "<what you expected vs. what happened>"'
)

# Cross-cutting concept pages — `agents man <topic>`.
TOPICS = {
    "herdr": """\
herdr — where an agent's terminal lives

`herdr` is a workspace host (alongside `tmux` and `foreground`). Messaging is
always the native socket, so the host only decides where you can watch/attach a
session — never whether a message is delivered.

The placement rule (this is the whole model):
  • Every LEAD gets its OWN workspace (named after the lead).
  • TABS are used ONLY for a lead's `ada` reports — an ada opens as a tab inside
    its lead's workspace. A lead that reports to another lead still gets its own
    workspace, never a tab. So the only things nested as tabs are atomic adas.

Bring an agent up in herdr, or move a running one in:
  agents up <name> --host herdr            # a stopped agent: start it in herdr
  agents up --meta project=ol --host herdr # a whole set at once
  agents attach -f <name> --host herdr     # a LIVE agent: -f, because moving it
                                           # restarts the session (lossless resume)

A move keeps the sessionId and transcript (resumed with --resume), so it never
drops a message. Placement follows the rule above automatically from the agent's
type and team.""",

    "conventions": """\
conventions — naming, types, and where each fact canonically lives

TYPE comes from the NAME:
  • human-ish name (Dewey, Saul, Valerie) -> a LEAD (coordinates; owns a domain;
    may staff its own reports).
  • atomic, task-scoped name (pr-14212, modsec-daily) -> an ADA (one scoped job).
  If you're about to register a human-named agent as an ada, stop — it's a lead.

WHERE EACH FACT LIVES (don't overload one field for another's job):
  • team   — the ONE lead an agent reports to (`-t <lead>`). Colocation unit.
  • group  — arbitrary sub-labels for filtering (`-g solr,tags`); MANY per agent.
             NOT the project — project moved to meta.
  • meta.project — the project an agent belongs to (e.g. `--meta project=ol`).
  • meta.issue / meta.pr — the issue/PR an ada is working (so agent<->issue<->PR
             resolves by query, not by parsing the name).
  • meta.epics — a LEAD's list of epic issue #s (`--meta '{"epics":[14200,...]}'`).
  • role/desc — the operator's one-line statement of purpose (`--desc "..."`).
  • recap    — where the agent IS right now; the agent sets it via `agents checkin`.

Query by any of these: `agents ls --meta project=ol`, `--meta pr=14212`,
`--meta epics=14200` (list membership), `-g solr`, `--type lead`, `-t saul`.""",

    "recap": """\
recap — keeping "where we are" current, cheaply

A recap is a one-line status an agent sets about ITSELF — opt-in and agent-driven,
never computed, so it costs no tokens and only ever says what the agent chose to
record. It's timestamped, and each check-in is also appended to a journal
(~/.agents/<name>/recap.log), so there's a progress trail without any history
service.

  agents checkin "builder patched; 2 of 3 commits done, tests green"

Leads should cascade it: check yourself in, ask each report to check in. The
freshest recap is what `ls` shows in DESC and what `more` shows with its age.""",
}

# Per-command authored extras. `man <cmd>` always renders the live synopsis +
# options + docstring; these add examples / best-practices / pointers on top.
# (command, comment) example tuples read as `example  # comment`.
MANUALS = {
    "register": dict(
        best="A lead gets a human-ish name and its own herdr workspace; an ada "
             "gets an atomic name and becomes a tab under its lead. Put the "
             "project in --meta, not a group. If you register from inside an "
             "agent session, the new agent's team defaults to you (the caller).",
        examples=[
            ('agents register saul --type lead -g solr --desc "Search & Solr lead" '
             '--meta project=ol',
             "a lead: own workspace, purpose in --desc, project in meta"),
            ('agents register pr-14212 --type ada -t wally -g workflows '
             '--meta project=ol --meta issue=14209 --meta pr=14212 '
             '--cwd ~/Projects/openlibrary --prompt "Review PR #14212…"',
             "an ada: scoped name, reports to wally, issue/pr in meta"),
            ("agents register dewey --type lead --session-id <SID> --cwd ~/Projects/ada",
             "adopt an existing Claude session (resume it on `up`)"),
        ],
        see_also=["up", "meta", "herdr", "conventions"],
    ),
    "up": dict(
        best="`up` launches or RESUMES (by sessionId — never mis-attributes). It "
             "only starts the DOWN agents in a selection; to move an already-live "
             "agent to another host use `attach -f --host`. A fresh agent binds "
             "most reliably under tmux; resumes bind on any host.",
        examples=[
            ("agents up saul -d", "start/resume one agent, detached"),
            ("agents up -t oliver --host herdr", "a whole team into herdr"),
            ("agents up --meta project=ol --host herdr", "everyone on a project"),
        ],
        see_also=["down", "attach", "herdr"],
    ),
    "down": dict(
        best="Host-agnostic — it SIGTERMs the bound pid wherever it runs. The "
             "registry row and home are untouched; the agent resumes on next `up`.",
        examples=[("agents down saul", "stop one"),
                  ("agents down -t oliver", "stop a whole team"),
                  ("agents down --meta project=ol", "stop everyone on a project")],
        see_also=["up", "rm"],
    ),
    "attach": dict(
        best="Watch a running agent, or MOVE it to another host. Moving a live "
             "agent restarts it (lossless resume), so it needs -f. A stopped "
             "agent is just started on the target host.",
        examples=[("agents attach saul", "watch it in its tmux window"),
                  ("agents attach -f valerie --host herdr",
                   "move a LIVE agent into herdr (own workspace if a lead, else a tab)")],
        see_also=["up", "detach", "herdr"],
    ),
    "detach": dict(
        best="Leave a tmux/herdr session running without you attached.",
        examples=[("agents detach saul", "detach from one agent's window")],
        see_also=["attach"],
    ),
    "ls": dict(
        best="The registry view. Tree by default (reports nest under their lead); "
             "--flat for a table. Filter by -g/--type/-t/--meta; --json for "
             "machine output. DESC shows the freshest of recap -> desc -> task.",
        examples=[("agents ls", "the whole fleet as a tree"),
                  ("agents ls --meta project=ol", "one project (meta is canonical)"),
                  ("agents ls --json --meta pr=14212", "the agent owning a PR"),
                  ("agents ls -g solr --running", "live agents in a group")],
        see_also=["more", "find", "meta", "whois"],
    ),
    "find": dict(
        best="Searches past CLAUDE SESSIONS (transcripts), not the registry — use "
             "it to recover/adopt an agent. -a greps full transcript bodies.",
        examples=[("agents find dewey", "sessions whose title/cwd/prompt match"),
                  ("agents find -a 'tag vocabulary'", "deep search inside transcripts")],
        see_also=["register", "ls", "session"],
    ),
    "send": dict(
        best="The guaranteed way to message an agent (registry-resolved, host- "
             "independent). Agents normally message each other with SendMessage — "
             "resolve the target first with `agents whois <name>`; `send` is the "
             "fallback that always lands.",
        examples=[('agents send saul "status?"', "message by registry name"),
                  ('agents send oliver "PR #14212 is ready for review"', "report up")],
        see_also=["whois", "checkin"],
    ),
    "whois": dict(
        best="Bridges a registry NAME to Claude's live SendMessage address (the "
             "native name rotates and differs from the registry name). Use it "
             "before SendMessage; `agents send` is the guaranteed fallback.",
        examples=[("agents whois oliver", "print its uds: SendMessage target"),
                  ("agents whois saul --json", "machine-readable (for tooling)")],
        see_also=["send", "whoami"],
    ),
    "whoami": dict(
        best="Identifies THIS session's agent (by $CLAUDE_CODE_SESSION_ID). Works "
             "for adopted sessions too.",
        examples=[("agents whoami", "who am I, per the registry")],
        see_also=["whois", "more"],
    ),
    "more": dict(
        best="The full record for one agent — everything beyond the ls line: desc, "
             "recap + age, task, home, cwd, session, meta, recent recap history.",
        examples=[("agents more dewey", "full record"),
                  ("agents more dewey --json", "machine-readable")],
        see_also=["ls", "session", "checkin"],
    ),
    "session": dict(
        best="Tail/head an agent's transcript (works up OR down — it reads the "
             "file, unlike `attach`). Default render is conversation turns; -n "
             "counts turns, not raw metadata lines. --raw for exact JSONL.",
        examples=[("agents session dewey", "last turns, human-rendered"),
                  ("agents session dewey -f", "follow live"),
                  ("agents session dewey --head -n 5", "first few turns")],
        see_also=["attach", "find", "more"],
    ),
    "checkin": dict(
        best="Record where you are — opt-in, agent-set, timestamped, journaled. "
             "Leads should cascade it to their reports. See `man recap`.",
        examples=[('agents checkin "2 of 3 commits done; tests green"', "self check-in"),
                  ('agents checkin --agent saul "solr reindex running"',
                   "set another agent's recap")],
        see_also=["recap", "more", "ls"],
    ),
    "meta": dict(
        best="Structured, queryable metadata. Canonical homes: project, issue, pr, "
             "and (leads) epics. See `man conventions`. Set scalars as key=value; "
             "lists (epics) via a {json} blob.",
        examples=[("agents meta set pr-14212 project=ol issue=14209 pr=14212",
                   "stamp an ada's identifiers"),
                  ("agents meta set dewey '{\"epics\":[14200,14300]}'",
                   "a lead's epics (a list)"),
                  ("agents meta get dewey", "read the JSON object")],
        see_also=["ls", "conventions"],
    ),
    "rm": dict(
        best="Archives by default (hidden from ls, restored by up/register); "
             "--purge hard-deletes the row (home dir kept). Also tombstones a bare "
             "session id so find/ls hide it.",
        examples=[("agents rm old-ada", "archive (reversible)"),
                  ("agents rm --purge old-ada", "hard delete the row")],
        see_also=["down", "ls"],
    ),
    "snapshot": dict(
        best="Force-write the JSON backup (agents-snapshot.json) now. Normally it "
             "refreshes lazily, at most hourly, on any command.",
        examples=[("agents snapshot", "back up the registry now")],
        see_also=["reindex", "schema"],
    ),
    "reindex": dict(
        best="Recovery: rebuild the sqlite registry from the JSON snapshot. The "
             "snapshot is at most ~an hour old, so use this when the db is lost or "
             "corrupt — it prints the snapshot's age so you can judge.",
        examples=[("agents reindex", "rebuild agents.db from the snapshot")],
        see_also=["snapshot", "schema"],
    ),
    "schema": dict(
        best="Show/apply the registry schema version. Migrations are additive at "
             "the baseline and numbered forward; `agents upgrade` runs this against "
             "the freshly installed code so migrations land as part of upgrade.",
        examples=[("agents schema", "current version (migrating if needed)")],
        see_also=["upgrade", "reindex"],
    ),
}

# Commands that MUST carry a manual entry (the test enforces this). Internal or
# trivially-self-explanatory commands are intentionally omitted.
CORE = sorted(MANUALS)
