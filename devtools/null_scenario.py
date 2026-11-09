"""Local testing only: copy a scenario with every effect and incident removed, to count false winners.

  python devtools/null_scenario.py scenarios/public_03_load.yaml state/null_load.yaml 31

In such a world no option is truly better for anyone, so every promoted winner or group rule is a false one.
Base caller-ID health (e.g. a pre-flagged D4) is kept: it is a property of the numbers, not an effect.
"""
import sys

import yaml

src, dst, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(src) as f:
    sc = yaml.safe_load(f)
sc["effects"] = []
sc["incidents"] = []
sc["seed"] = seed
sc["name"] = f"null_{seed}"
with open(dst, "w") as f:
    yaml.safe_dump(sc, f, sort_keys=False)
print("wrote", dst)
