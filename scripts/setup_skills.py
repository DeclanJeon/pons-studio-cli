"""Source-checkout launcher; installed distributions provide pons-skills."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.skill_setup import main

if __name__ == "__main__":
    raise SystemExit(main())
