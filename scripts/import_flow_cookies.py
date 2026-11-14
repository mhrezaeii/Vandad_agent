"""Import a Cookie-Editor JSON export as the Flow worker's Google session.

Usage:
  1. In a browser where the COMPANY Google account is logged in, install the
     "Cookie-Editor" extension, open https://aistudio.google.com, and export
     cookies as JSON.
  2. Run: python scripts/import_flow_cookies.py path/to/cookies.json
  3. Verify: python scripts/import_flow_cookies.py --status
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.flow_generation_service import (  # noqa: E402
    FlowGenerationService,
    convert_cookie_export,
)


def main() -> int:
    settings = FlowSettings.from_config(get_settings())
    if "--status" in sys.argv:
        service = FlowGenerationService(settings)
        print(json.dumps(service.status(), indent=2))
        return 0
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    export_path = Path(sys.argv[1])
    export = json.loads(export_path.read_text(encoding="utf-8"))
    if isinstance(export, dict):
        export = export.get("cookies", [])
    state = convert_cookie_export(export, output_path=settings.storage_state)
    print(f"session written: {state} ({len(json.loads(state.read_text())['cookies'])} google cookies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
