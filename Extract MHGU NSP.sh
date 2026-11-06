#!/usr/bin/env bash
# MHGU Recomp - Extract from NSP (Linux counterpart of "Extract MHGU NSP.bat").
#
# Opens a window listing what is needed (the base game NSP, the 1.4.0 update NSP, prod.keys, the output
# folder), checks each file as it is picked, then unpacks exefs/ + romfs/ into data/ with the hactool
# shipped beside mhgurecomp. Original NSPs are not modified.
#
# The window is tools/extract_mhgu_nsp.py's: GTK 3 (python3-gi, on GNOME/KDE desktops by default), else
# tkinter. With neither, the same steps run as zenity (GNOME) or kdialog (KDE) dialogs. Run it from a
# file manager ("Run as a Program") or a terminal: ./"Extract MHGU NSP.sh"
set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1

TITLE="MHGU Recomp - Extract from NSP"
SCRIPT=tools/extract_mhgu_nsp.py
HAVE_ZENITY=$(command -v zenity >/dev/null 2>&1 && echo 1)
HAVE_KDIALOG=$(command -v kdialog >/dev/null 2>&1 && echo 1)

# An error the player can see even when started from a file manager (no terminal).
fail() {
    if [ -n "$HAVE_ZENITY" ]; then
        zenity --error --no-markup --title="$TITLE" --width=480 --text="$1" 2>/dev/null
    elif [ -n "$HAVE_KDIALOG" ]; then
        kdialog --title "$TITLE" --error "$1" 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1 && [ ! -t 1 ]; then
        notify-send "$TITLE" "$1"
    fi
    printf '%s\n' "$1" >&2
    exit 1
}

# The command that installs the window's toolkit (GTK for Python) on this distribution.
gui_hint() {
    if command -v apt-get >/dev/null 2>&1; then echo "sudo apt install python3-gi gir1.2-gtk-3.0"
    elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install python3-gobject gtk3"
    elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S python-gobject gtk3"
    elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-3_0"
    else echo "install your distribution's Python GTK 3 bindings (PyGObject) or python3 tkinter"
    fi
}

[ -f "$SCRIPT" ] || fail "$SCRIPT is missing next to this script. Extract the whole release archive again."

# The window's Python: the first 3.10+ with GTK, else the first with tkinter; else the first 3.10+ for the dialogs.
# (/usr/bin/python3 too: a pyenv/conda/uv python3 first in PATH usually lacks the system's GTK bindings.)
PY= GTK_PY= TK_PY=
for p in python3 /usr/bin/python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    command -v "$p" >/dev/null 2>&1 || continue
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || continue
    [ -n "$PY" ] || PY=$p
    if [ -z "$GTK_PY" ] && "$p" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
        GTK_PY=$p
    fi
    if [ -z "$TK_PY" ] && "$p" -c 'import tkinter' >/dev/null 2>&1; then
        TK_PY=$p
    fi
done
GUI_PY=${GTK_PY:-$TK_PY}
[ -n "$PY" ] || fail "Need Python 3.10 or newer (python3). Install it with your package manager, then run this again."

