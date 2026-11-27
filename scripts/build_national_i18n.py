#!/usr/bin/env python3
"""
全国40都道府県（national_gtfs/*.zip ＋ 都営バス）の translations.txt から
日本語バス停名・行き先名 -> 公式英語名（なければ ja-Hrkt ひらがなからヘボン式ローマ字自動変換）
の軽量辞書 data/i18n/pref_XX.json を一括生成するスクリプト。
"""

import argparse
import concurrent.futures
import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


# ゼロ依存・ひらがな/カタカナ -> ヘボン式ローマ字変換
KANA_DIGRAPHS = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo",
    "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho",
    "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo",
    "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo",
    "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "じゃ": "ja", "じゅ": "ju", "じょ": "jo",
    "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo",
    "てぃ": "ti", "でぃ": "di", "うぃ": "wi", "うぇ": "we",
    "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
}

KANA_SINGLES = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ー": "", "　": " ", "・": " ", "（": " (", "）": ")",
}


def kata_to_hira(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def kana_to_romaji(text: str) -> str:
    hira = kata_to_hira(text.strip())
    res: List[str] = []
    i = 0
    n = len(hira)
    while i < n:
        ch = hira[i]
        if ch == "っ" and i + 1 < n:
            nxt = KANA_DIGRAPHS.get(hira[i + 1:i + 3]) or KANA_SINGLES.get(hira[i + 1], "")
            if nxt and nxt[0].isalpha():
                res.append("t" if nxt.startswith("ch") else nxt[0])
            i += 1
            continue
        if i + 1 < n and hira[i:i + 2] in KANA_DIGRAPHS:
            res.append(KANA_DIGRAPHS[hira[i:i + 2]])
            i += 2
            continue
        if ch in KANA_SINGLES:
            res.append(KANA_SINGLES[ch])
        else:
            res.append(ch)
        i += 1
    raw = re.sub(r"\s+", " ", "".join(res)).strip()
    # 先頭・スペース後を大文字化
    return " ".join(w[:1].upper() + w[1:] if w else "" for w in raw.split(" "))


def extract_i18n_from_zip(zip_path: Path) -> Dict[str, str]:
    if not zip_path.exists():
        return {}
    mapping: Dict[str, str] = {}
    hira_fallback: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            if "translations.txt" not in names:
                return {}

            # stop_id -> ja stop_name の逆引き（record_id 形式の translations.txt 用）
            stop_id_to_ja: Dict[str, str] = {}
            if "stops.txt" in names:
                for st in csv.DictReader(io.StringIO(zf.read("stops.txt").decode("utf-8-sig", errors="replace"))):
                    sid = (st.get("stop_id") or "").strip()
                    snm = (st.get("stop_name") or "").strip()
                    if sid and snm:
                        stop_id_to_ja[sid] = snm

            tr_rows = csv.DictReader(io.StringIO(zf.read("translations.txt").decode("utf-8-sig", errors="replace")))
            for r in tr_rows:
                lang = (r.get("language") or r.get("lang") or "").strip().lower()
                val = (r.get("translation") or "").strip()
                if not val:
                    continue
                ja_key = (r.get("trans_id") or r.get("field_value") or "").strip()
                if not ja_key:
                    rec_id = (r.get("record_id") or "").strip()
                    tname = (r.get("table_name") or "").strip()
                    if tname == "stops" and rec_id in stop_id_to_ja:
                        ja_key = stop_id_to_ja[rec_id]
                if not ja_key or ja_key == val:
                    continue

                if lang in ("en", "en-us", "en-gb"):
                    # 公式英語名を最優先
                    if ja_key not in mapping:
                        mapping[ja_key] = val
                elif lang in ("ja-hrkt", "ja-hira", "hrkt"):
                    if ja_key not in hira_fallback:
                        hira_fallback[ja_key] = val
    except Exception:
        return {}

    # 英語訳がない日本語名だけ、ひらがな読みからヘボン式ローマ字を生成
    for ja_key, hira_val in hira_fallback.items():
        if ja_key not in mapping:
            rom = kana_to_romaji(hira_val)
            if rom and any(c.isascii() and c.isalpha() for c in rom):
                mapping[ja_key] = rom

    return mapping


def _process_one(task: Tuple[int, Path]) -> Tuple[int, Dict[str, str]]:
    pref, zp = task
    return (pref, extract_i18n_from_zip(zp))


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="全国47都道府県 バス停・行き先英語/ローマ字辞書生成")
    parser.add_argument("--raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "national_gtfs")
    parser.add_argument("--odpt-raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "odpt_and_rt")
    parser.add_argument("--toei-zip", type=Path, default=root / "data-pipeline" / "raw" / "ToeiBus-GTFS.zip")
    parser.add_argument("--out-i18n-dir", type=Path, default=root / "national-data-site" / "data" / "i18n")
    parser.add_argument("--bundle-tokyo-out", type=Path, default=root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "i18n" / "pref_13.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    args.out_i18n_dir.mkdir(parents=True, exist_ok=True)
    tasks: List[Tuple[int, Path]] = []
    for zp in sorted(args.raw_dir.glob("p*_*.zip")):
        m = re.match(r"^p(\d{2})_", zp.name)
        if m:
            tasks.append((int(m.group(1)), zp))

    # ODPT 追加取得済み ZIP (odpt_*.zip / odpt_pub_*.zip) も都道府県別に辞書抽出対象へ追加
    if args.odpt_raw_dir and args.odpt_raw_dir.exists():
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from merge_odpt_and_rt import ODPT_PUBLIC_STATIC_TARGETS, ODPT_STATIC_TARGETS
            pref_by_odpt_id = {t["id"]: t["pref"] for t in ODPT_PUBLIC_STATIC_TARGETS + ODPT_STATIC_TARGETS}
            for zp in sorted(args.odpt_raw_dir.glob("odpt_*.zip")):
                stem = zp.stem
                oid = stem[len("odpt_pub_") :] if stem.startswith("odpt_pub_") else stem[len("odpt_") :]
                if oid in pref_by_odpt_id:
                    tasks.append((pref_by_odpt_id[oid], zp))
        except Exception as e:
            print(f"[WARN] ODPT i18n target scan skipped: {e}")

    by_pref: Dict[int, Dict[str, str]] = defaultdict(dict)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for pref, mp in ex.map(_process_one, tasks):
            for k, v in mp.items():
                if k not in by_pref[pref]:
                    by_pref[pref][k] = v

    # 東京都(13)に都営バスの公式英語訳を最優先マージ
    if args.toei_zip.exists():
        toei_map = extract_i18n_from_zip(args.toei_zip)
        for k, v in toei_map.items():
            by_pref[13][k] = v
        print(f"Merged Toei Bus official English names: {len(toei_map)}")

    total_entries = 0
    total_bytes = 0
    for pref in sorted(by_pref.keys()):
        mp = by_pref[pref]
        if not mp:
            continue
        text = json.dumps(mp, ensure_ascii=False, separators=(",", ":"))
        out_file = args.out_i18n_dir / f"pref_{pref:02d}.json"
        out_file.write_text(text, encoding="utf-8")
        total_entries += len(mp)
        total_bytes += out_file.stat().st_size

    # 東京都分はアプリ内バンドルへもコピー（オフライン即表示用）
    if 13 in by_pref and args.bundle_tokyo_out:
        args.bundle_tokyo_out.parent.mkdir(parents=True, exist_ok=True)
        args.bundle_tokyo_out.write_text(
            json.dumps(by_pref[13], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    print(
        "NATIONAL_I18N_OK",
        json.dumps({
            "prefectures": len(by_pref),
            "total_entries": total_entries,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "tokyo_entries": len(by_pref.get(13, {})),
            "tokyo_kb": round(args.bundle_tokyo_out.stat().st_size / 1024, 1) if args.bundle_tokyo_out.exists() else 0,
        }),
    )


if __name__ == "__main__":
    main()
