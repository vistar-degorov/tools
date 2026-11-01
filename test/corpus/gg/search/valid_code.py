import os

def target():
  return os.getcwd()

def target_extra():
  return None

VALUE = 1
route = "route/123"
AlphaMarker = object()

class Target:
  pass

def target():
  return "second"

BetaMarker = object()

import decimal

from package import (
  ImportedMarker)

ImportedMarker()
