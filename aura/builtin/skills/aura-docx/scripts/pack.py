#!/usr/bin/env python3
"""
Pack an unpacked DOCX folder back into a .docx file.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from utilities import zip_dir_to_docx

def main() -> None:
    ap = argparse.ArgumentParser(description="Pack folder -> DOCX")
    ap.add_argument("src_dir", type=Path)
    ap.add_argument("out_docx", type=Path)
    args = ap.parse_args()

    zip_dir_to_docx(args.src_dir, args.out_docx)
    print(str(args.out_docx))

if __name__ == "__main__":
    main()
