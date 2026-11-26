#!/usr/bin/env python3
"""Give a Proxmox installer ISO a serial console, without repacking it.

WHY NOT REPACK

The obvious approach -- extract, edit boot/grub/grub.cfg, rebuild with xorriso --
does not work on this image. Its boot layout combines a GRUB2 MBR, an
isohybrid-style GPT and an appended EFI partition, and every rebuild attempt
(including one driven by the exact options xorriso itself reports via
`-report_el_torito as_mkisofs`) fails with:

    libisofs: FAILURE : Overlapping MBR partition entries requested

So instead the bytes are patched in place. ISO9660 stores file contents
verbatim and contiguously, and grub.cfg is a plain text file inside the image,
so the kernel command line can be rewritten directly. Nothing that describes the
boot layout is touched, which is exactly the property that makes this safe:
El Torito records, the MBR, the GPT and the EFI partition come through
bit-identical.

The constraint this buys is that the replacement must be EXACTLY as long as what
it replaces -- any length change would shift every following byte and corrupt
the filesystem. That is arranged by dropping `quiet` and `splash=silent`, which
buys enough room for the console arguments and is desirable anyway: a silent
kernel is the opposite of what a serial console is for.

WHAT IT FIXES

A stock installer boots with no `console=`, so once GRUB hands off to Linux the
installation is invisible over Serial-over-LAN. GRUB's own menu IS on serial
already (the ISO sets `serial --unit=0 --speed=115200`), which makes the silence
afterwards look like a hang rather than a missing setting.

An auto-install that fails -- a bad answer file, an unsatisfiable disk filter --
then reports nothing at all: the machine sits at an error screen reachable only
through the BMC's graphical console.
"""

import os
import re
import shutil
import sys

# The automated entry specifically. The graphical and TUI entries are for a human
# at a screen and are left alone; this is about the unattended path.
#
# The leading indentation is part of the match on purpose. It is whitespace GRUB
# ignores, and including it roughly doubles the room available for the
# replacement -- which is what makes it possible to keep BOTH consoles rather
# than only the serial one. Dropping console=tty0 would blank the BMC's
# graphical console, and that is the view that makes a stopped installer
# diagnosable in the first place.
PATTERN = re.compile(
    rb"(?P<indent>[ \t]*)linux(?P<pad>[ \t]+)(?P<path>/boot/linux26)"
    rb"(?P<args>[^\n]*?)proxmox-start-auto-installer"
)

DROP = (b"quiet", b"splash=silent")


def build_replacement(match: re.Match, console: bytes) -> bytes:
    """Rewrite one kernel line, preserving its exact byte length."""
    original = match.group(0)

    args = [a for a in match.group("args").split() if a not in DROP]
    args.append(console)
    body = b"linux " + match.group("path") + b" " + b" ".join(args) + b" proxmox-start-auto-installer"

    shortfall = len(original) - len(body)
    if shortfall < 0:
        raise SystemExit(
            f"cannot patch within the original {len(original)} bytes: the new command "
            f"line needs {-shortfall} more. Shorten the console arguments, or drop "
            f"another boot argument in DROP."
        )

    # The reclaimed space goes back as padding after `linux`, where GRUB treats
    # any run of whitespace as a single separator -- so the line stays valid at
    # whatever length is needed to hit the original byte count exactly.
    return b"linux" + b" " * (shortfall + 1) + body[len(b"linux "):]


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <source.iso> <output.iso> <console-args>", file=sys.stderr)
        return 2

    src, dst, console = sys.argv[1], sys.argv[2], sys.argv[3].encode()

    # Validated rather than trusted, because a malformed value here is silent.
    # `console=,115200` -- what an empty device variable renders to -- is accepted
    # by the kernel, ignored, and produces an ISO that looks patched and has no
    # serial output. That exact string got this far once.
    tokens = console.split()
    valid = all(re.fullmatch(rb"console=tty[A-Za-z]*\d+(,\d+[noe]?[78]?[01]?)?", t) for t in tokens)
    serial = any(re.fullmatch(rb"console=tty[A-Za-z]+\d+,\d+.*", t) for t in tokens)
    if not tokens or not valid or not serial:
        raise SystemExit(
            f"refusing to patch with {console.decode()!r}: expected one or more "
            f"console= arguments including a serial one, e.g. "
            f"'console=tty0 console=ttyS0,115200'. An empty device renders as "
            f"'console=,115200', which the kernel silently ignores."
        )

    data = open(src, "rb").read()
    matches = list(PATTERN.finditer(data))
    if not matches:
        raise SystemExit(
            "no automated-installer kernel line found in the ISO. The grub.cfg layout "
            "has changed for this Proxmox release; update PATTERN before relying on this."
        )

    # Refuse to double-patch -- but judged on the automated entries alone, not on
    # the file as a whole.
    #
    # A stock Proxmox ISO already ships an "Install Proxmox VE (Terminal UI,
    # Serial Console)" menuentry carrying this exact console= string. Searching
    # the whole image therefore always matches, concludes the work is already
    # done, and silently produces nothing -- which is what happened the first
    # time this ran. The question is only ever whether the UNATTENDED entries
    # have it.
    if all(console in m.group(0) for m in matches):
        print(f"{src}: automated entries already carry {console.decode()}; nothing to do")
        return 0

    shutil.copy2(src, dst)
    with open(dst, "r+b") as fh:
        for m in matches:
            replacement = build_replacement(m, console)
            assert len(replacement) == len(m.group(0)), "internal length error"
            fh.seek(m.start())
            fh.write(replacement)
            print(f"patched at offset {m.start()}: {replacement.decode().strip()}")

    if os.path.getsize(dst) != len(data):
        raise SystemExit("output size changed -- the image would be corrupt; refusing")

    print(f"{len(matches)} entrie(s) patched; size unchanged at {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
