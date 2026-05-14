import sys
import os
from operator import itemgetter
from collections import defaultdict
from itertools import groupby, repeat, chain
import hashlib
from shlex import quote

def ls_files(roots):
  for root in roots:
    for dirpath, dirnames, filenames in os.walk(root):
      yield from (os.path.join(dirpath, fname) for fname in filenames)

def sha256(filename):
  # calc = hashlib.sha256()
  calc = hashlib.sha1()
  with open(filename, 'rb') as istr:
    calc.update(istr.read())
  return calc.hexdigest()

def repeat_with_blanks(word):
  return chain([word], repeat(" " * len(word)))


def main():
  counting = sys.argv[1] == "--count"
  if counting:
    roots = sys.argv[2:]
  else:
    roots = sys.argv[1:]

  files = list((os.path.basename(fpath), os.path.dirname(fpath)) for fpath in ls_files(roots))
  if counting:
    print(len(files))
    sys.exit(0)

  files.sort()

  # for filename, dirpath in files:
  #   print(f"{filename} {dirpath}")
  # exit(0)

  dups = []
  dups_by_dir = defaultdict(list)
  file_counts_by_dir = defaultdict(lambda: 0)

  for filename, group in groupby(files, key=itemgetter(0)):
    group = tuple(group)
    dirpaths = tuple(dirpath for _, dirpath in group)
    fullpaths = tuple(os.path.join(dirpath, filename) for filename, dirpath in group)

    for dirpath in dirpaths:
      file_counts_by_dir[dirpath] += 1

    if len(group) < 2:
      continue

    hashes = tuple(sha256(filepath) for filepath in fullpaths)

    same = all((hashes[0] == h for h in hashes))
    if same:
      keep = tuple(fp for fp in fullpaths if "think" in fp.lower())
      if len(keep) == 0:
        dups.extend(fullpaths[1:])
      else:
        dups.extend((fp for fp in fullpaths if fp not in keep))

    else:
      print(f"different files same name: {filename}")
      sys.exit(0)

    dup_key = tuple(sorted(dirpaths))
    dups_by_dir[dup_key].append(filename)

    for print_fname, filehash, dirpath in zip(repeat_with_blanks(filename), hashes, dirpaths):
      print(f"{print_fname} {filehash} {dirpath}")


  print("-------- DUPS: ------------------------")
  for dirpaths, filenames in dups_by_dir.items():
    for dirpath in dirpaths:
      total_count = file_counts_by_dir[dirpath]
      dups_count = len(filenames)
      print(f"{quote(dirpath)} ({dups_count}/{total_count})")

    for filename in filenames:
      print(f"    {quote(filename)}")

  # for dup in dups:
  #   print(dup)

if __name__ == "__main__":
  main()
