#!/usr/bin/env python3
"""Black-box tests for bin/guard-read.py.

Run with `python3 plugins/read-relay/tests/test_guard.py`. Each case feeds a
PreToolUse payload to the guard and checks whether it denies or allows.
"""
import json, os, subprocess, sys, tempfile, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
G = os.path.join(HERE, "..", "bin", "guard-read.py")
TMP = tempfile.mkdtemp(prefix="read-relay-test-")
BIG = os.path.join(TMP, "big.txt"); SMALL = os.path.join(TMP, "small.txt")
with open(BIG, "w") as f: f.write("".join(f"line {i}\n" for i in range(1, 1001)))
with open(SMALL, "w") as f: f.write("".join(f"line {i}\n" for i in range(1, 51)))
def run(tool, inp, env=None):
    e = dict(os.environ); e.pop("READ_RELAY_OFF", None); e.update(env or {})
    p = subprocess.run([sys.executable, G], input=json.dumps({"session_id": "t-"+uuid.uuid4().hex[:8], "tool_name": tool, "tool_input": inp}), capture_output=True, text=True, env=e)
    return "DENY" if '"deny"' in p.stdout else "ALLOW"
cases = [
 ("Read big",                  "Read", {"file_path": BIG}, "DENY"),
 ("Read small",                "Read", {"file_path": SMALL}, "ALLOW"),
 ("Read big limit 100",        "Read", {"file_path": BIG, "limit": 100}, "ALLOW"),
 ("Read big offset 900",       "Read", {"file_path": BIG, "offset": 900}, "ALLOW"),
 ("cat big",                   "Bash", {"command": f"cat {BIG}"}, "DENY"),
 ("cat small",                 "Bash", {"command": f"cat {SMALL}"}, "ALLOW"),
 ("wc && cat big  (le trou)",  "Bash", {"command": f"wc -l {BIG} && cat {BIG}"}, "DENY"),
 ("cd ; cat big",              "Bash", {"command": f"cd /tmp; cat {BIG}"}, "DENY"),
 ("ls || cat big",             "Bash", {"command": f"ls || cat {BIG}"}, "DENY"),
 ("newline then cat big",      "Bash", {"command": f"echo hi\ncat {BIG}"}, "DENY"),
 ("cat big | head",            "Bash", {"command": f"cat {BIG} | head"}, "ALLOW"),
 ("wc && cat big | head",      "Bash", {"command": f"wc -l {BIG} && cat {BIG} | head -20"}, "ALLOW"),
 ("cat big > out",             "Bash", {"command": f"cat {BIG} > /dev/null"}, "ALLOW"),
 ("echo $(cat big)",           "Bash", {"command": f"echo $(cat {BIG})"}, "ALLOW"),
 ("cat big &  (background)",   "Bash", {"command": f"cat {BIG} &"}, "ALLOW"),
 ("head -n 5 big",             "Bash", {"command": f"head -n 5 {BIG}"}, "ALLOW"),
 ("head -n 900 big",           "Bash", {"command": f"head -n 900 {BIG}"}, "DENY"),
 ("wc && head -n 900 big",     "Bash", {"command": f"wc -l {BIG} && head -n 900 {BIG}"}, "DENY"),
 ("sed -n 777p big",           "Bash", {"command": f"sed -n 777p {BIG}"}, "ALLOW"),
 ("cat two files",             "Bash", {"command": f"cat {BIG} {SMALL}"}, "ALLOW"),
 ("cat glob",                  "Bash", {"command": f"cat {os.path.dirname(BIG)}/*.txt"}, "ALLOW"),
 ("READ_RELAY_OFF",            "Read", {"file_path": BIG}, "ALLOW", {"READ_RELAY_OFF": "1"}),
 ("guard_bash=false",          "Bash", {"command": f"wc -l {BIG} && cat {BIG}"}, "ALLOW", {"READ_RELAY_GUARD_BASH": "false"}),
 ("threshold 2000",            "Read", {"file_path": BIG}, "ALLOW", {"READ_RELAY_THRESHOLD_LINES": "2000"}),
]
fail = 0
for c in cases:
    name, tool, inp, want = c[:4]; env = c[4] if len(c) > 4 else None
    got = run(tool, inp, env); ok = got == want; fail += not ok
    print(("ok  " if ok else "FAIL"), f"{name:28} want={want} got={got}")
# bounce: same session, second try passes
sid = "bounce-"+uuid.uuid4().hex[:6]
r = [ '"deny"' in subprocess.run([sys.executable, G], input=json.dumps({"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": BIG}}), capture_output=True, text=True).stdout for _ in range(2)]
ok = r == [True, False]; fail += not ok
print(("ok  " if ok else "FAIL"), f"{'bounce: deny then allow':28} got={r}")

print("\n%d failure(s)" % fail); sys.exit(1 if fail else 0)
