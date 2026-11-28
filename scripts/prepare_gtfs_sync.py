#!/usr/bin/env python3
"""日次ビルドの入力を取得し、直近に公開したGTFSとの差を確認する。"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from typing import Optional

from build_national_shards import (
    API_URL,
    TOEI_URL,
    catalog_items,
    download_zip,
    fetch_json,
    is_allowed_public_catalog_license,
    operator_label,
)
from extra_feeds_registry import EXTRA_PUBLIC_FEEDS
from extra_feeds_sync import acquire_feed_zip, today_jst_ymd


def read_published_slots(data_dir: Path) -> dict:
    """初回のみ、配信済みシャードから事業者名と使用済みIDを復元する。"""
    published = {}
    for index_path in sorted((data_dir / "index").glob("pref_*.json")):
        index = json.loads(index_path.read_text(encoding="utf-8"))
        by_bucket = defaultdict(dict)
        seen = set()
        for entry in index["poles"]:
            match = re.match(r"^nat_p\d{2}_(\d{3})_", entry["pole_id"])
            if match and int(match[1]) not in seen:
                slot = int(match[1])
                seen.add(slot)
                by_bucket[entry["b"]][slot] = entry["pole_id"]
        by_name = defaultdict(list)
        for bucket, entries in by_bucket.items():
            path = data_dir / "timetables" / index_path.stem / f"{bucket}.json"
            poles = json.loads(path.read_text(encoding="utf-8"))
            for slot, pole_id in entries.items():
                by_name[poles[pole_id]["operator_name"]].append(slot)
        published[str(index["pref"])] = dict(by_name)
    return published


def assign_feed_slots(items: list, previous: dict, data_dir: Path) -> dict:
    slots = previous.get("feed_slots", {})
    if "feed_slots" in previous:
        published = {}
    else:
        published = read_published_slots(data_dir)

    def label(item):
        return operator_label((item.get("organization_name") or "").strip(),
                              (item.get("feed_name") or "").strip())

    labels = Counter((str(item.get("feed_pref_id")), label(item)) for item in items)
    for pref, by_name in published.items():
        slots[pref] = {"reserved": sorted(slot for values in by_name.values() for slot in values),
                       "feeds": {}}

    for item in items:
        pref = str(item.get("feed_pref_id"))
        if not pref.isdecimal() or not 1 <= int(pref) <= 47:
            continue
        group = slots.setdefault(pref, {"reserved": [], "feeds": {}})
        key = f"{item['organization_id']}/{item['feed_id']}"
        if key not in group["feeds"]:
            name = label(item)
            matches = published.get(pref, {}).get(name, [])
            if len(matches) == 1 and labels[pref, name] == 1:
                slot = matches[0]
            else:
                used = set(group["reserved"]) | set(group["feeds"].values())
                slot = next(candidate for candidate in range(1000) if candidate not in used)
            group["feeds"][key] = slot
        item["_slot"] = group["feeds"][key]
    return slots


def check_extra_public_feeds(raw_dir: Path, ref_ymd: Optional[str] = None) -> tuple[dict, list]:
    """全国カタログ外の公開追加フィード（鳥取・広島・宮崎・福井・ODPT公開等）の更新を検査する。"""
    extra_dir = raw_dir / "extra_public"
    extra_dir.mkdir(parents=True, exist_ok=True)
    versions: dict = {}
    details: list = []
    ref = ref_ymd or today_jst_ymd()
    for feed in EXTRA_PUBLIC_FEEDS:
        fid = feed["id"]
        zp, matched_url, val, err = acquire_feed_zip(feed, raw_dir=extra_dir, api_key="", ref_ymd=ref)
        if zp and zp.exists():
            digest = hashlib.sha256(zp.read_bytes()).hexdigest()
            sig = f"{digest[:16]}:{val.get('valid_from', '')}-{val.get('valid_to', '')}"
            versions[fid] = sig
            details.append({
                "id": fid, "pref": feed["pref"], "label": feed["label"],
                "status": "ok", "valid_from": val.get("valid_from", ""), "valid_to": val.get("valid_to", ""),
                "url": matched_url, "sig": sig,
            })
        else:
            details.append({
                "id": fid, "pref": feed["pref"], "label": feed["label"],
                "status": "error", "error": err,
            })
    return versions, details


def write_summary(summary_path: Path, previous: dict, state: dict, catalog: list, extra_details: Optional[list] = None) -> None:
    old = previous.get("feed_versions", {})
    new = state["feed_versions"]
    labels = {f"{item['organization_id']}/{item['feed_id']}": item["organization_name"]
              for item in catalog}
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    updated = sorted(key for key in old.keys() & new.keys() if old[key] != new[key])
    toei = "更新あり" if previous.get("toei_sha256") != state["toei_sha256"] else "更新なし"
    old_extra = previous.get("extra_feed_versions", {})
    new_extra = state.get("extra_feed_versions", {})
    extra_changed = sorted(k for k in new_extra.keys() | old_extra.keys() if old_extra.get(k) != new_extra.get(k))
    with summary_path.open("a", encoding="utf-8") as summary:
        summary.write("### GTFS取得元の更新確認\n\n")
        summary.write(f"- ODPT 都営バスのZIP: {toei}\n")
        summary.write(f"- 追加公開フィード（鳥取・広島・宮崎・福井・ODPT公開等）: 取得成功 {len(new_extra)} / 変更 {len(extra_changed)}\n")
        if extra_details:
            errs = [d for d in extra_details if d["status"] != "ok"]
            if errs:
                summary.write(f"  - 取得失敗（前回有効版フォールバック対象）: {len(errs)} 件\n")
                for e in errs[:10]:
                    summary.write(f"    - {escape(e['label'])}: {escape(str(e.get('error', '')))}\n")
        if "feed_versions" not in previous:
            summary.write(f"- GTFSデータリポジトリ: {len(new)} フィードを初回記録\n")
            return
        summary.write(f"- GTFSデータリポジトリ: 追加 {len(added)} / 更新 {len(updated)} / 一覧から消失 {len(removed)}\n")
        for heading, keys in (("追加", added), ("更新", updated), ("一覧から消失", removed)):
            for key in keys[:30]:
                name = " ".join(str(labels.get(key) or "").split())
                summary.write(f"  - {heading}: {escape(name)} <code>{escape(key)}</code>\n")
            if len(keys) > 30:
                summary.write(f"  - {heading}: 残り {len(keys) - 30} 件\n")


def prepare(raw_dir: Path, state_path: Path, data_dir: Path, force: bool,
            summary_path: Optional[Path] = None) -> bool:
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_items = catalog_items(fetch_json(API_URL))
    # 公開再配布可能なライセンスを確認できたフィードのみ取り込む（確認不能な新規・変更データの公開を防止）
    items = []
    blocked_unverified = []
    for it in raw_items:
        ok, lic, lic_url = is_allowed_public_catalog_license(it)
        if ok:
            items.append(it)
        else:
            blocked_unverified.append(
                f"{it.get('organization_id')}/{it.get('feed_id')} (license={lic!r}, url={lic_url!r})"
            )
    if blocked_unverified:
        print(f"[LICENSE-BLOCK] Excluded {len(blocked_unverified)} unverified-license feeds from sync:")
        for b in blocked_unverified[:20]:
            print(f"  - {b}")
    # UIDは /files のファイル版を識別する。一覧の並べ替えだけでは更新しない。
    items = sorted(items, key=lambda item: (
        str(item.get("feed_pref_id")), item["organization_id"], item["feed_id"]
    ))
    fields = (
        "organization_id", "organization_name", "organization_web_url", "feed_id",
        "feed_name", "feed_pref_id", "feed_page_url", "feed_version", "file_uid",
        "file_url", "file_from_date", "file_to_date", "file_last_updated_at",
        "feed_license_id", "feed_license_url",
    )
    catalog = [{field: item.get(field) for field in fields} for item in items]
    catalog_hash = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
    toei_path = raw_dir / "ToeiBus-GTFS.zip"
    toei_path.unlink(missing_ok=True)
    if not download_zip(TOEI_URL, toei_path):
        raise RuntimeError("ODPTの都営バスGTFSを取得できませんでした")
    toei_hash = hashlib.sha256(toei_path.read_bytes()).hexdigest()
    patches_hash = hashlib.sha256(
        Path(__file__).with_name("timetable_patches.json").read_bytes()
    ).hexdigest()
    extra_versions, extra_details = check_extra_public_feeds(raw_dir)
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    # 取得失敗した追加フィードは前回ハッシュを引き継ぎ、単発通信エラーによる無限再構築を防止
    merged_extra_versions = dict(previous.get("extra_feed_versions", {}))
    merged_extra_versions.update(extra_versions)
    extra_hash = hashlib.sha256(
        json.dumps(merged_extra_versions, sort_keys=True).encode()
    ).hexdigest()
    state = {
        "catalog_sha256": catalog_hash,
        "toei_sha256": toei_hash,
        "patches_sha256": patches_hash,
        "extra_feeds_sha256": extra_hash,
    }
    if not force and {"feed_slots", "feed_versions", "extra_feed_versions"} <= previous.keys() and previous.items() >= state.items():
        if summary_path:
            write_summary(summary_path, previous, previous, catalog, extra_details)
        return False
    state["feed_versions"] = {
        f"{item['organization_id']}/{item['feed_id']}": hashlib.sha256(
            json.dumps(item, sort_keys=True).encode()
        ).hexdigest()
        for item in catalog
    }
    state["extra_feed_versions"] = merged_extra_versions
    if summary_path:
        write_summary(summary_path, previous, state, catalog, extra_details)
    state["feed_slots"] = assign_feed_slots(items, previous, data_dir)
    (raw_dir / "catalog.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    (raw_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--published-data-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    changed = prepare(args.raw_dir, args.state, args.published_data_dir, args.force, args.summary)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"changed={str(changed).lower()}\n")
    print("GTFSまたは時刻表パッチに更新あり" if changed else "GTFSと時刻表パッチに更新なし")


if __name__ == "__main__":
    main()
