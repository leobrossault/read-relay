#!/usr/bin/env python3
"""PreToolUse guard for read-relay.

Reads a Claude Code PreToolUse payload on stdin. If the tool call is about to
pull a large file into the main context, it denies the call and tells the model
to relay the read to the `bulk-reader` subagent instead.

Design rules:
  - Fail open, but never silently. Any unexpected condition allows the tool
    call and says why on stderr. A guard that breaks a session to save tokens
    is not worth having, and one that disables itself without a word is worse.
  - Bounce a bounded number of times, never forever. A given file is denied at
    most `max_bounces` times per session, so a deliberate retry always gets
    through and no path can deadlock, including the bulk-reader subagent
    hitting this same hook on its way to read the file.
  - Stay out of the way of cheap reads. Targeted ranges, small files, binaries
    and piped shell commands are never touched.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time

DEFAULT_THRESHOLD = 400
DEFAULT_SKIP = "*.lock,*-lock.json,*-lock.yaml,*.min.js,*.min.css,*.map,*.snap,*.svg,*.csv,*.tsv"
DEFAULT_MAX_BOUNCES = 1
BOUNCE_CEILING = 10
STATE_TTL_SECONDS = 6 * 60 * 60
CHUNK = 262144

OPAQUE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".xz", ".7z", ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".so", ".dylib", ".dll", ".exe", ".wasm",
}

DUMPERS = ("cat", "less", "more", "bat")
RANGED = ("head", "tail")


def warn(message: str) -> None:
    """Say why the guard stood down. Never fatal."""
    try:
        sys.stderr.write("read-relay: " + message + "\n")
    except Exception:
        pass


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


# --------------------------------------------------------------------------
# Options
#
# Two sources, in order. READ_RELAY_<KEY> is a plain environment variable and
# always works. CLAUDE_PLUGIN_OPTION_<KEY> is what a Claude Code build exports
# from `userConfig` in plugin.json; not every build does, so it is the fallback
# rather than the primary.
# --------------------------------------------------------------------------

def opt(name: str, default: str) -> str:
    for var in ("READ_RELAY_" + name.upper(), "CLAUDE_PLUGIN_OPTION_" + name.upper()):
        value = os.environ.get(var)
        if value is not None and value.strip() != "":
            return value
    return default


def opt_bool(name: str, default: bool) -> bool:
    return opt(name, "true" if default else "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def opt_int(name: str, default: int, low: int, high: int) -> int:
    raw = opt(name, str(default))
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        warn("ignoring unreadable %s=%r, using %d" % (name, raw, default))
        return default
    if value < low or value > high:
        warn("%s=%d out of range [%d, %d], using %d" % (name, value, low, high, default))
        return default
    return value


def is_skipped(path: str, patterns: str) -> bool:
    base = os.path.basename(path)
    for raw in patterns.split(","):
        pattern = raw.strip()
        if pattern and (fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern)):
            return True
    return False


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def count_lines(path: str, cap: int) -> tuple[int, bool] | None:
    """Return (lines, exact). Stops early once `cap` is exceeded.

    `exact` is False when counting stopped early, so the caller can say "more
    than N" instead of quoting a number that is really just where it gave up.
    Returns None when the file is not readable as text, which means "allow".
    """
    total = 0
    last = b"\n"
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                if b"\0" in chunk:
                    return None
                total += chunk.count(b"\n")
                last = chunk[-1:]
                if total > cap:
                    return total, False
    except OSError as exc:
        warn("cannot read %s (%s), allowing" % (path, exc.__class__.__name__))
        return None
    if last != b"\n":
        total += 1  # a final line with no trailing newline still counts
    return total, True


def resolve(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd, path))


# --------------------------------------------------------------------------
# Bounce state
# --------------------------------------------------------------------------

def state_dir() -> str | None:
    """A per-user directory under the system temp dir, or None if unusable."""
    try:
        uid = os.getuid()  # type: ignore[attr-defined]
    except AttributeError:
        uid = os.environ.get("USERNAME", "user")
    path = os.path.join(tempfile.gettempdir(), "read-relay-%s" % uid)
    try:
        if os.path.islink(path):
            warn("state dir %s is a symlink, refusing to use it" % path)
            return None
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            return None
        # On a shared temp dir another local user could have created this
        # path first. Refuse anything not owned by us or open to others,
        # rather than reading state someone else can write.
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            warn("state dir %s is owned by uid %d, refusing to use it" % (path, info.st_uid))
            return None
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
        return path
    except OSError as exc:
        warn("cannot create state dir %s (%s)" % (path, exc.__class__.__name__))
        return None


def bounce_count(session_id: str, key: str) -> int | None:
    """Record a bounce for `key` and return how many happened before it.

    Returns None when state cannot be kept at all, which the caller treats as
    "do not block", so a broken temp dir can never turn into a hard wall.
    """
    directory = state_dir()
    if directory is None:
        return None

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:80]
    state_path = os.path.join(directory, safe + ".json")
    now = time.time()

    seen: dict[str, list] = {}
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                # Keep only well-formed, unexpired entries. One bad value can
                # no longer poison the whole file.
                if isinstance(v, list) and len(v) == 2:
                    count, stamp = v
                    if isinstance(count, int) and isinstance(stamp, (int, float)):
                        if now - stamp < STATE_TTL_SECONDS:
                            seen[k] = [count, stamp]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        warn("resetting unreadable state file (%s)" % exc.__class__.__name__)

    previous = seen.get(key, [0, now])[0]
    seen[key] = [previous + 1, now]

    tmp = None
    try:
        # mkstemp opens with O_EXCL and 0600, so a planted symlink or a
        # guessable name can never redirect this write.
        fd, tmp = tempfile.mkstemp(prefix=safe + ".", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)
        os.replace(tmp, state_path)
    except OSError as exc:
        warn("cannot persist state (%s)" % exc.__class__.__name__)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None

    return previous


# --------------------------------------------------------------------------
# Bash parsing
# --------------------------------------------------------------------------

# Operators that merely sequence commands. Each side is judged on its own, so
# `wc -l f && cat f` no longer hides the dump behind the count.
CHAIN_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")
# Within one segment, any of these means the output is consumed, bounded or
# too ambiguous to judge, so the segment is left alone.
SEGMENT_META = ("|", ">", "<", "$(", "`", "&")


def bash_target(command: str, threshold: int) -> str | None:
    """Return the file a plain reader command would dump, or None.

    The command is split on `&&`, `||`, `;` and newlines and every segment is
    judged separately. A segment that pipes, redirects or substitutes is left
    alone: it is either cheap or too ambiguous to judge safely. The first
    segment that is a plain dump of one large file wins.
    """
    if not command:
        return None
    for segment in CHAIN_SPLIT.split(command):
        target = _segment_target(segment, threshold)
        if target is not None:
            return target
    return None


def _segment_target(command: str, threshold: int) -> str | None:
    if not command or any(token in command for token in SEGMENT_META):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None

    program = os.path.basename(parts[0])
    if program not in DUMPERS and program not in RANGED:
        return None

    args = parts[1:]
    files: list[str] = []
    ranged_n: int | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            files.extend(args[index + 1:])
            break
        if token.startswith("-") and token != "-":
            if program in RANGED:
                digits = ""
                if token.startswith("-n") and len(token) > 2:
                    digits = token[2:]
                elif token in ("-n", "--lines") and index + 1 < len(args):
                    digits = args[index + 1]
                    index += 1
                elif re.fullmatch(r"-\d+", token):
                    digits = token[1:]
                if digits:
                    try:
                        ranged_n = abs(int(digits))
                    except ValueError:
                        ranged_n = None
            index += 1
            continue
        files.append(token)
        index += 1

    # One unambiguous file, no globbing left to guess at.
    if len(files) != 1 or any(ch in files[0] for ch in "*?["):
        return None
    if program in RANGED:
        # No -n means 10 lines, and a small -n is a deliberate peek. Only a
        # `head -n 100000` is really a dump wearing a disguise.
        if ranged_n is None or ranged_n <= threshold:
            return None
    return files[0]


# --------------------------------------------------------------------------

RELAY_HINT = (
    "Do not work around this by reading the file in slices just to reassemble "
    "it, and do not use a shell command to dump it. Either relay it, or read a "
    "specific range you have a reason to need."
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        allow()
    if not isinstance(payload, dict):
        allow()

    if os.environ.get("READ_RELAY_OFF", "").strip().lower() in ("1", "true", "yes", "on"):
        allow()

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        allow()
    cwd = payload.get("cwd") or os.getcwd()

    threshold = opt_int("threshold_lines", DEFAULT_THRESHOLD, 50, 1000000)
    max_bounces = opt_int("max_bounces", DEFAULT_MAX_BOUNCES, 0, BOUNCE_CEILING)
    if max_bounces == 0:
        allow()

    offset = tool_input.get("offset")
    offset = int(offset) if isinstance(offset, (int, float)) and offset > 0 else 0

    if tool == "Read":
        raw_path = tool_input.get("file_path")
        limit = tool_input.get("limit")
        if isinstance(limit, (int, float)) and 0 < limit <= threshold:
            allow()
        source = "Read"
    elif tool == "Bash" and opt_bool("guard_bash", True):
        raw_path = bash_target(tool_input.get("command") or "", threshold)
        offset = 0
        source = "Bash"
    else:
        allow()

    if not isinstance(raw_path, str) or not raw_path:
        allow()

    path = resolve(raw_path, cwd)
    if os.path.splitext(path)[1].lower() in OPAQUE_SUFFIXES:
        allow()
    if is_skipped(path, opt("skip_patterns", DEFAULT_SKIP)):
        allow()
    if not os.path.isfile(path):
        allow()

    counted = count_lines(path, threshold + offset)
    if counted is None:
        allow()
    lines, exact = counted

    # An unbounded read starting late in the file only pulls in the remainder.
    incoming = max(lines - offset, 0)
    if incoming <= threshold:
        allow()

    seen_before = bounce_count(payload.get("session_id") or "", path)
    if seen_before is None or seen_before >= max_bounces:
        allow()

    size = ("roughly %d" % incoming) if exact else ("well over %d" % threshold)
    verb = "Reading" if source == "Read" else "Dumping"
    deny(
        f"{verb} {raw_path} would pull {size} lines into this context, which is "
        f"over the read-relay threshold of {threshold}.\n\n"
        f"Delegate it instead: launch the `bulk-reader` subagent and tell it the "
        f"file path AND the specific question it has to answer. It runs on a "
        f"cheaper model in its own context and will hand back only the answer, "
        f"with path:line anchors.\n\n{RELAY_HINT}\n\n"
        f"If you genuinely need the literal contents, repeating this exact read "
        f"will be allowed."
    )


if __name__ == "__main__":
    main()
