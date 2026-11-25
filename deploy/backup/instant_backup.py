"""ESSA INSTANT BACKUP: back up right now, at any time of the day.

Started by double-clicking instant_backup.bat. Clicked at 10 AM, it saves all
data up to 10 AM (every bill, stock entry and customer so far), while ESSA keeps
running and billing. A message box says when it is done, or why it failed.

    Saved as ESSA-INSTANT-2026-11-21_100512.zip in the backup folder, and on
    the pen drive too if it is plugged in. The newest 20 are kept (keep_manual);
    the nightly daily-backup clean-up never deletes them.

The backup itself, and the SETTINGS (folder, pen drive, how many to keep), are
in daily_backup.py beside this file, so both backups always agree.

From a command prompt:
    instant_backup.py                 back up now (message box at the end)
    instant_backup.py --folder X      into folder X instead of the usual one
    instant_backup.py --no-copies     skip the pen drive / extra folder

Exit code: 0 OK, 1 FAILED, 2 made but the pen drive / extra copy was missed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import daily_backup as engine
except ImportError:
    print("daily_backup.py is missing: keep it in the same folder as instant_backup.py.")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="ESSA instant backup")
    ap.add_argument("--folder", help="use this backup folder instead of the one in SETTINGS")
    ap.add_argument("--no-copies", action="store_true", help="skip pen drive and extra folder")
    args = ap.parse_args()
    if args.folder:
        engine.SETTINGS["backup_folder"] = args.folder
    if args.no_copies:
        engine.SETTINGS["usb_label"] = engine.SETTINGS["extra_folder"] = ""

    code, status = engine.backup(manual=True)
    note = "" if engine.task_installed() else (
        "Reminder: the automatic 10:30 PM daily backup is NOT set up yet. "
        "Right-click daily_backup.bat > Run as administrator once.")
    engine.report(code, status, note, title="ESSA instant backup")
    if note:
        print(note)
    return code


if __name__ == "__main__":
    sys.exit(main())
