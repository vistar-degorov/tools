#! /usr/bin/python

import re
import sys

from git_search import (
  SearchResult,
  open_search_result,
  remove_color_codes,
  resolve_setting,
  select_result,
)


def _parse_result(line):
  line = remove_color_codes(line.decode("utf-8", "replace"))
  if match := re.match(r"^(.*?):(\d+):(.*)$", line):
    path, line_number, text = match.groups()
    return SearchResult(path, int(line_number), 0, text)
  return SearchResult(line, None, None, "")


if __name__ == "__main__":
  if sys.stdin.isatty():
    sys.exit(0)

  results = [
    _parse_result(line)
    for line in sys.stdin.buffer.read().splitlines()
  ]

  idx = 0
  if len(sys.argv) > 1:
    idx = int(sys.argv[1])

  result = select_result(results, idx, clamp=True)
  if result is not None:
    tool = resolve_setting("GG_TOOL", "")
    sys.exit(open_search_result(tool, result))
