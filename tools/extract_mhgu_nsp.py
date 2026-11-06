#!/usr/bin/env python3
"""Extract MHGU base + 1.4.0 update NSPs into exefs/ + romfs/ using build/hactool.

Double-click Extract MHGU NSP.bat (Windows) or run Extract MHGU NSP.sh (Linux), or:
  python tools/extract_mhgu_nsp.py
  python tools/extract_mhgu_nsp.py --cli --base game.nsp --update update.nsp --keys prod.keys --out ../mhgu/game

Never modifies the NSP files. Needs ~20 GB free while working.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Callable

TITLE_ID = 0x0100770008DD8000
# Update/patch NSPs use the app title ID with bit 0x800 set (Switchbrew).
PATCH_TITLE_ID = TITLE_ID | 0x800  # 0100770008DD8800
CNMT_VERSION = 262144  # 1.4.0
NEED_FREE = 20 * 1024**3


def title_id_ok(tid: int) -> bool:
    return tid in (TITLE_ID, PATCH_TITLE_ID, 0)

ProgressFn = Callable[[str], None]


class ExtractError(Exception):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def find_hactool() -> Path:
    root = repo_root()
    for p in (root / "build" / "hactool.exe", root / "build" / "hactool",
              root / "hactool.exe", root / "hactool"):
        if p.is_file():
            return p
    raise ExtractError(
        "hactool not found.\nIt ships next to mhgurecomp in the release; from source, build it first"
        + (" in MSYS2 UCRT64" if os.name == "nt" else "") + ":\n  make -j4 hactool"
    )


def build_hint(out: Path) -> str:
    """What a from-source checkout runs next with the extracted dump."""
    where = " (MSYS2 UCRT64)" if os.name == "nt" else ""
    return f"From source{where}, next:\n  make gen GAME={out.as_posix()}\n  make -j4 GAME={out.as_posix()}"


def free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def run(hactool: Path, keys: Path, home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    cmd = [str(hactool), "-k", str(keys), "--disablekeywarns", *args]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        detail = ((r.stdout or "") + (r.stderr or "")).strip()
        raise ExtractError(f"hactool failed ({r.returncode}).\n{detail[-1500:]}")
    return r


def setup_keys(work: Path, prod_keys: Path, title_keys: Path | None) -> tuple[Path, Path]:
    home = work / "home"
    switch = home / ".switch"
    switch.mkdir(parents=True)
    dest = switch / "prod.keys"
    shutil.copy2(prod_keys, dest)
    tk_path = switch / "title.keys"
    if title_keys and title_keys.is_file():
        shutil.copy2(title_keys, tk_path)
    elif not tk_path.exists():
        tk_path.write_text("", encoding="utf-8")
    return home, dest


def parse_ticket(tik: Path) -> tuple[str, str] | None:
    """Return (rights_id_hex, encrypted_titlekey_hex) from a common ticket. hactool 1.4 has no -t tik."""
    data = tik.read_bytes()
    if len(data) < 0x2B0:
        return None
    # Common ticket: titlekey @ 0x180 (16), rights_id @ 0x2A0 (16)
    enc = data[0x180:0x190].hex()
    rights = data[0x2A0:0x2B0].hex()
    if rights.strip("0") == "" or enc.strip("0") == "":
        return None
    return rights.lower(), enc.lower()


def import_tickets(hactool: Path, keys: Path, home: Path, nsp_dir: Path, sample_nca: Path | None) -> str | None:
    """Append ticket titlekeys to title.keys and return the first encrypted titlekey.

    hactool's title.keys values are the *encrypted* titlekey (it runs titlekek itself).
    Writing the decrypted key makes basenca merges decrypt the base NCA with the wrong key.
    """
    tk_path = home / ".switch" / "title.keys"
    nca = sample_nca
    if nca is None:
        ncas = sorted(nsp_dir.glob("*.nca"), key=lambda p: p.stat().st_size, reverse=True)
        nca = ncas[0] if ncas else None
    if nca is None:
        return None
    first_enc: str | None = None
    with tk_path.open("a", encoding="utf-8") as tk:
        for tik in nsp_dir.glob("*.tik"):
            parsed = parse_ticket(tik)
            if not parsed:
                continue
            rights, enc = parsed
            if first_enc is None:
                first_enc = enc
            # Always record the encrypted titlekey (needed for --basenca merges). Probe is optional.
            tk.write(f"{rights}={enc}\n")
            r = run(hactool, keys, home, f"--titlekey={enc}", "-t", "nca", str(nca), check=False)
            text = (r.stdout or "") + (r.stderr or "")
            ok = any("titlekey (decrypted)" in line.lower() for line in text.splitlines())
            if ok:
                print(f"  imported titlekey for {rights[:16]}…")
            else:
                print(f"  imported titlekey for {rights[:16]}… (could not verify against sample NCA)")
    return first_enc


def unpack_nsp(hactool: Path, keys: Path, home: Path, nsp: Path, outdir: Path) -> str | None:
    with nsp.open("rb") as f:
        if f.read(4) != b"PFS0":
            raise ExtractError(f"Not a valid NSP (missing PFS0 header):\n{nsp}")
    outdir.mkdir(parents=True, exist_ok=True)
    # hactool accepts --outdir=path (equals form is reliable with spaces)
    run(hactool, keys, home, "-t", "pfs0", f"--outdir={outdir}", str(nsp))
    # Prefer Program NCA as the sample for titlekey probe (largest .nca that isn't meta)
    sample = None
    for nca in sorted(outdir.glob("*.nca"), key=lambda p: p.stat().st_size, reverse=True):
        if "cnmt" not in nca.name.lower():
            sample = nca
            break
    return import_tickets(hactool, keys, home, outdir, sample)


def nca_info(hactool: Path, keys: Path, home: Path, nca: Path) -> tuple[str, int]:
    r = run(hactool, keys, home, "-t", "nca", str(nca), check=False)
    text = (r.stdout or "") + (r.stderr or "")
    ctype, tid = "", 0
    for line in text.splitlines():
        if line.lower().startswith("content type"):
            ctype = line.split(":", 1)[1].strip()
        if line.lower().startswith("title id"):
            try:
                tid = int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                pass
    return ctype, tid


def find_program_and_meta(hactool: Path, keys: Path, home: Path, folder: Path) -> tuple[Path, Path | None]:
    program = meta = None
    program_size = -1
    for nca in folder.glob("*.nca"):
        ctype, _ = nca_info(hactool, keys, home, nca)
        size = nca.stat().st_size
        if "Meta" in ctype or "cnmt" in nca.name.lower():
            meta = nca
        if "Program" in ctype and size > program_size:
            program, program_size = nca, size
    if not program:
        # Fallback: largest non-cnmt NCA (titlekey may still be missing)
        candidates = [p for p in folder.glob("*.nca") if "cnmt" not in p.name.lower()]
        if candidates:
            program = max(candidates, key=lambda p: p.stat().st_size)
        else:
            raise ExtractError(f"No Program NCA found in:\n{folder}\nIs this the right NSP?")
    return program, meta


def read_cnmt(hactool: Path, keys: Path, home: Path, work: Path, meta: Path | None) -> tuple[int, int]:
    if not meta:
        return 0, 0
    sec = work / "cnmt_sec"
    if sec.exists():
        shutil.rmtree(sec)
    sec.mkdir()
    run(hactool, keys, home, "-t", "nca", f"--section0dir={sec}", str(meta), check=False)
    files = [p for p in sec.rglob("*") if p.is_file()]
    if not files:
        return 0, 0
    data = files[0].read_bytes()[:16]
    if len(data) < 16:
        return 0, 0
    # CNMT: title_id u64 @0, title_version u32 @8
    tid = struct.unpack_from("<Q", data, 0)[0]
    ver = struct.unpack_from("<I", data, 8)[0]
    return tid, ver


def extract_mhgu(
    base: Path,
    update: Path,
    keys: Path,
    out: Path,
    title_keys: Path | None = None,
    progress: ProgressFn | None = None,
) -> None:
    def say(msg: str) -> None:
        print(msg)
        if progress:
            progress(msg)

    for label, p in (("base NSP", base), ("update NSP", update), ("prod.keys", keys)):
        if not p.is_file():
            raise ExtractError(f"Missing {label}:\n{p}")
    if base.resolve() == update.resolve():
        raise ExtractError(
            "Base game and update are the same file.\n"
            "Pick the big base NSP (~12 GB, title …8000) and the small update NSP (~200 MB, title …8800)."
        )

    hactool = find_hactool()
    say(f"Using {hactool.name}")
    out.mkdir(parents=True, exist_ok=True)

    free = free_bytes(out)
    if free is not None and free < NEED_FREE:
        raise ExtractError(
            f"Not enough free space.\nNeed about 20 GB free (have {free / 1024**3:.1f} GB)."
        )

    # Prefer the output drive over /tmp: base NSP unpack alone is ~12 GB, and tmpfs is often smaller.
    work_root = out.resolve().parent
    try:
        work_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        work_root = Path(tempfile.gettempdir())
    work = Path(tempfile.mkdtemp(prefix="mhgu_nsp_", dir=str(work_root)))
    try:
        home, keyfile = setup_keys(work, keys, title_keys)
        base_dir = work / "base_nsp"
        upd_dir = work / "upd_nsp"

        say("1/5 Unpacking base game NSP…")
        base_enc = unpack_nsp(hactool, keyfile, home, base, base_dir)
        say("2/5 Unpacking 1.4.0 update NSP…")
        upd_enc = unpack_nsp(hactool, keyfile, home, update, upd_dir)

        say("3/5 Checking this is MHGU 1.4.0…")
        base_prog, _ = find_program_and_meta(hactool, keyfile, home, base_dir)
        upd_prog, upd_meta = find_program_and_meta(hactool, keyfile, home, upd_dir)
        tid, ver = read_cnmt(hactool, keyfile, home, work, upd_meta)
        # Update CNMT title ID is usually …8800 (patch); base is …8000. Both are MHGU.
        if tid and not title_id_ok(tid):
            raise ExtractError(
                f"Wrong game.\nTitle ID {tid:016X}\n"
                f"Expected 0100770008DD8000 (base) or 0100770008DD8800 (update)."
            )
        if ver and ver != CNMT_VERSION:
            raise ExtractError(
                f"Wrong update version (v{ver}).\nNeed update 1.4.0 (v{CNMT_VERSION})."
            )
        if not base_enc or not upd_enc:
            raise ExtractError(
                "Could not read title keys from the NSP tickets.\n"
                "Make sure each NSP includes a .tik and that prod.keys can decrypt them."
            )

        exefs = out / "exefs"
        romfs = out / "romfs"
        if exefs.exists():
            shutil.rmtree(exefs)
        if romfs.exists():
            shutil.rmtree(romfs)
        exefs.mkdir()
        romfs.mkdir()

        say("4/5 Extracting game files (this can take a while)…")
        # ExeFS and Patch-RomFS must be separate hactool passes. Combining
        # --exefsdir + --romfsdir + --basenca often finishes with an empty exefs.
        say("  ExeFS (update)…")
        run(
            hactool, keyfile, home,
            f"--titlekey={upd_enc}", "-t", "nca", f"--exefsdir={exefs}", str(upd_prog),
        )
        if not (exefs / "main").is_file():
            listing = ", ".join(p.name for p in exefs.iterdir()) or "(empty)"
            raise ExtractError(
                "Update ExeFS extract finished but main is missing.\n"
                f"exefs contains: {listing}\n"
                "Check that the small NSP is the 1.4.0 update and prod.keys is correct."
            )
        # RomFS merge: do NOT pass --titlekey= on the CLI. A single CLI titlekey is
        # applied to both the update NCA and --basenca, which corrupts BKTR output.
        # title.keys (encrypted keys per rights id) supplies each NCA its own key.
        say("  RomFS (base + update merge)…")
        basenca = f"--basenca={base_prog}"
        r = run(
            hactool, keyfile, home,
            "-t", "nca", basenca, f"--romfsdir={romfs}", str(upd_prog),
            check=False,
        )
        romfs_ok = any(romfs.iterdir()) if romfs.is_dir() else False
        mfx = romfs / "nativeNX" / "system" / "app_shader" / "AppShaderPackage.mfx"
        mfx_ok = mfx.is_file() and mfx.read_bytes()[:3] == b"MFX"
        if r.returncode != 0 or not romfs_ok or not mfx_ok:
            say("  Retrying RomFS via full base extract, then update patch…")
            if romfs.exists():
                shutil.rmtree(romfs)
            romfs.mkdir()
            run(
                hactool, keyfile, home,
                f"--titlekey={base_enc}", "-t", "nca", f"--romfsdir={romfs}", str(base_prog),
            )
            run(
                hactool, keyfile, home,
                "-t", "nca", basenca, f"--romfsdir={romfs}", str(upd_prog),
            )
            if not (mfx.is_file() and mfx.read_bytes()[:3] == b"MFX"):
                raise ExtractError(
                    "RomFS merge finished but AppShaderPackage.mfx is missing or still encrypted.\n"
                    "Check prod.keys and that both NSPs include tickets."
                )

        say("5/5 Verifying build ID…")
        main_nso = exefs / "main"
        if not main_nso.is_file():
            listing = ", ".join(p.name for p in exefs.iterdir()) or "(empty)"
            raise ExtractError(
                "Extraction finished but exefs/main is missing.\n"
                f"exefs contains: {listing}\n\n"
                "Usually title keys did not apply. Use:\n"
                "• Base NSP named …8000 (~12 GB)\n"
                "• Update NSP named …8800 (~200 MB)\n"
                "• Your prod.keys (and optional title.keys)"
            )
        hdr = main_nso.read_bytes()[:0x48]
        want = bytes.fromhex("fb08f1d20fd1204f")
        if hdr[:4] != b"NSO0" or hdr[0x40:0x48] != want:
            raise ExtractError(
                "exefs/main is not MHGU 1.4.0\n(build id should start with FB08F1D20FD1204F)."
            )

        install_note = install_into_build_data(out)
        say(f"Done!\n\nFiles are in:\n{out}\n\n{install_note or build_hint(out)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _link_or_copy_dir(src: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            shutil.copytree(src, dest)
    else:
        dest.symlink_to(src, target_is_directory=True)


def install_into_build_data(game: Path) -> str:
    """If a mhgurecomp build/release is present, ensure its data/ points at the extracted dump."""
    if not (game / "exefs" / "main").is_file() or not (game / "romfs").is_dir():
        return ""
    root = repo_root()
    exe_name = "mhgurecomp.exe" if os.name == "nt" else "mhgurecomp"
    candidates = [
        (root / "build" / exe_name, root / "build" / "data"),
        (root / exe_name, root / "data"),
    ]
    notes: list[str] = []
    for exe, data in candidates:
        if not exe.is_file():
            continue
        # Already extracted straight into this data/ folder.
        if game.resolve() == data.resolve():
            notes.append(f"Ready to start:\n  {exe}")
            continue
        try:
            data.mkdir(parents=True, exist_ok=True)
            for name in ("exefs", "romfs"):
                _link_or_copy_dir((game / name).resolve(), data / name)
            (data / ".complete").touch()
            notes.append(f"Also installed into:\n{data}\n\nYou can start:\n  {exe}")
        except OSError as e:
            notes.append(f"(Could not install into {data}: {e})")
    return "\n\n".join(notes)


# --- The GUI: one window listing what is needed, checking each choice as it is made ------------------

WINDOW_TITLE = "MHGU Recomp — Extract from NSP"
INTRO = ("This copies the game files out of your own Monster Hunter Generations Ultimate dumps so "
         "MHGU Recomp can run them. Choose the files below; each one is checked as you pick it. "
         "Your NSP files are not changed.")

# (key, title, what it is / how to recognise it, kind, required). Order is the order on screen.
FIELDS = [
    ("base", "1.  Base game NSP",
     "The full game, about 12 GB. Its file name usually contains [0100770008DD8000][v0].", "nsp", True),
    ("update", "2.  Update 1.4.0 NSP",
     "The 1.4.0 update, about 200 MB. Its file name usually contains [0100770008DD8800][v262144].", "nsp", True),
    ("keys", "3.  prod.keys",
     "The keys dumped from your own Switch (for example with Lockpick_RCM). Never share this file.", "keys", True),
    ("out", "4.  Where to put the game files",
     "Keep this release's data folder so MHGU Recomp finds the game. About 20 GB must be free while extracting.",
     "dir", True),
    ("title", "Optional:  title.keys",
     "Usually not needed (the NSPs carry their own keys). Leave it empty unless extracting fails on title keys.",
     "keys", False),
]
FIELD_NAMES = {"base": "the base game NSP", "update": "the 1.4.0 update NSP", "keys": "prod.keys",
               "out": "an output folder", "title": "title.keys"}

# Tk file-dialog patterns: case-sensitive off Windows, where "*.*" also skips names without a dot.
NSP_FILES = "*.nsp" if os.name == "nt" else ("*.nsp", "*.NSP")
ALL_FILES = "*.*" if os.name == "nt" else "*"
GAME_EXE = "mhgurecomp.exe" if os.name == "nt" else "mhgurecomp"
STEP_RE = re.compile(r"^(\d)/5 (.*)")


def default_output() -> Path:
    """Release folder: straight into data/ next to mhgurecomp. Source tree: mhgu_game/."""
    root = repo_root()
    return root / "data" if (root / GAME_EXE).is_file() else root / "mhgu_game"


def game_to_start(out: Path) -> Path | None:
    """The mhgurecomp that now has the extracted game in its data/ folder, if any."""
    root = repo_root()
    for exe in (root / GAME_EXE, root / "build" / GAME_EXE):
        if exe.is_file() and (exe.parent / "data" / "exefs" / "main").is_file():
            return exe
    return None


def size_text(n: int) -> str:
    if n >= 1024**4:
        return f"{n / 1024**4:.1f} TB"
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


def check_field(key: str, values: dict[str, str]) -> tuple[str, str]:
    """('ok' | 'warn' | 'bad' | 'empty', text) for one input of the GUI. 'bad' and 'empty' (when required) block Extract."""
    v = values.get(key, "").strip()
    if not v:
        if key == "title":
            return "ok", "Not used (that is fine)"
        if key == "out":
            return "bad", "Choose a folder"
        return "empty", "Not chosen yet"
    p = Path(v).expanduser()

    if key == "out":
        probe = p
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if probe.exists() and not probe.is_dir():
            return "bad", "This is a file, not a folder"
        free = free_bytes(probe)
        if free is not None and free < NEED_FREE:
            return "bad", f"Only {size_text(free)} free here: about 20 GB is needed"
        free_s = f"{size_text(free)} free" if free is not None else "free space unknown"
        if (p / "exefs" / "main").is_file():
            return "warn", f"Already has game files: they will be replaced ({free_s})"
        return "ok", f"OK ({free_s})"

    if not p.is_file():
        return "bad", "File not found"
    size = p.stat().st_size
    name = p.name.upper()

    if key == "base":
        if size < 2 * 1024**3:
            return "bad", f"This file is only {size_text(size)}: the base game is about 12 GB (the small one is the update)"
        if "0100770008DD8800" in name:
            return "warn", "The name says this is the update, not the base game"
        return "ok", f"Base game NSP ({size_text(size)})"

    if key == "update":
        base = values.get("base", "").strip()
        if base and Path(base).expanduser().resolve() == p.resolve():
            return "bad", "This is the same file as the base game: pick the small update NSP"
        if size > 2 * 1024**3:
            return "bad", f"This file is {size_text(size)}: the update is about 200 MB (the big one is the base game)"
        m = re.search(r"\[V(\d+)\]", name)
        if m and int(m.group(1)) != CNMT_VERSION:
            return "warn", f"The name says update v{m.group(1)}: version 1.4.0 is v{CNMT_VERSION}"
        return "ok", f"Update NSP ({size_text(size)})"

    # prod.keys / title.keys: small text files of "name = hex" lines.
    if size > 1024**2:
        return "bad", f"This file is {size_text(size)}: a keys file is a few KB"
    try:
        text = p.read_text(errors="replace").lower()
    except OSError as e:
        return "bad", f"Cannot read it: {e.strerror}"
    if key == "keys":
        if "header_key" not in text:
            if "title.keys" in p.name.lower() or re.search(r"^[0-9a-f]{32}\s*=", text, re.M):
                return "bad", "This is title.keys: pick prod.keys (it holds header_key)"
            return "bad", "This does not look like prod.keys (it has no header_key line)"
        return "ok", "Keys file found"
    if not re.search(r"^[0-9a-f]{32}\s*=", text, re.M):
        return "warn", "This does not look like title.keys (no rights-ID lines)"
    return "ok", "Title keys found"


def gui_blockers(values: dict[str, str]) -> list[str]:
    """What still stops Extract, as phrases for the window's bottom line."""
    missing, wrong = [], []
    for key, _, _, _, required in FIELDS:
        level, _ = check_field(key, values)
        if level == "bad":
            wrong.append(FIELD_NAMES[key])
        elif level == "empty" and required:
            missing.append(FIELD_NAMES[key])
    return missing + wrong


