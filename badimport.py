#! /usr/bin/python

from pathlib import Path
import re
import sys

def should_ignore(match, text):
  phrase = " __future__"
  return phrase == text[match.start() - len(phrase) : match.start()]

if __name__ == "__main__":
  imp_rx = re.compile(r' *\bimport +(.+)')
  imp_ml_rx = re.compile(r' *\bimport +\((.+?)\)', flags=re.DOTALL)
  ident_rx = re.compile(r'([\w\.\*]+)(.*)', flags=re.DOTALL)
  rename_rx = re.compile(r'as +(\w+)(.*)', flags=re.DOTALL)

  def _parse_import(match, trailing_comma_ok):
    end_idx = match.end()
    imported_names = []

    for line in match.group(1).splitlines():
      line = line.lstrip()

      while match := ident_rx.match(line):
        orig_name, line = match.group(1, 2)
        line = line.lstrip()

        if match := rename_rx.match(line):
          new_name, line = match.group(1, 2)
          line = line.lstrip()
        else:
          new_name = orig_name

        imported_names.append((orig_name, new_name, end_idx))

        if has_comma := line.startswith(","):
          line = line[1:].lstrip()
        if line == "" or line.startswith("#"):
          if has_comma and not trailing_comma_ok:
            return ()
          break
        elif not has_comma:
          return ()
    return imported_names

  def _parse_imports(text):
    for match in imp_rx.finditer(text):
      if not should_ignore(match, text):
        yield from _parse_import(match, trailing_comma_ok=False)
    for match in imp_ml_rx.finditer(text):
      if not should_ignore(match, text):
        yield from _parse_import(match, trailing_comma_ok=True)

  for filename in sys.argv[1:]:
    if Path(filename).name == "__init__.py":
      continue

    try:
      with open(filename, 'r') as istr:
        text = istr.read()
    except FileNotFoundError as err:
      sys.stderr.write(f"Error reading {filename}\n")
      continue

    list_header = f"- {filename}:\n"
    for _, new_name, begin_idx in _parse_imports(text):
      if new_name == "*":
        continue
      try:
        rx = re.compile(r'\b' + re.escape(new_name) + r'\b')
      except re.error as err:
        sys.stderr.write(f"Error in {filename}:\n  {err}\n")
        continue
      if rx.search(text, begin_idx) is None:
        print(f"{list_header}    {new_name}")
        list_header = ""
