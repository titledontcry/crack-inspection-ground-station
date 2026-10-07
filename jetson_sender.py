"""Watch a Jetson capture folder and upload each completed still to the ground station.

The camera program is intentionally separate: save JPEG/PNG photos into --source, and this
sender retries uploads without deleting the original. Add MAVLink telemetry fields in
metadata_for() once the exact camera and Pixhawk telemetry setup are decided.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def metadata_for(path: Path) -> dict:
    return {
        "captured_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        # Add latitude, longitude, distance_to_surface_m, gsd_mm_per_px here when available.
    }


def upload(endpoint: str, path: Path) -> None:
    with path.open("rb") as f:
        response = requests.post(
            f"{endpoint.rstrip('/')}/api/images",
            files={"file": (path.name, f, "image/jpeg")},
            data={"metadata": json.dumps(metadata_for(path))},
            timeout=(5, 90),
        )
    response.raise_for_status()
    print(f"uploaded {path.name}: {response.json()['image_id']}")


def is_stable(path: Path, delay: float = 1.0) -> bool:
    size = path.stat().st_size
    time.sleep(delay)
    return path.exists() and path.stat().st_size == size and size > 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True, help="e.g. http://192.168.1.10:8000")
    parser.add_argument("--source", type=Path, required=True, help="folder where camera saves still images")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    sent: set[Path] = set()
    while True:
        for path in sorted(args.source.glob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if path in sent or not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if not is_stable(path):
                continue
            try:
                upload(args.endpoint, path)
                sent.add(path)
            except requests.RequestException as exc:
                print(f"upload failed for {path.name}: {exc}; will retry")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
