#! /usr/bin/python

import io
from pathlib import Path
import sys
import tokenize


VERSION = "0.0.2"


def _tokenize_best_effort(text):
  tokens = []
  lines = text.splitlines(keepends=True)
  line_offset = 0

  while line_offset < len(lines):
    segment = "".join(lines[line_offset:])
    token_generator = tokenize.generate_tokens(io.StringIO(segment).readline)
    try:
      while True:
        item = next(token_generator)
        item = item._replace(
          start=(item.start[0] + line_offset, item.start[1]),
          end=(item.end[0] + line_offset, item.end[1]),
        )
        tokens.append(item)
    except StopIteration:
      break
    except IndentationError as err:
      # Skip the offending line and resume so imports later in the file are
      # still visible. Tokens yielded before the error remain useful.
      error_line = err.lineno or 1
      line_offset += max(1, error_line)
    except tokenize.TokenError:
      # Errors such as an unterminated bracket occur at EOF, after all tokens
      # that can be recovered have already been yielded.
      break
  return tokens


def _iter_statements(tokens):
  current = []
  bracket_depth = 0
  ignored = {
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.INDENT,
    tokenize.DEDENT,
  }

  for item in tokens:
    if item.type in ignored:
      continue
    if item.type == tokenize.ENDMARKER:
      break
    if item.type in (tokenize.NEWLINE, tokenize.NL):
      if bracket_depth == 0 and current:
        yield current
        current = []
      continue
    if item.type == tokenize.ERRORTOKEN and item.string in ("\n", "\r\n"):
      if bracket_depth == 0 and current:
        yield current
        current = []
      continue

    # Recover after an unmatched opening bracket when a later physical line
    # clearly starts another import statement.
    starts_import = (
      item.type == tokenize.NAME
      and item.string in ("from", "import")
      and item.line[:item.start[1]].strip() == ""
    )
    continues_from_import = (
      current
      and current[0].string == "from"
      and item.string == "import"
      and not any(
        token.string == "import" for token in current[1:]
      )
    )
    if current and starts_import and not continues_from_import:
      yield current
      current = []
      bracket_depth = 0

    if item.type == tokenize.OP:
      if item.string in "([{":
        bracket_depth += 1
      elif item.string in ")]}":
        bracket_depth = max(0, bracket_depth - 1)
      elif bracket_depth == 0 and item.string in (";", ":"):
        if current:
          yield current
          current = []
        continue

    current.append(item)

  if current:
    yield current


def _is_name(item, value=None):
  return (
    item.type == tokenize.NAME
    and (value is None or item.string == value)
  )


def _parse_dotted_name(statement, index):
  if index >= len(statement) or not _is_name(statement[index]):
    return None, index
  if statement[index].string in ("as", "import"):
    return None, index

  parts = [statement[index].string]
  index += 1
  while (
    index + 1 < len(statement)
    and statement[index].type == tokenize.OP
    and statement[index].string == "."
    and _is_name(statement[index + 1])
  ):
    parts.append(statement[index + 1].string)
    index += 2
  return ".".join(parts), index


def _parse_direct_import(statement):
  imported_names = []
  index = 1

  while index < len(statement):
    original_name, next_index = _parse_dotted_name(statement, index)
    if original_name is None:
      break
    index = next_index

    alias = None
    if index < len(statement) and _is_name(statement[index], "as"):
      if index + 1 >= len(statement) or not _is_name(statement[index + 1]):
        index += 1
        break
      alias = statement[index + 1].string
      index += 2

    displayed_name = alias or original_name
    bound_name = alias or original_name.split(".", 1)[0]
    imported_names.append((displayed_name, bound_name))

    if not (
      index < len(statement)
      and statement[index].type == tokenize.OP
      and statement[index].string == ","
    ):
      break
    index += 1

  return imported_names, index


