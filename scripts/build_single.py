#!/usr/bin/env python3
"""Compatibility entry point for the readable Python 3.6+ CamFlow build.

The previous implementation embedded a compressed release tree and required
Python 3.10.  CamFlow now follows CamC's readable source-concatenation model;
use build_camflow.py for new callers.
"""
from __future__ import print_function

import argparse
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.path.join(ROOT, "dist", "camflow"))
    parser.add_argument("--release-dir", help="obsolete; accepted for compatibility")
    args = parser.parse_args(argv)
    sys.path.insert(0, ROOT)
    import build_camflow
    output = build_camflow.build()
    parent = os.path.dirname(os.path.abspath(args.output))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(args.output, "w") as handle:
        handle.write(output)
    os.chmod(args.output, os.stat(args.output).st_mode | 0o111)
    print("Built %s (%d lines)" % (args.output, output.count("\n")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
