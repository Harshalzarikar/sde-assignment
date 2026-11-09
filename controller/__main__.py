"""Command line.

  python -m controller run    --url http://127.0.0.1:8700 --db state/controller.db
  python -m controller status --db state/controller.db
  python -m controller health --db state/controller.db
  python -m controller supervisor --db state/controller.db [--workflow nw_renewal]
  python -m controller why --db state/controller.db --workflow nw_renewal --at "D3 14:00"
"""
import argparse

from .loop import run
from .report import print_health, print_status
from .views import print_supervisor, print_why


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="drive the simulator until the scenario finishes")
    r.add_argument("--url", required=True)
    r.add_argument("--db", default="state/controller.db")
    s = sub.add_parser("status", help="print what the notebook knows")
    s.add_argument("--db", default="state/controller.db")
    hp = sub.add_parser("health", help="the loop's own health per simulated day")
    hp.add_argument("--db", default="state/controller.db")
    sv = sub.add_parser("supervisor", help="one screen per workflow: live, testing, waiting on you, changed today")
    sv.add_argument("--db", default="state/controller.db")
    sv.add_argument("--workflow")
    wy = sub.add_parser("why", help="why did a workflow change at a time, who approved it, what happened next")
    wy.add_argument("--db", default="state/controller.db")
    wy.add_argument("--workflow", required=True)
    wy.add_argument("--at", required=True, help="'D3 14:00' or an ISO timestamp")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a.url, a.db)
    elif a.cmd == "health":
        print_health(a.db)
    elif a.cmd == "supervisor":
        print_supervisor(a.db, a.workflow)
    elif a.cmd == "why":
        print_why(a.db, a.workflow, a.at)
    else:
        print_status(a.db)


if __name__ == "__main__":
    main()