def _parse_from_import(statement, import_index):
  imported_names = []
  index = import_index + 1
  parenthesized = False

  if (
    index < len(statement)
    and statement[index].type == tokenize.OP
    and statement[index].string == "("
  ):
    parenthesized = True
    index += 1

  if (
    index < len(statement)
    and statement[index].type == tokenize.OP
    and statement[index].string == "*"
  ):
    return [("*", "*")], index + 1

  while index < len(statement):
    if (
      parenthesized
      and statement[index].type == tokenize.OP
      and statement[index].string == ")"
    ):
      index += 1
      break
    if not _is_name(statement[index]):
      break

    original_name = statement[index].string
    index += 1
    alias = None
    if index < len(statement) and _is_name(statement[index], "as"):
      if index + 1 >= len(statement) or not _is_name(statement[index + 1]):
        index += 1
        break
      alias = statement[index + 1].string
      index += 2

    displayed_name = alias or original_name
    imported_names.append((displayed_name, displayed_name))

    if (
      index < len(statement)
      and statement[index].type == tokenize.OP
      and statement[index].string == ","
    ):
      index += 1
      continue
    if (
      parenthesized
      and index < len(statement)
      and statement[index].type == tokenize.OP
      and statement[index].string == ")"
    ):
      index += 1
    break

  return imported_names, index


def _parse_import_statement(statement):
  if not statement or not _is_name(statement[0]):
    return None

  if statement[0].string == "import":
    imported_names, end_index = _parse_direct_import(statement)
  elif statement[0].string == "from":
    import_index = next(
      (
        index
        for index, item in enumerate(statement[1:], start=1)
        if _is_name(item, "import")
      ),
      None,
    )
    if import_index is None:
      return None

    module_name = "".join(
      item.string for item in statement[1:import_index]
    )
    imported_names, end_index = _parse_from_import(statement, import_index)
    if module_name == "__future__":
      imported_names = []
  else:
    return None

  end_index = max(1, end_index)
  end_position = statement[end_index - 1].end
  imports = [
    (displayed_name, bound_name, end_position)
    for displayed_name, bound_name in imported_names
  ]
  excluded_names = {
    item.start
    for item in statement[:end_index]
    if item.type == tokenize.NAME
  }
  return imports, excluded_names


def analyze_imports(text):
  tokens = _tokenize_best_effort(text)
  imports = []
  excluded_names = set()

  for statement in _iter_statements(tokens):
    parsed = _parse_import_statement(statement)
    if parsed is None:
      continue
    statement_imports, statement_excluded_names = parsed
    imports.extend(statement_imports)
    excluded_names.update(statement_excluded_names)

  usages = {}
  for item in tokens:
    if item.type == tokenize.NAME and item.start not in excluded_names:
      usages.setdefault(item.string, []).append(item.start)

  imports.sort(key=lambda item: item[2])
  return imports, usages


if __name__ == "__main__":

  if sys.argv[1:] in (["--version"], ["-v"]):
    print(VERSION)
    sys.exit(0)

  for filename in sys.argv[1:]:
    if filename.startswith("-"):
      sys.stderr.write(f"Ignoring argument {filename}\n")
      continue

    if Path(filename).name == "__init__.py":
      continue

    try:
      with open(filename, 'r') as istr:
        text = istr.read()
    except FileNotFoundError:
      sys.stderr.write(f"Error reading {filename}\n")
      continue

    imports, usages = analyze_imports(text)

    imported_names_in_file = set()
    list_header = f"- {filename}:\n"
    for displayed_name, bound_name, end_position in imports:
      if displayed_name == "*":
        continue

      duplicate = displayed_name in imported_names_in_file
      imported_names_in_file.add(displayed_name)

      used_later = any(
        position > end_position
        for position in usages.get(bound_name, ())
      )

      if not used_later or duplicate:
        suffix = "  (duplicate)" if duplicate else ""
        print(f"{list_header}    {displayed_name}{suffix}")
        list_header = ""