def gui_summary(values: dict[str, str]) -> tuple[str, str]:
    """(level, text) of the window's bottom line: what is left to do, or ready."""
    missing = [FIELD_NAMES[k] for k, _, _, _, req in FIELDS if req and check_field(k, values)[0] == "empty"]
    wrong = [FIELD_NAMES[k] for k, *_ in FIELDS if check_field(k, values)[0] == "bad"]
    parts = []
    if missing:
        parts.append("Still needed: " + ", ".join(missing) + ".")
    if wrong:
        parts.append("Check " + ", ".join(wrong) + " (marked ✖ above).")
    if parts:
        return ("bad" if wrong else "empty"), "  ".join(parts)
    return "ok", "Ready. Click Extract: it takes a few minutes."


def output_error(lines: list[str]) -> str:
    """The failure message in the --cli child's output: its 'Error: …' block, else a traceback, else the tail."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Error: "):
            return "\n".join([lines[i][len("Error: "):]] + lines[i + 1:]).strip()
    for i, line in enumerate(lines):
        if line.startswith("Traceback"):
            return "\n".join(lines[i:])[-2000:]
    return "\n".join(lines[-15:]).strip() or "The extract stopped without a message."


def gtk_available() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def run_gtk() -> None:
    """The window on Linux desktops: GTK 3 through PyGObject, which GNOME/KDE distributions ship by default."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk, Pango

    ok, _ = Gtk.init_check(sys.argv)
    if not ok:
        raise RuntimeError("no display for GTK")

    esc = GLib.markup_escape_text
    colors = {"ok": "#26a269", "warn": "#e5a50a", "bad": "#e01b24", "empty": "#888a85"}
    marks = {"ok": "✔", "warn": "⚠", "bad": "✖", "empty": "○"}
    values = {key: "" for key, *_ in FIELDS}
    values["out"] = str(default_output())
    state = {"proc": None, "cancelled": False, "lines": [], "last_dir": str(Path.home())}

    try:
        hactool_error = ""
        find_hactool()
    except ExtractError as e:
        hactool_error = str(e)

    win = Gtk.Window(title=WINDOW_TITLE)
    win.set_default_size(760, -1)
    header = Gtk.HeaderBar(title="Extract MHGU from NSP", subtitle="Monster Hunter Generations Ultimate 1.4.0")
    header.set_show_close_button(True)
    win.set_titlebar(header)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ("top", "bottom", "start", "end"):
        getattr(outer, f"set_margin_{side}")(18)
    win.add(outer)

    intro = Gtk.Label(label=INTRO, xalign=0)
    intro.set_line_wrap(True)
    intro.set_max_width_chars(80)
    outer.pack_start(intro, False, False, 0)

    if hactool_error:
        bar = Gtk.InfoBar(message_type=Gtk.MessageType.ERROR)
        bar.get_content_area().add(Gtk.Label(label=hactool_error, xalign=0))
        outer.pack_start(bar, False, False, 0)

    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    frame = Gtk.Frame()
    frame.add(listbox)
    outer.pack_start(frame, False, False, 0)

    entries: dict[str, Gtk.Entry] = {}
    status_labels: dict[str, Gtk.Label] = {}

    def choose(key: str, kind: str, title: str) -> None:
        action = Gtk.FileChooserAction.SELECT_FOLDER if kind == "dir" else Gtk.FileChooserAction.OPEN
        dlg = Gtk.FileChooserNative.new(title, win, action, "_Select", "_Cancel")
        current = values[key].strip()
        start = Path(current).expanduser() if current else Path(state["last_dir"])
        folder = start.parent if start.is_file() else start
        while not folder.is_dir() and folder.parent != folder:  # (a folder still to be made: its nearest parent)
            folder = folder.parent
        if folder.is_dir():
            dlg.set_current_folder(str(folder))
        if kind != "dir":
            f = Gtk.FileFilter()
            if kind == "nsp":
                f.set_name("NSP files (*.nsp)")
                f.add_pattern("*.nsp")
                f.add_pattern("*.NSP")
            else:
                f.set_name("Keys files (*.keys)")
                f.add_pattern("*.keys")
            dlg.add_filter(f)
            every = Gtk.FileFilter()
            every.set_name("All files")
            every.add_pattern("*")
            dlg.add_filter(every)
        if dlg.run() == Gtk.ResponseType.ACCEPT and dlg.get_filename():
            picked = dlg.get_filename()
            entries[key].set_text(picked)
            entries[key].set_position(-1)  # (long paths: show the file name end)
            state["last_dir"] = picked if kind == "dir" else str(Path(picked).parent)
        dlg.destroy()

    for key, title, hint, kind, _required in FIELDS:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{side}")(12)
        top = Gtk.Box(spacing=12)
        name = Gtk.Label(xalign=0)
        name.set_markup(f"<b>{esc(title, -1)}</b>")
        top.pack_start(name, False, False, 0)
        row.pack_start(top, False, False, 0)
        desc = Gtk.Label(label=hint, xalign=0)
        desc.set_line_wrap(True)
        desc.set_max_width_chars(90)
        desc.get_style_context().add_class("dim-label")
        row.pack_start(desc, False, False, 0)
        line = Gtk.Box(spacing=6)
        entry = Gtk.Entry(text=values[key], hexpand=True)
        entry.set_placeholder_text("Not used" if key == "title" else "Click Choose… to pick it")
        line.pack_start(entry, True, True, 0)
        btn = Gtk.Button(label="Choose folder…" if kind == "dir" else "Choose file…")
        btn.connect("clicked", lambda _b, k=key, kd=kind, t=title: choose(k, kd, t.split("  ", 1)[-1]))
        line.pack_start(btn, False, False, 0)
        row.pack_start(line, False, False, 0)
        st = Gtk.Label(xalign=0)
        st.set_line_wrap(True)
        st.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        st.set_max_width_chars(90)
        row.pack_start(st, False, False, 0)
        listbox.add(row)
        entries[key] = entry
        status_labels[key] = st

    progress = Gtk.ProgressBar(show_text=True)
    progress.set_no_show_all(True)
    outer.pack_start(progress, False, False, 0)

    bottom = Gtk.Box(spacing=10)
    spinner = Gtk.Spinner()
    bottom.pack_start(spinner, False, False, 0)
    footer = Gtk.Label(xalign=0)
    footer.set_line_wrap(True)
    footer.set_selectable(True)
    bottom.pack_start(footer, True, True, 0)
    start_btn = Gtk.Button(label="Start MHGU Recomp")
    start_btn.get_style_context().add_class("suggested-action")
    start_btn.set_no_show_all(True)
    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.get_style_context().add_class("destructive-action")
    cancel_btn.set_no_show_all(True)
    extract_btn = Gtk.Button(label="Extract")
    extract_btn.get_style_context().add_class("suggested-action")
    for b in (extract_btn, cancel_btn, start_btn):
        b.set_size_request(120, -1)
        bottom.pack_end(b, False, False, 0)
    outer.pack_start(bottom, False, False, 0)

    def set_footer(text: str, level: str | None = None) -> None:
        if level:
            footer.set_markup(f'<span foreground="{colors[level]}">{marks[level]}</span>  {esc(text, -1)}')
        else:
            footer.set_text(text)

    def refresh(*_args) -> None:
        if state["proc"]:
            return
        for key, *_ in FIELDS:
            values[key] = entries[key].get_text()
        for key, *_ in FIELDS:
            level, text = check_field(key, values)
            status_labels[key].set_markup(
                f'<span foreground="{colors[level]}" weight="bold">{marks[level]}  {esc(text, -1)}</span>')
        level, text = gui_summary(values)
        extract_btn.set_sensitive(level == "ok" and not hactool_error)
        if hactool_error:
            set_footer("hactool is missing (see above): extracting needs it.", "bad")
        else:
            set_footer(text, level)

    for e in entries.values():
        e.connect("changed", refresh)

    def set_running(on: bool) -> None:
        listbox.set_sensitive(not on)
        extract_btn.set_visible(not on)
        cancel_btn.set_visible(on)
        cancel_btn.set_sensitive(True)
        (spinner.start if on else spinner.stop)()
        if on:
            progress.set_fraction(0.0)
            progress.set_text("Starting…")
            progress.show()

    def on_line(line: str) -> bool:
        state["lines"].append(line)
        m = STEP_RE.match(line)
        if m:
            k = int(m.group(1))
            progress.set_fraction((k - 1) / 5)
            progress.set_text(f"Step {k} of 5:  {m.group(2)}")
            set_footer(m.group(2))
        elif line.startswith("  ") and line.strip():
            set_footer(line.strip())
        return False

    def on_exit(rc: int) -> bool:
        state["proc"] = None
        set_running(False)
        out = Path(values["out"].strip()).expanduser()
        if state["cancelled"]:
            progress.hide()
            refresh()
            set_footer("Cancelled. Nothing was changed; you can start again.", "warn")
            return False
        if rc == 0:
            progress.set_fraction(1.0)
            progress.set_text("Finished")
            extract_btn.hide()
            exe = game_to_start(out)
            if exe:
                start_btn.show()
                set_footer(f"Done! The game files are in {out}. Click Start MHGU Recomp to play.", "ok")
            else:
                set_footer(f"Done! The game files are in {out}.\n{build_hint(out)}", "ok")
            state["exe"] = exe
            return False
        progress.hide()
        refresh()
        msg = output_error(state["lines"])
        set_footer("Extraction failed: see the message.", "bad")
        dlg = Gtk.MessageDialog(transient_for=win, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text="Extraction failed")
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()
        return False

    def reader(proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            GLib.idle_add(on_line, line.rstrip("\n"))
        GLib.idle_add(on_exit, proc.wait())

    def do_extract(_btn) -> None:
        refresh()
        if gui_blockers(values) or hactool_error:
            return
        cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--cli",
               "--base", values["base"].strip(), "--update", values["update"].strip(),
               "--keys", values["keys"].strip(), "--out", values["out"].strip()]
        if values["title"].strip():
            cmd += ["--title-keys", values["title"].strip()]
        state.update(cancelled=False, lines=[])
        state["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         bufsize=1, errors="replace")
        set_running(True)
        threading.Thread(target=reader, args=(state["proc"],), daemon=True).start()

    def do_cancel(_btn) -> None:
        proc = state["proc"]
        if proc and proc.poll() is None:
            state["cancelled"] = True
            cancel_btn.set_sensitive(False)
            set_footer("Cancelling…")
            proc.terminate()  # (the --cli child unwinds on SIGTERM: hactool stops, the work folder goes)

    def do_start(_btn) -> None:
        exe = state.get("exe")
        if exe:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            win.destroy()

    def on_close(*_args) -> bool:
        proc = state["proc"]
        if proc and proc.poll() is None:
            dlg = Gtk.MessageDialog(transient_for=win, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.YES_NO, text="Stop the extraction?")
            dlg.format_secondary_text("It is still running. Stopping it leaves your NSP files as they are.")
            answer = dlg.run()
            dlg.destroy()
            if answer != Gtk.ResponseType.YES:
                return True
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        return False

    extract_btn.connect("clicked", do_extract)
    cancel_btn.connect("clicked", do_cancel)
    start_btn.connect("clicked", do_start)
    win.connect("delete-event", on_close)
    win.connect("destroy", lambda *_: Gtk.main_quit())

    refresh()
    win.show_all()
    Gtk.main()


def run_tk() -> None:
    """The window on Windows (and on Linux when GTK is missing): tkinter."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    win = tk.Tk()
    win.title(WINDOW_TITLE)
    win.geometry("780x760")
    win.minsize(640, 640)

    values: dict[str, tk.StringVar] = {key: tk.StringVar() for key, *_ in FIELDS}
    values["out"].set(str(default_output()))
    colors = {"ok": "#1e8a4c", "warn": "#a86b00", "bad": "#c01c28", "empty": "#777777"}
    marks = {"ok": "✔", "warn": "⚠", "bad": "✖", "empty": "○"}
    busy = {"on": False}
    status_labels: dict[str, ttk.Label] = {}

    try:
        hactool_error = ""
        find_hactool()
    except ExtractError as e:
        hactool_error = str(e)

    def current() -> dict[str, str]:
        return {k: v.get() for k, v in values.items()}

    def browse(key: str, kind: str, title: str) -> None:
        if kind == "dir":
            p = filedialog.askdirectory(parent=win, title=title, initialdir=values[key].get() or None)
        else:
            pats = [("NSP files", NSP_FILES)] if kind == "nsp" else [("Keys files", "*.keys")]
            p = filedialog.askopenfilename(parent=win, title=title, filetypes=pats + [("All files", ALL_FILES)])
        if p:
            values[key].set(p)

    frm = ttk.Frame(win, padding=14)
    frm.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frm, text="Extract MHGU from NSP", font=("TkDefaultFont", 14, "bold")).pack(anchor=tk.W)
    ttk.Label(frm, text=INTRO, wraplength=720, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 10))
    if hactool_error:
        ttk.Label(frm, text=hactool_error, foreground=colors["bad"], wraplength=720,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 8))

    for key, title, hint, kind, _required in FIELDS:
        box = ttk.Frame(frm, padding=(0, 6))
        box.pack(fill=tk.X)
        head = ttk.Frame(box)
        head.pack(fill=tk.X)
        ttk.Label(head, text=title, font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(box, text=hint, foreground="#666666", wraplength=720, justify=tk.LEFT).pack(anchor=tk.W)
        line = ttk.Frame(box)
        line.pack(fill=tk.X, pady=(2, 0))
        ttk.Entry(line, textvariable=values[key]).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(line, text="Choose folder…" if kind == "dir" else "Choose file…", width=14,
                   command=lambda k=key, kd=kind, t=title: browse(k, kd, t.split("  ", 1)[-1])).pack(side=tk.RIGHT, padx=(6, 0))
        st = ttk.Label(box, text="", wraplength=720, justify=tk.LEFT, font=("TkDefaultFont", 10, "bold"))
        st.pack(anchor=tk.W, pady=(2, 0))
        status_labels[key] = st

    progress = ttk.Progressbar(frm, maximum=5)
    progress.pack(fill=tk.X, pady=(12, 4))
    bottom = ttk.Frame(frm)
    bottom.pack(fill=tk.X)
    footer = ttk.Label(bottom, text="", wraplength=560, justify=tk.LEFT)
    footer.pack(side=tk.LEFT, fill=tk.X, expand=True)
    extract_btn = ttk.Button(bottom, text="Extract", width=14)
    extract_btn.pack(side=tk.RIGHT)

    def refresh(*_args) -> None:
        if busy["on"]:
            return
        vals = current()
        for key, *_ in FIELDS:
            level, text = check_field(key, vals)
            status_labels[key].configure(text=f"{marks[level]}  {text}", foreground=colors[level])
        level, text = gui_summary(vals)
        extract_btn.configure(state=tk.NORMAL if level == "ok" and not hactool_error else tk.DISABLED)
        if hactool_error:
            footer.configure(text="✖  hactool is missing (see above): extracting needs it.", foreground=colors["bad"])
        else:
            footer.configure(text=f"{marks[level]}  {text}", foreground=colors[level])

    for v in values.values():
        v.trace_add("write", refresh)

    def set_progress(msg: str) -> None:
        def apply() -> None:
            m = STEP_RE.match(msg)
            if m:
                progress.configure(value=int(m.group(1)) - 1)
                footer.configure(text=f"Step {m.group(1)} of 5:  {m.group(2)}", foreground="")
            elif msg.startswith("  ") and msg.strip():
                footer.configure(text=msg.strip(), foreground="")
        win.after(0, apply)

    def finish(ok: bool, msg: str, out: Path) -> None:
        busy["on"] = False
        if ok:
            progress.configure(value=5)
            exe = game_to_start(out)
            footer.configure(text=f"✔  Done! The game files are in {out}.", foreground=colors["ok"])
            extract_btn.configure(state=tk.DISABLED)
            if exe and messagebox.askyesno("Done", f"The game files are in:\n{out}\n\nStart MHGU Recomp now?", parent=win):
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
                win.destroy()
            elif not exe:
                messagebox.showinfo("Done", f"The game files are in:\n{out}\n\n{build_hint(out)}", parent=win)
        else:
            progress.configure(value=0)
            refresh()
            footer.configure(text="✖  Extraction failed: see the message.", foreground=colors["bad"])
            messagebox.showerror("Extraction failed", msg, parent=win)

    def do_extract() -> None:
        vals = current()
        if busy["on"] or gui_blockers(vals) or hactool_error:
            return
        out = Path(vals["out"].strip()).expanduser()
        title_keys = Path(vals["title"].strip()) if vals["title"].strip() else None
        busy["on"] = True
        extract_btn.configure(state=tk.DISABLED)
        footer.configure(text="Starting…", foreground="")

        def worker() -> None:
            try:
                extract_mhgu(Path(vals["base"].strip()), Path(vals["update"].strip()), Path(vals["keys"].strip()),
                             out, title_keys, progress=set_progress)
                win.after(0, lambda: finish(True, "", out))
            except ExtractError as e:
                win.after(0, lambda m=str(e): finish(False, m, out))
            except Exception:
                win.after(0, lambda m=traceback.format_exc()[-2000:]: finish(False, m, out))

        threading.Thread(target=worker, daemon=True).start()

    extract_btn.configure(command=do_extract)
    refresh()
    win.mainloop()


def run_gui() -> None:
    """GTK on Linux desktops (looks native, ships with them), else tkinter."""
    if os.name != "nt" and gtk_available():
        try:
            run_gtk()
            return
        except RuntimeError:
            pass  # (no display for GTK: try Tk, which reports its own error)
    run_tk()


def run_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cli", action="store_true", help="Force command-line mode")
    ap.add_argument("--gui", action="store_true", help="Force GUI mode")
    ap.add_argument("--base", type=Path)
    ap.add_argument("--update", type=Path)
    ap.add_argument("--keys", type=Path)
    ap.add_argument("--title-keys", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.gui or (not args.cli and not any([args.base, args.update, args.keys, args.out])):
        run_gui()
        return 0

    if not args.base or not args.update or not args.keys or not args.out:
        ap.error("--cli needs --base --update --keys --out")
    # Extract MHGU NSP.sh stops a cancelled run with SIGTERM: unwind, so hactool is killed and the work folder removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        extract_mhgu(args.base, args.update, args.keys, args.out, args.title_keys)
    except ExtractError as e:
        print(f"Error: {e}", file=sys.stderr)  # (the GTK window shows what follows "Error: ")
        return 1
    return 0


if __name__ == "__main__":
    # Default: GUI. Use --cli for scripts.
    if len(sys.argv) == 1:
        try:
            run_gui()
        except Exception:
            print("GUI failed; use: python tools/extract_mhgu_nsp.py --cli ...", file=sys.stderr)
            traceback.print_exc()
            sys.exit(1)
    else:
        sys.exit(run_cli())
