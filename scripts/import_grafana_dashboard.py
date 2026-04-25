"""grafana/dashboards/*.json を Grafana API で一括アップロード.

環境変数:
  GRAFANA_URL    https://grafana.sogawa-yk.com または cluster internal
  GRAFANA_TOKEN  Service Account Token (dashboards:write が必要)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx


def main() -> int:
    url = os.environ["GRAFANA_URL"].rstrip("/")
    token = os.environ["GRAFANA_TOKEN"]
    folder_uid = os.environ.get("GRAFANA_FOLDER_UID", "")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    repo = Path(__file__).resolve().parents[1]
    dashboards_dir = repo / "grafana" / "dashboards"
    if not dashboards_dir.exists():
        print(f"no dashboards dir: {dashboards_dir}", file=sys.stderr)
        return 1

    with httpx.Client(timeout=30) as client:
        for j in sorted(dashboards_dir.glob("*.json")):
            dashboard = json.loads(j.read_text())
            payload = {
                "dashboard": dashboard,
                "overwrite": True,
                "message": f"Imported by {Path(__file__).name}",
            }
            if folder_uid:
                payload["folderUid"] = folder_uid
            r = client.post(f"{url}/api/dashboards/db", headers=headers, json=payload)
            if r.status_code >= 400:
                print(f"FAIL {j.name}: {r.status_code} {r.text}", file=sys.stderr)
                return 2
            out = r.json()
            print(f"OK {j.name} -> uid={out.get('uid')} url={out.get('url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
