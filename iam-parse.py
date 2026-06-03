#! /usr/bin/python

import csv
import sys
from dataclasses import dataclass
from itertools import product
from collections import defaultdict
import re

def read_rows(lines):
  delim = '│'
  lines = (line.strip().strip(delim) for line in lines)
  reader = csv.reader(lines, delimiter=delim)
  return (tuple(col.strip() for col in row) for row in reader if len(row) > 1)


@dataclass
class Block:
  sign: str
  effect: str
  principal: str
  resources: list[str]
  actions: list[str]

def resource_name_begins(name):
  return (name.startswith('arn:')
    or name.startswith('${')
    or name.startswith('{"Fn::'))


with open(sys.argv[1], 'r') as istr:
  blocks = []
  block = None

  for row in read_rows(istr):
    # check header
    if row[:2] == ('', 'Resource'):
      assert row == ('', 'Resource', 'Effect', 'Action', 'Principal', 'Condition'), f"bad header {row}"
      continue

    _sign, _res, _eff, _action, _princ, _cond = row

    assert (_sign, _eff, _princ == '', '', '') or (len(_sign) and len(_eff) and len(_princ))
    princ_name_begins = _eff != ''

    if princ_name_begins:
      if block:
        blocks.append(block)
      block = Block(sign=_sign, effect=_eff, principal=_princ, resources=[], actions=[])

    else:
      assert block is not None, f"bad start of block {row}"
      block.principal += _princ

    if _sign != '' or resource_name_begins(_res):
      block.resources.append(_res)
    else:
      assert len(block.resources), f"bad resource name {row}"
      block.resources[-1] += _res

    if re.match(r'[a-z0-9\-]+:[a-zA-Z]', _action):
      block.actions.append(_action)
    else:
      assert len(block.actions), f"bad action name {row}"
      block.actions[-1] += _action

  if block:
    blocks.append(block)


diffs = defaultdict(list)

for block in blocks:
  key = block.sign, block.principal, block.effect
  diffs[key].extend(product(block.actions, block.resources))

for key, changes in tuple(diffs.items()):
  diffs[key] = set(changes)

keys = sorted(diffs, key=lambda k: (*k[1:], k[0]))
for key in keys:
  print(" ".join(key))
  changes = diffs[key]

  sign, *key = key
  key = {'+': '-', '-': '+'}[sign], *key

  change_diff = changes.difference(set(diffs[key]))
  if len(change_diff) == 0:
    print("    nothing")
  for cd in sorted(change_diff, key=lambda v: (*v[1:], v[0])):
    print("    " + "\t".join(cd))
  print()
