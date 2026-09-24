#! /usr/bin/python

from pathlib import Path
import sys
import tokenize


VERSION = "0.0.2"


def _tokenize_stream(input_stream):
  def unindented_readline():
    return input_stream.readline().lstrip(" \t\f")

  try:
    yield from tokenize.generate_tokens(unindented_readline)
  except (IndentationError, tokenize.TokenError):
    # Analyze all tokens produced before an unrecoverable tokenization error.
    pass


def _iter_statements(tokens):
  current = []
  bracket_depth = 0
  ignored = {
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.INDENT,
    tokenize.DEDENT,
  }

  for token in tokens:
    if token.type in ignored:
      continue
    if token.type == tokenize.ENDMARKER:
      break
    if token.type in (tokenize.NEWLINE, tokenize.NL) or (
      token.type == tokenize.ERRORTOKEN and token.string in ("\n", "\r\n")):
      if bracket_depth == 0 and current:
        yield current
        current = []
      continue

    # Recover after an unmatched opening bracket when a later physical line
    # clearly starts another import statement.
    starts_import = (
      token.type == tokenize.NAME
      and token.string in ("from", "import")
      and token.line[:token.start[1]].strip() == ""
    )
    continues_from_import = (
      current
      and current[0].string == "from"
      and token.string == "import"
      and not any(
        token.string == "import" for token in current[1:]
      )
    )
    if current and starts_import and not continues_from_import:
      yield current
      current = []
      bracket_depth = 0

    if token.type == tokenize.OP:
      if token.string in "([{":
        bracket_depth += 1
      elif token.string in ")]}":
        bracket_depth = max(0, bracket_depth - 1)
      elif bracket_depth == 0 and token.string in (";", ":"):
        if current:
          yield current
          current = []
        continue

    current.append(token)

  if current:
    yield current


def _is_name(token, value=None):
  return (
    token.type == tokenize.NAME
    and (value is None or token.string == value)
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
        for index, token in enumerate(statement[1:], start=1)
        if _is_name(token, "import")
      ),
      None,
    )
    if import_index is None:
      return None

    module_name = "".join(
      token.string for token in statement[1:import_index]
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
    token.start
    for token in statement[:end_index]
    if token.type == tokenize.NAME
  }
  return imports, excluded_names


def parse_imports(input_stream):
  tokens = list(_tokenize_stream(input_stream))
  imports = []
  excluded_names = set()

  for statement in _iter_statements(tokens):
    if (parsed := _parse_import_statement(statement)) is not None:
      statement_imports, statement_excluded_names = parsed
      imports.extend(statement_imports)
      excluded_names.update(statement_excluded_names)

  usages = {}
  for token in tokens:
    if token.type == tokenize.NAME and token.start not in excluded_names:
      usages.setdefault(token.string, []).append(token.start)

  return imports, usages


def main():
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
        imports, usages = parse_imports(istr)

    except FileNotFoundError:
      sys.stderr.write(f"Error reading {filename}\n")
      continue

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


if __name__ == "__main__":
  main()
