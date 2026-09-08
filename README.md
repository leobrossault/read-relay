# read-relay

A Claude Code plugin that keeps bulk file reading out of your main context.

A lot of what a coding agent does is not thinking, it is opening files and
reading them. Those tokens sit in the context window for the rest of the
session, they are billed at your main model's rate, and almost none of them
carry reasoning. Spotify's [shunt plugin](https://github.com/spotify/portal-ai-plugins/tree/main/plugins/shunt)
routes that work to a cheap worker model and reports around 90% token savings
on bulk reads, but it needs their Portal/AiKA platform.

read-relay does the same thing with nothing but what Claude Code already ships:
a hook, a subagent, and a skill. The cheap worker is a subagent pinned to Haiku
instead of an external model, so there is no infrastructure to run.

## Install

```bash
claude plugin marketplace add leobrossault/read-relay
claude plugin install read-relay@lbrossault
```

Two different names on purpose. `leobrossault/read-relay` is the GitHub repo
being added. `@lbrossault` is the marketplace that repo publishes, which is
just a label inside `marketplace.json`, and one marketplace can list many
plugins.

Requires Python 3 on your PATH. No other dependencies.

## How it works

Three pieces, each of which is a documented Claude Code feature:

1. **`bulk-reader` subagent** (`model: haiku`, read-only tools). Runs in its own
   context window and returns an answer with `path:line` anchors, never the
   file's contents.
2. **`PreToolUse` hook** on `Read` and `Bash`. If a read would pull in more than
   `threshold_lines` lines, it is denied with a message telling the model to
   relay it instead. This is the part that makes the saving reliable rather
   than aspirational.
3. **`read-relay` skill**. Teaches the model to delegate _before_ it gets
   blocked, and how to write a prompt the subagent can actually act on.

Without the hook you have a suggestion. With it you have a rule.

## Configuration

Set these in `/plugin` after installing.

| Option            | Default                         | What it does                                                                                                                         |
| ----------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `threshold_lines` | `400`                           | Reads that would pull in more than this many lines are relayed. See "Picking a threshold" below.                                     |
| `max_bounces`     | `1`                             | How many times one file may be refused in a session before it is let through. `0` disables the guard.                                |
| `guard_bash`      | `true`                          | Also blocks plain `cat` / `less` / `more` / `bat` on large files, even inside a `&&` / `;` chain. Piped, redirected and already-bounded commands are always allowed. |
| `skip_patterns`   | `*.lock,*-lock.json,*.min.js,…` | Comma-separated globs that are never relayed, matched on the full path and the basename.                                             |

Every option is also readable as a plain environment variable, named
`READ_RELAY_` plus the option in upper case, for example
`READ_RELAY_THRESHOLD_LINES=800`. Use that if `/plugin` does not seem to reach
the hook: not every Claude Code build forwards `userConfig` to hook processes,
and the environment variable works regardless.

Escape hatch: `READ_RELAY_OFF=1 claude` disables the guard for one session
without uninstalling anything.

`max_bounces` deserves a word. The guard never blocks a file forever, by
design. Each refusal is counted, and once a file has been bounced
`max_bounces` times in a session it is simply allowed through, so a
deliberate retry always works. Raise it to push harder, never to make the
block absolute.

The `bulk-reader` subagent itself is never bounced. Claude Code tags hook
payloads fired inside a subagent with its `agent_type`, and the guard lets that
agent read whatever it was sent. On an older build without that field the
bounce cap still keeps the relay from deadlocking, at the cost of one wasted
turn per relayed file.

## What it deliberately leaves alone

Binaries, images and PDFs. Files that do not exist. Reads made by the
`bulk-reader` subagent itself. Ranged reads with a `limit` at or under the
threshold. Shell commands that pipe, redirect or substitute. The
`Edit` and `Write` tools, always. Any payload it cannot parse.

The guard fails open on every error. A plugin that breaks your session to save
tokens is not worth having.

## Trade-offs, honestly

This is a real constraint on the agent, so it has real costs.

**You pay a round trip.** A relayed read is a subagent launch, its own prompt
and its own reasoning. On a file just over the threshold that is often a net
loss. The threshold is the whole game: too low and you spend more than you save,
too high and the guard never fires. See below.

**Total tokens do not drop as much as context does.** The file still gets read,
just by a cheaper model in a context you throw away. The saving is on the
per-token price and on not carrying those tokens through every subsequent turn.
It is not free.

**Summaries lose things.** The subagent starts with no knowledge of your
conversation, so it decides what matters from its prompt alone. A vague prompt
gets a vague summary, and the main model may not realise something was dropped.
This is the failure mode to watch for: not an error, a quietly incomplete
answer.

**Editing wants the real bytes.** Never edit from a summary. The skill says so
and the hook leaves `Edit` and `Write` untouched, but if the model relays a read
and then edits from what came back, you get plausible-looking corruption. If you
are about to change a file, read it.

**It will occasionally block something you wanted.** Long generated files,
fixtures, a config you know by heart. Hence the bounded bounce count, the skip
patterns and the kill switch.

**The bash guard is a speed bump, not a wall.** It catches the obvious
`cat big-file.ts`, and the equally common `wc -l big-file.ts && cat big-file.ts`,
because a chained command is split on `&&`, `||`, `;` and newlines and every
part is judged on its own. It deliberately ignores anything piped, redirected,
substituted or already bounded. A determined agent can still dump a file some
other way. The point is to stop the cheap accident, not to win an arms race.

**It is a floor, not a ceiling.** Claude Code already ships an `Explore`
subagent for read-only search. If that covers your use, you may not need this
plugin at all. read-relay is for when you want the behaviour enforced and the
worker pinned to a cheap model.

## Picking a threshold

The default is 400 lines. Spotify's shunt uses 350, on a Java monorepo. Neither
number means anything on its own, because what matters is where your files
actually sit. Look before you tune:

```bash
git ls-files -z '*.ts' '*.tsx' '*.js' '*.vue' '*.py' '*.go' \
  | xargs -0 -r wc -l | awk '
      $2 != "total" { n++; if ($1 > 400) big++ }
      END { if (n) printf "%d files, %d over 400 lines (%.0f%%)\n", n, big, 100*big/n
            else print "no matching files" }'
```

The `-z` / `-0` pair matters: without it, paths containing a space are silently
dropped from the count, which biases the result against exactly the kind of
large file you are looking for.

If under 5% of your files cross the threshold, the guard almost never fires and
you may as well use the subagent by hand. If more than a third do, you are
relaying routine work and paying a round trip for it, so raise the number until
only the genuinely large files are caught.

## Measuring whether it helps

Run `/context` before and after a task that touches a large codebase, with and
without the plugin. Compare the file-content share of the main window, and
compare total cost, not just context. If the two numbers move in opposite
directions, your threshold is too low.

## Prior art

The idea and the 350-line default come from Spotify's shunt plugin and
[the article behind it](https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90).
This is the same pattern rebuilt on stock Claude Code so it works without their
platform.

## License

MIT
