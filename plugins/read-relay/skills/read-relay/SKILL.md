---
name: read-relay
description: How to look inside a large file without paying for it in the main context. Use before reading any file you expect to be long (several hundred lines or more), when you only need to know what a file contains or where something lives in it, and whenever a Read was denied with a read-relay message.
---

# Relaying large reads

Opening a large file costs context that never comes back, and most of the time
the answer you needed was a few lines of it. Relay the read instead.

## When to relay

Relay when you want to know *about* a file: what it contains, where something
is defined, whether it handles a case, how it is structured. Send the
`bulk-reader` subagent the path and the exact question.

Read it yourself when you need the literal bytes, which is most often because
you are about to edit them. Editing from a summary is how files get corrupted,
so read what you are about to change.

## How to relay well

The subagent has none of your conversation. A prompt like "read src/api.ts"
gets you a generic summary you will have to follow up on, which costs more than
reading the file would have. Say what the answer is for.

Good: "In `src/server/auth.ts`, find every place a session token is created or
invalidated. Give me the function names, their line ranges, and the exact
expiry values."

Bad: "Summarize src/server/auth.ts."

## Working with what comes back

The summary carries `path:line` anchors. When you need the real text of one
part, read that range with `offset` and `limit` rather than opening the whole
file. A bounded read under the threshold is never blocked.

## When a read is denied

The denial names the file and the threshold. Relay it. Do not reassemble the
file through a series of ranged reads or a shell command, which spends the
context you were just told to protect. If you truly need the whole file
verbatim, repeat the same Read once and it will go through.
