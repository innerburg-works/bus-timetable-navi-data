#!/usr/bin/env python3
"""
公開用 national-data-site/data/ 全体に対する非公開データ混入・整合性監査スクリプト。

検査対象:
  - manifest.json
  - index/pref_*.json
  - timetables/pref_*/*.json
  - routes/pref_*.json
  - spots/pref_*.json および spots/spots_manifest.json
  - i18n/pref_*.json (JSON形式・破損検査)

使い方:
  python3 national-data-site/scripts/audit_public_data.py --data-dir national-data-site/data
"""

import argparse
import json
import sys
import zlib
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extra_feeds_registry import NON_PUBLIC_POLE_PREFIXES


def _is_non_public_id(val: object) -> bool:
    if not val:
        return False
    return str(val).startswith(NON_PUBLIC_POLE_PREFIXES)


def _load_json_z(path: Path) -> object:
    raw = zlib.decompress(path.read_bytes(), -zlib.MAX_WBITS)
    return json.loads(raw.decode("utf-8"))


def _load_bundled_stop_signatures(bundled_dir: Optional[Path]) -> Dict[int, Set[Tuple[str, float, float]]]:
    """同梱専用(bundled_odpt)の停留所(名称, lat丸め, lng丸め)集合を読み込み、ID剥ぎ取り混入を検知する。"""
    sigs: Dict[int, Set[Tuple[str, float, float]]] = {}
    if not bundled_dir or not (bundled_dir / "index").exists():
        return sigs
    for fp in sorted((bundled_dir / "index").glob("pref_*.json.z")):
        try:
            d = _load_json_z(fp)
            pref = int(d.get("pref") or 0)
            s: Set[Tuple[str, float, float]] = set()
            for p in d.get("poles", []):
                name = str(p.get("stop_name") or "").strip()
                lat = round(float(p.get("lat") or 0), 4)
                lng = round(float(p.get("lng") or 0), 4)
                if name:
                    s.add((name, lat, lng))
            sigs[pref] = s
        except Exception:
            continue
    return sigs


