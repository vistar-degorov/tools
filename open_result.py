#! /usr/bin/python

import os
import sys
from finddef import Config, split_result_idx, open_search_result, remove_color_codes

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
  open_search_result(inp, idx, config, tool=_TOOL)
