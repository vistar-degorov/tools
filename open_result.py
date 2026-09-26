#! /usr/bin/python

import os
import sys
from git_search import Config, get_search_result, open_with

if __name__ == "__main__":
  if sys.stdin.isatty():
    sys.exit(0)

  inp = sys.stdin.buffer.read()
  inp = inp.splitlines()

  idx = 0
  if len(sys.argv) > 1:
    idx = int(sys.argv[1])

  config = Config([])

  _TOOL = os.getenv("GG_TOOL") or os.getenv("GG_TOOL_DEFAULT") or ""
  open_with(_TOOL, get_search_result(inp, idx, config))
