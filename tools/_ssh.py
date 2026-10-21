#!/usr/bin/env python3
"""Password-SSH helper (debug/read-only use only).

Usage:  python3 tools/_ssh.py "<remote-command>"
Connects to PIETOSHER via ssh, runs the command, prints its stdout.
Password-driven via pexpect. Throwaway helper — do not commit.
"""
import sys, secrets, pexpect

HOST, USER, PASS = "192.168.0.99", "pietro", "2004"


def run(cmd, timeout=60):
    marker = "DNR_" + secrets.token_hex(8)
    ssh = pexpect.spawn("ssh %s@%s" % (USER, HOST), timeout=timeout)
    ssh.expect([r"(?i)password:"], timeout=timeout)
    ssh.sendline(PASS)
    ssh.expect([r"#", r"\$"], timeout=timeout)
    # send the command, then a marker line. The first marker match is the
    # terminal's echo of `echo MARKER`; everything before it is: greeting,
    # the echoed command, and the REAL output. Drop echoed/marker lines.
    ssh.sendline(cmd)
    ssh.sendline("echo %s" % marker)
    ssh.expect(marker, timeout=timeout)
    out = ssh.before
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    ssh.close()
    keep = []
    for l in out.splitlines():
        s = l.strip()
        if not s:
            continue
        if marker in s:            # echo of the marker / marker output
            continue
        if s == cmd or s.endswith("; " + cmd) or s.startswith("$ " + cmd):
            continue               # echoed prompt of the command itself
        keep.append(l)
    return "\n".join(keep).strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: _ssh.py <command>"); sys.exit(2)
    sys.stdout.write(run(" ".join(sys.argv[1:])) + "\n")
