#!/usr/bin/env python3
"""公開用の時刻表シャードに公式告知に基づく暫定パッチを適用する。"""

import argparse
import json
from pathlib import Path


def apply_patch(data_dir: Path, patch: dict) -> bool:
    # バケット数が変わった場合も、生成されたインデックスから対象を特定する。
    pref = f"pref_{patch['pref']:02d}"
    index_path = data_dir / "index" / f"{pref}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    pole_id = patch["pole_id"]
    meta = next((entry for entry in index["poles"] if entry["pole_id"] == pole_id), None)
    if meta is None:
        print(f"[SKIP] 停留所が現行GTFSにありません: {pole_id}")
        return False

    shard_path = data_dir / "timetables" / pref / f"{meta['b']}.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    pole = shard[pole_id]
    route_id = patch["route_id"]
    route = next((entry for entry in pole["routes"] if entry["route_id"] == route_id), None)
    if route is None or route.get("destination_stop_name") != patch["destination"]:
        print(f"[SKIP] 行先に一致する系統が現行GTFSにありません: {pole_id}/{route_id}")
        return False

    day = patch["day"]
    times = route["timetable"][day]
    before, after = patch["before"], patch["after"]
    if route.get("timetable_patch") is None and (not times or times[0] != before):
        print(f"[SKIP] 暫定変更の前提と異なるため現行GTFSを使用: {pole_id}/{route_id}")
        return False
    if not times or times[0] not in (before, after):
        raise ValueError(f"暫定変更済み時刻表の始発が不一致: {pole_id}/{route_id}")

    metadata = {"day": day, "before": before, "after": after}
    if before == times[0]:
        route["timetable"][day] = sorted(set([after, *times[1:]]))
    route.update(
        revision_pending=True,
        revised_date=patch["revised_date"],
        revised_url=patch["revised_url"],
        timetable_patch=metadata,
    )
    payload = json.dumps(shard, ensure_ascii=False, separators=(",", ":"))
    if payload == shard_path.read_text(encoding="utf-8"):
        return False
    pending_path = shard_path.with_suffix(".json.tmp")
    pending_path.write_text(payload, encoding="utf-8")
    pending_path.replace(shard_path)
    print(f"[PATCH] {pole_id}/{route_id}: {before} -> {after} ({patch['revised_date']})")
    return True


def main() -> None:
    site = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=site / "data")
    parser.add_argument("--patches", type=Path, default=site / "scripts" / "timetable_patches.json")
    args = parser.parse_args()
    patches = json.loads(args.patches.read_text(encoding="utf-8"))
    for patch in patches:
        apply_patch(args.data_dir, patch)


if __name__ == "__main__":
    main()