# tar keeps the executable bit, but a copy through some file managers or archivers drops it.
for f in hactool build/hactool; do
    [ -f "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null
done

if [ -n "$GUI_PY" ]; then
    exec "$GUI_PY" "$SCRIPT" --gui
fi

if [ -z "$HAVE_ZENITY" ] && [ -z "$HAVE_KDIALOG" ]; then
    fail "The extract window needs GTK for Python (or tkinter, zenity or kdialog). Install it with:

  $(gui_hint)

then run this again. Or from a terminal, without any window:
  $PY $SCRIPT --cli --base game.nsp --update update.nsp --keys prod.keys --out data"
fi

# --- No tkinter: the same steps with zenity / kdialog dialogs ------------------------------------

pick_file() {  # title, filter pattern, filter label, start dir
    if [ -n "$HAVE_ZENITY" ]; then
        zenity --file-selection --title="$1" --filename="$4/" \
            --file-filter="$3 | $2" --file-filter="All files | *" 2>/dev/null
    else
        kdialog --title "$1" --getopenfilename "$4" "$2|$3" 2>/dev/null
    fi
}

pick_dir() {  # title, start dir
    if [ -n "$HAVE_ZENITY" ]; then
        zenity --file-selection --directory --title="$1" --filename="$2/" 2>/dev/null
    else
        kdialog --title "$1" --getexistingdirectory "$2" 2>/dev/null
    fi
}

ask() {  # yes/no question
    if [ -n "$HAVE_ZENITY" ]; then
        zenity --question --no-markup --title="$TITLE" --width=420 --text="$1" 2>/dev/null
    else
        kdialog --title "$TITLE" --yesno "$1" 2>/dev/null
    fi
}

info() {
    if [ -n "$HAVE_ZENITY" ]; then
        zenity --info --no-markup --title="$TITLE" --width=480 --text="$1" 2>/dev/null
    else
        kdialog --title "$TITLE" --msgbox "$1" 2>/dev/null
    fi
}

ask "This copies the game files out of your own Monster Hunter Generations Ultimate dumps so MHGU Recomp can run them. You will be asked for these, one at a time:

  1. Base game NSP: the full game, about 12 GB
      (its name usually contains [0100770008DD8000][v0])
  2. Update 1.4.0 NSP: about 200 MB
      (its name usually contains [0100770008DD8800][v262144])
  3. prod.keys: the keys dumped from your own Switch
  4. Where to put the game files: keep this release's data folder

About 20 GB must be free while extracting. Your NSP files are not changed.

Start?" || exit 0

start=$HOME
BASE=$(pick_file "Step 1 of 4: the base game NSP (about 12 GB, …8000][v0])" "*.nsp *.NSP" "NSP files" "$start") || exit 0
start=$(dirname "$BASE")
UPDATE=$(pick_file "Step 2 of 4: the 1.4.0 update NSP (about 200 MB, …8800][v262144])" "*.nsp *.NSP" "NSP files" "$start") || exit 0
KEYS=$(pick_file "Step 3 of 4: prod.keys (from your own Switch)" "*.keys" "Keys" "$start") || exit 0
# Release folder: straight into data/ next to mhgurecomp (as the tkinter window does). Source tree: mhgu_game/.
if [ -f mhgurecomp ]; then default_out=$PWD/data; else default_out=$PWD/mhgu_game; fi
mkdir -p "$default_out" 2>/dev/null
OUT=$(pick_dir "Step 4 of 4: where to put the game files (keep this release's data folder)" "$default_out") || exit 0

args=(--cli --base "$BASE" --update "$UPDATE" --keys "$KEYS" --out "$OUT")

log=$(mktemp "${TMPDIR:-/tmp}/mhgu_extract.XXXXXX.log")
"$PY" -u "$SCRIPT" "${args[@]}" >"$log" 2>&1 &
pid=$!

if [ -n "$HAVE_ZENITY" ]; then
    # Each progress line becomes the dialog's text; Cancel stops the extraction (it cleans up its work folder).
    tail -n +1 -f --pid="$pid" "$log" | sed -u 's/^/# /' |
        zenity --progress --pulsate --auto-close --no-markup --title="$TITLE" --width=480 \
            --text="Starting…" 2>/dev/null
    if [ "${PIPESTATUS[2]}" -ne 0 ]; then  # (Cancel or closed; auto-close after the end is 0)
        kill -TERM "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        rm -f "$log"
        info "Extraction cancelled. The NSPs were not changed."
        exit 0
    fi
else
    kdialog --title "$TITLE" --passivepopup "Extracting… this takes several minutes. A message appears when it is done." 10 2>/dev/null &
fi

wait "$pid"
status=$?
if [ "$status" -eq 0 ]; then
    # From "Done!" on: where the files went and what to start.
    info "$(sed -n '/^Done!/,$p' "$log")"
    rm -f "$log"
else
    fail "Extraction failed:

$(tail -n 20 "$log")

(Full log: $log)"
fi
