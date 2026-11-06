#!/usr/bin/env bash
# MHGU Recomp - Extract from NSP (Linux counterpart of "Extract MHGU NSP.bat").
#
# Opens the file pickers for the base game NSP, the 1.4.0 update NSP and prod.keys, then unpacks
# exefs/ + romfs/ into data/ with the hactool shipped beside mhgurecomp. Original NSPs are not modified.
#
# Uses the tkinter window of tools/extract_mhgu_nsp.py when Python has tkinter; otherwise the same
# steps with zenity (GNOME and most desktops) or kdialog (KDE) dialogs. Run it from a file manager
# ("Run as a Program") or a terminal: ./"Extract MHGU NSP.sh"
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

# The command that installs tkinter on this distribution.
tk_hint() {
    if command -v apt-get >/dev/null 2>&1; then echo "sudo apt install python3-tk"
    elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install python3-tkinter"
    elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S tk"
    elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install python3-tk"
    else echo "install your distribution's python3 tkinter package"
    fi
}

[ -f "$SCRIPT" ] || fail "$SCRIPT is missing next to this script. Extract the whole release archive again."

PY=
for p in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY=$p
        break
    fi
done
[ -n "$PY" ] || fail "Need Python 3.10 or newer (python3). Install it with your package manager, then run this again."

# tar keeps the executable bit, but a copy through some file managers or archivers drops it.
for f in hactool build/hactool; do
    [ -f "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null
done

if "$PY" -c 'import tkinter' 2>/dev/null; then
    exec "$PY" "$SCRIPT"
fi

if [ -z "$HAVE_ZENITY" ] && [ -z "$HAVE_KDIALOG" ]; then
    fail "This needs tkinter for Python (or zenity / kdialog). Install it with:

  $(tk_hint)

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

ask "Extract Monster Hunter Generations Ultimate 1.4.0 from NSP files.

You need the base game NSP, the 1.4.0 update NSP, and your own prod.keys. About 20 GB free space is required. Original NSPs are not changed.

Continue?" || exit 0

start=$HOME
BASE=$(pick_file "Select base game NSP (~12 GB, title …8000)" "*.nsp *.NSP" "NSP files" "$start") || exit 0
start=$(dirname "$BASE")
UPDATE=$(pick_file "Select 1.4.0 update NSP (~200 MB, title …8800)" "*.nsp *.NSP" "NSP files" "$start") || exit 0
KEYS=$(pick_file "Select prod.keys" "*.keys" "Keys" "$start") || exit 0
TITLE_KEYS=
if ask "Do you also have a title.keys file? (Optional: usually not needed.)"; then
    TITLE_KEYS=$(pick_file "Select title.keys" "*.keys" "Keys" "$(dirname "$KEYS")") || TITLE_KEYS=
fi
# Release folder: straight into data/ next to mhgurecomp (as the tkinter window does). Source tree: mhgu_game/.
if [ -f mhgurecomp ]; then default_out=$PWD/data; else default_out=$PWD/mhgu_game; fi
mkdir -p "$default_out" 2>/dev/null
OUT=$(pick_dir "Select output folder (this release's data folder)" "$default_out") || exit 0

args=(--cli --base "$BASE" --update "$UPDATE" --keys "$KEYS" --out "$OUT")
[ -n "$TITLE_KEYS" ] && args+=(--title-keys "$TITLE_KEYS")

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