def audit_public_data(data_dir: Path, bundled_dir: Optional[Path] = None, *, generated_only: bool = False) -> Tuple[bool, Dict]:
    issues: List[str] = []
    stats = {
        "index_poles": 0,
        "index_leaked": 0,
        "timetables_poles": 0,
        "timetables_leaked": 0,
        "routes_total": 0,
        "routes_leaked": 0,
        "spots_prefs": 0,
        "spots_total": 0,
        "spots_patterns": 0,
        "spots_leaked_prefs": 0,
        "spots_leaked_patterns": 0,
        "spots_leaked_stop_rows": 0,
        "spots_leaked_unique_ids": 0,
        "spots_leaked_featured": 0,
        "spots_orphan_stop_rows": 0,
        "spots_bundled_sig_only_patterns": 0,
        "spots_index_errors": 0,
        "manifest_leaked_operators": 0,
        "i18n_files": 0,
        "i18n_errors": 0,
    }

    public_poles_by_pref: Dict[int, Set[str]] = {}
    public_sigs_by_pref: Dict[int, Set[Tuple[str, float, float]]] = {}
    bundled_sigs_by_pref = _load_bundled_stop_signatures(bundled_dir)

    # 1. index/pref_*.json
    index_dir = data_dir / "index"
    if index_dir.exists():
        for fp in sorted(index_dir.glob("pref_*.json")):
            d = json.loads(fp.read_text(encoding="utf-8"))
            pref = int(d.get("pref") or 0)
            pids: Set[str] = set()
            sigs: Set[Tuple[str, float, float]] = set()
            for p in d.get("poles", []):
                stats["index_poles"] += 1
                pid = str(p.get("pole_id") or "")
                if _is_non_public_id(pid):
                    stats["index_leaked"] += 1
                    if stats["index_leaked"] <= 10:
                        issues.append(f"index/{fp.name}: non-public pole_id {pid}")
                else:
                    pids.add(pid)
                    name = str(p.get("stop_name") or "").strip()
                    if name:
                        sigs.add((name, round(float(p.get("lat") or 0), 4), round(float(p.get("lng") or 0), 4)))
            public_poles_by_pref[pref] = pids
            public_sigs_by_pref[pref] = sigs

    # 2. timetables/pref_*/*.json
    tt_dir = data_dir / "timetables"
    if tt_dir.exists():
        for pref_dir in sorted(tt_dir.glob("pref_*")):
            if not pref_dir.is_dir():
                continue
            for fp in sorted(pref_dir.glob("*.json")):
                d = json.loads(fp.read_text(encoding="utf-8"))
                poles_obj = d.get("poles") if isinstance(d, dict) and "poles" in d else d
                if not isinstance(poles_obj, dict):
                    continue
                for pid, pole in poles_obj.items():
                    stats["timetables_poles"] += 1
                    leaked_here = _is_non_public_id(pid)
                    if not leaked_here and isinstance(pole, dict):
                        for r in pole.get("routes") or []:
                            if (
                                _is_non_public_id(r.get("route_id"))
                                or _is_non_public_id(r.get("prev_pole_id"))
                                or _is_non_public_id(r.get("next_pole_id"))
                            ):
                                leaked_here = True
                                break
                    if leaked_here:
                        stats["timetables_leaked"] += 1
                        if stats["timetables_leaked"] <= 10:
                            issues.append(f"timetables/{pref_dir.name}/{fp.name}: non-public entry {pid}")

    # 3. routes/pref_*.json
    routes_dir = data_dir / "routes"
    if routes_dir.exists():
        for fp in sorted(routes_dir.glob("pref_*.json")):
            d = json.loads(fp.read_text(encoding="utf-8"))
            for r in d.get("routes", []):
                stats["routes_total"] += 1
                rid = str(r.get("id") or r.get("route_id") or "")
                if _is_non_public_id(rid):
                    stats["routes_leaked"] += 1
                    if stats["routes_leaked"] <= 10:
                        issues.append(f"routes/{fp.name}: non-public route {rid}")

    # 4. spots/pref_*.json
    spots_dir = data_dir / "spots"
    leaked_unique_ids: Set[str] = set()
    if spots_dir.exists():
        for fp in sorted(spots_dir.glob("pref_*.json")):
            d = json.loads(fp.read_text(encoding="utf-8"))
            pref = int(d.get("pref") or 0)
            stats["spots_prefs"] += 1
            patterns = d.get("patterns") or []
            spots = d.get("spots") or []
            stats["spots_total"] += len(spots)
            stats["spots_patterns"] += len(patterns)
            pub_pids = public_poles_by_pref.get(pref, set())
            pub_sigs = public_sigs_by_pref.get(pref, set())
            bun_sigs = bundled_sigs_by_pref.get(pref, set())

            pref_had_leak = False
            for p_idx, pat in enumerate(patterns):
                pts = pat.get("s") or []
                pat_leaked = False
                bun_only_hits = 0
                for pt in pts:
                    if not isinstance(pt, list) or len(pt) < 4:
                        stats["spots_index_errors"] += 1
                        issues.append(f"spots/{fp.name}: malformed stop row in pattern[{p_idx}]")
                        continue
                    pid = str(pt[0] or "")
                    erid = str(pt[4] or "") if len(pt) > 4 else ""
                    if _is_non_public_id(pid) or _is_non_public_id(erid):
                        pat_leaked = True
                        stats["spots_leaked_stop_rows"] += 1
                        if _is_non_public_id(pid):
                            leaked_unique_ids.add(pid)
                    else:
                        if not pid:
                            stats["spots_orphan_stop_rows"] += 1
                        elif erid and pub_pids and pid not in pub_pids:
                            stats["spots_orphan_stop_rows"] += 1
                            if stats["spots_orphan_stop_rows"] <= 10:
                                issues.append(
                                    f"spots/{fp.name}: pattern[{p_idx}] boarding pole {pid} not found in public index"
                                )
                        # ID剥ぎ取り混入検知: 公開側に存在しないのに同梱側(名称+座標)に一致する停留所
                        sig = (str(pt[1] or "").strip(), round(float(pt[2] or 0), 4), round(float(pt[3] or 0), 4))
                        if bun_sigs and sig in bun_sigs and sig not in pub_sigs:
                            bun_only_hits += 1
                if pat_leaked:
                    pref_had_leak = True
                    stats["spots_leaked_patterns"] += 1
                elif pts and bun_only_hits >= max(2, len(pts) // 2):
                    stats["spots_bundled_sig_only_patterns"] += 1
                    if stats["spots_bundled_sig_only_patterns"] <= 10:
                        issues.append(
                            f"spots/{fp.name}: pattern[{p_idx}] ({pat.get('r')}) matches bundled-only stop signatures ({bun_only_hits}/{len(pts)})"
                        )

            for sp in spots:
                sid = sp.get("id", "?")
                for al in sp.get("alights") or []:
                    if not isinstance(al, list) or len(al) < 4:
                        stats["spots_index_errors"] += 1
                        issues.append(f"spots/{fp.name}: spot {sid} malformed alight {al}")
                        continue
                    pat_idx, al_idx, al_name = al[0], al[1], str(al[2] or "")
                    if not (isinstance(pat_idx, int) and 0 <= pat_idx < len(patterns)):
                        stats["spots_index_errors"] += 1
                        issues.append(f"spots/{fp.name}: spot {sid} alight pat_idx out of range ({pat_idx}/{len(patterns)})")
                        continue
                    pts = patterns[pat_idx].get("s") or []
                    if not (isinstance(al_idx, int) and 0 <= al_idx < len(pts)):
                        stats["spots_index_errors"] += 1
                        issues.append(f"spots/{fp.name}: spot {sid} alight al_idx out of range ({al_idx}/{len(pts)})")
                        continue
                    actual_name = str(pts[al_idx][1] or "")
                    if al_name and actual_name != al_name:
                        stats["spots_index_errors"] += 1
                        issues.append(f"spots/{fp.name}: spot {sid} alight name mismatch ({al_name} != {actual_name})")

                for f in sp.get("featured") or []:
                    fid = str(f.get("from_id") or "")
                    frid = str(f.get("route_id") or "")
                    if _is_non_public_id(fid) or _is_non_public_id(frid):
                        pref_had_leak = True
                        stats["spots_leaked_featured"] += 1
                    else:
                        if pub_pids and fid and fid not in pub_pids:
                            stats["spots_index_errors"] += 1
                            if stats["spots_index_errors"] <= 10:
                                issues.append(f"spots/{fp.name}: spot {sid} featured from_id {fid} missing in public index")
                        # featured が patterns 内の実在乗車→降車に紐づくか確認
                        to_name = str(f.get("to_name") or "").strip()
                        matched_pat = False
                        for pat in patterns:
                            if f.get("rid") and pat.get("rid") and pat["rid"] != f["rid"]:
                                continue
                            if f.get("r") and pat.get("r") and pat["r"] != f["r"]:
                                continue
                            pts = pat.get("s") or []
                            b_idx = next(
                                (i for i, st in enumerate(pts) if st and str(st[0]) == fid and (not frid or not st[4] or str(st[4]) == frid)),
                                -1,
                            )
                            if b_idx >= 0 and any(
                                (not to_name or str(pts[ai][1] or "").strip() == to_name)
                                for ai in range(b_idx + 1, len(pts))
                            ):
                                matched_pat = True
                                break
                        if not matched_pat:
                            stats["spots_index_errors"] += 1
                            if stats["spots_index_errors"] <= 10:
                                issues.append(f"spots/{fp.name}: spot {sid} featured {fid}->{to_name} has no matching pattern")

            if pref_had_leak:
                stats["spots_leaked_prefs"] += 1
                issues.append(f"spots/{fp.name}: contains non-public ODPT basic-license data")

    stats["spots_leaked_unique_ids"] = len(leaked_unique_ids)

    # 5. manifest.json
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        for p in m.get("prefs", []):
            for op in p.get("operators", []):
                lic = str(op.get("license") or "")
                oid = str(op.get("id") or "")
                if "基本ライセンス" in lic or _is_non_public_id(f"odpt_{oid}_"):
                    stats["manifest_leaked_operators"] += 1
                    issues.append(f"manifest.json: pref_{p.get('pref')} operator {op.get('name')} has non-public license/id")

    # 6. i18n/pref_*.json (JSON破損・公開インデックス未存在県への非公開i18n混入検査)
    i18n_dir = data_dir / "i18n"
    if i18n_dir.exists():
        for fp in sorted(i18n_dir.glob("pref_*.json")):
            stats["i18n_files"] += 1
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    stats["i18n_errors"] += 1
                    issues.append(f"i18n/{fp.name}: root is not JSON object")
                    continue
            except Exception as e:
                stats["i18n_errors"] += 1
                issues.append(f"i18n/{fp.name}: invalid JSON ({e})")
                continue
            try:
                pref_num = int(fp.stem.split("_")[1])
            except Exception:
                pref_num = 0
            if pref_num and public_poles_by_pref and pref_num not in public_poles_by_pref:
                stats["i18n_errors"] += 1
                issues.append(f"i18n/{fp.name}: exists for non-public/bundled-only pref_{pref_num:02d}")

    expected_i18n = {f"pref_{pref:02d}.json" for pref in public_poles_by_pref}
    for name in sorted(expected_i18n - {p.name for p in i18n_dir.glob("pref_*.json")}):
        stats["i18n_errors"] += 1
        issues.append(f"i18n/{name}: missing dictionary")

    for required in ("manifest.json", "index", "timetables", "routes", "i18n"):
        if not (data_dir / required).exists():
            issues.append(f"{required}: missing public data")
    if not public_poles_by_pref:
        issues.append("index: no prefectures")
    if not generated_only:
        spot_manifest = spots_dir / "spots_manifest.json"
        if not spot_manifest.is_file():
            issues.append("spots/spots_manifest.json: missing")
        else:
            manifest = json.loads(spot_manifest.read_text(encoding="utf-8"))
            expected = {entry["file"] for entry in manifest["prefectures"]}
            actual = {p.name for p in spots_dir.glob("pref_*.json")}
            if not expected or expected != actual or manifest["total_spots"] != stats["spots_total"]:
                issues.append("spots: manifest files or total disagree with published data")

    ok = (
        not issues
        and stats["index_leaked"] == 0
        and stats["timetables_leaked"] == 0
        and stats["routes_leaked"] == 0
        and stats["spots_leaked_patterns"] == 0
        and stats["spots_leaked_stop_rows"] == 0
        and stats["spots_leaked_featured"] == 0
        and stats["spots_orphan_stop_rows"] == 0
        and stats["spots_bundled_sig_only_patterns"] == 0
        and stats["spots_index_errors"] == 0
        and stats["manifest_leaked_operators"] == 0
        and stats["i18n_errors"] == 0
    )
    return ok, {"ok": ok, "stats": stats, "issues": issues[:50], "total_issues": len(issues)}


def main() -> None:
    root = SCRIPT_DIR.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=root / "national-data-site" / "data")
    parser.add_argument(
        "--bundled-dir",
        type=Path,
        default=root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "bundled_odpt",
    )
    parser.add_argument("--generated-only", action="store_true", help="日次生成直後の時刻表等を検査（固定のスポット目録は公開直前に検査）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    bundled = args.bundled_dir if args.bundled_dir and args.bundled_dir.exists() else None
    ok, result = audit_public_data(args.data_dir, bundled, generated_only=args.generated_only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PUBLIC_DATA_AUDIT_PASS" if ok else "PUBLIC_DATA_AUDIT_FAIL"
        print(status)
        print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
        if result["issues"]:
            print(f"Issues ({result['total_issues']} total, showing up to 50):")
            for line in result["issues"]:
                print("  -", line)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
