"""Keep the service and ordered onward calls from the same GTFS as the timetable."""
import hashlib
from collections import defaultdict
from itertools import groupby
from zipfile import ZipFile

from gtfs_to_app_data import normalize_hhmm, read_csv_from_zip


def find_turnback(calls, fallback, names):
    """Resolve an advertised outbound destination to its actual alighting call."""
    terminal = names[calls[-1]["stop_id"]]
    directions = [head for head, _ in groupby(
        (call.get("stop_headsign") or fallback).strip() for call in calls
    )]
    if len(directions) != 2:
        return None
    outbound, onward = directions
    if not outbound or "循環" in outbound or onward != terminal:
        return None

    # The headsign can switch before or after the actual turnaround. Use the
    # ordered stop itself, including drop-off-only stops, rather than the switch.
    matches = [int(call["stop_sequence"]) for call in calls[1:-1]
               if names[call["stop_id"]] == outbound and call.get("drop_off_type") != "1"]
    if len(matches) == 1:
        return [matches[0], outbound, onward]
    return None


def build_trip_calls(zip_path, *, community_prefix=None):
    version = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with ZipFile(zip_path) as archive:
        trips = {r["trip_id"]: r for r in read_csv_from_zip(archive, "trips.txt")}
        names = {r["stop_id"]: r["stop_name"] for r in read_csv_from_zip(archive, "stops.txt")}
        rows = read_csv_from_zip(archive, "stop_times.txt")
    if community_prefix is not None:
        # An incomplete source trip cannot establish its own terminal or sequence.
        invalid = {r["trip_id"] for r in rows if r.get("stop_id") not in names
                   or not (r.get("stop_sequence") or "").isdigit()}
        rows = [r for r in rows if r["trip_id"] in trips and r["trip_id"] not in invalid]
    rows.sort(key=lambda r: (r["trip_id"], int(r["stop_sequence"])))
    poles = {}
    leg_indexes = defaultdict(dict)
    for trip, group in groupby(rows, key=lambda r: r["trip_id"]):
        calls = list(group)
        trip_info = trips[trip]
        fallback = trip_info.get("trip_headsign") or names[calls[-1]["stop_id"]]
        turn = find_turnback(calls, fallback, names)
        for i, origin in enumerate(calls[:-1]):
            if origin.get("pickup_type") == "1":
                continue
            pole_id = origin["stop_id"]
            pole = poles.setdefault(pole_id, {"version": version, "turnback_schema": 2, "legs": [], "trips": {}})
            if (turn and int(origin["stop_sequence"]) < turn[0]
                    and (origin.get("stop_headsign") or fallback).strip() == turn[1]):
                pole.setdefault("turnbacks", {})[trip] = turn
            # Share the names and geometry of a hop across trips. Include alight-only stops
            # up to the next boardable pole, which has the continuation in its bucket.
            leg = []
            arrivals = []
            for j in range(i + 1, len(calls)):
                dest = calls[j]
                stop = dest["stop_id"]
                terminal = j == len(calls) - 1
                leg.append((int(dest["stop_sequence"]), (community_prefix or "") + stop, names[stop], int(terminal)))
                arrivals.append(normalize_hhmm(dest.get("arrival_time") or ""))
                if terminal or (dest.get("pickup_type") != "1" and (dest.get("departure_time") or dest.get("arrival_time"))):
                    break
            signature = tuple(leg)
            indexes = leg_indexes[pole_id]
            if signature not in indexes:
                indexes[signature] = len(pole["legs"])
                pole["legs"].append(leg)
            trip_calls = pole["trips"].setdefault(trip, [trip_info["service_id"]])
            dep = normalize_hhmm(origin.get("departure_time") or origin.get("arrival_time") or "")
            call = [int(origin["stop_sequence"]), dep, indexes[signature], *arrivals]
            if community_prefix is not None:
                # A pole can serve several routes or the same trip twice in different directions.
                variants = pole.setdefault("variants", [])
                variant = [trip_info["route_id"], (origin.get("stop_headsign") or "").strip() or fallback]
                if variant not in variants:
                    variants.append(variant)
                call.append(variants.index(variant))
            trip_calls.append(call)
    return poles


def attach_community_calls(poles, zip_path, prefix):
    if not poles or poles[0].get("operator_type") != "community":
        return
    onward = build_trip_calls(zip_path, community_prefix=prefix)
    for pole in poles:
        if pole["pole_id"] in onward:
            pole["onward"] = onward[pole["pole_id"]]
