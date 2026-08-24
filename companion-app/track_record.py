#!/usr/bin/env python3
"""Thin, brand-named entry point: `python track_record.py` instead of
`python review_ui.py`. review_ui.py itself keeps its plain module name -
same convention as every other file here (plan.py, apply.py, scoring.py,
...: a functional name for what the module does, not the brand, since a
DJ never actually sees a filename, only the window title, which already
reads "Track Record"). This file exists purely so the command you type
matches the app.

Same {"__main__", "__mp_main__"} guard as review_ui.py's own - see the
bottom of that file for why: native mode's window spawn can re-execute
whatever file was originally run as sys.argv[0], so main() has to stay
gated behind this guard here too, not called unconditionally at import
time.
"""
from review_ui import main

if __name__ in {"__main__", "__mp_main__"}:
    main()
