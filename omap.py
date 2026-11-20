"""Validate and package small GeoJSON maps for offline viewing."""
import argparse
import base64
import hashlib
import io
import json
import math
import os
import stat
import sys
from pathlib import Path
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

MAP_NAME = "map.geojson"
MANIFEST_NAME = "manifest.json"
MAX_MAP = 4 * 1024 * 1024
MAX_MANIFEST = 8 * 1024
MAX_PACK = MAX_MAP + MAX_MANIFEST + 8 * 1024
MAX_TEMPLATE = 256 * 1024
MAX_FEATURES = 2_000
MAX_POSITIONS = 50_000
MAX_DEPTH = 20
PLACEHOLDER = "MAP_PAYLOAD_BASE64"


class Invalid(ValueError):
    """An unsupported input or inconsistent pack."""


def read_regular(path, limit):
    # Nonblocking open prevents hanging on a substituted special file on POSIX.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if Path(path).is_symlink():
        raise Invalid("symlink inputs are not supported")
    with os.fdopen(os.open(path, flags), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Invalid("input must be an ordinary file")
        if info.st_size > limit:
            raise Invalid("input exceeds size limit")
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise Invalid("input exceeds size limit")
    return raw


def unique_object(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise Invalid("duplicate JSON member")
        obj[key] = value
    return obj


def nonfinite(_value):
    raise Invalid("non-finite JSON number")


def finite_float(text):
    result = float(text)
    if not math.isfinite(result):
        raise Invalid("non-finite JSON number")
    return result


def bounded_int(text):
    if len(text) > 80:
        raise Invalid("JSON integer is too large")
    return int(text)


def decode(raw):
    data = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_int=bounded_int,
        parse_float=finite_float,
        parse_constant=nonfinite,
    )
    def inspect(value, depth):
        if depth > MAX_DEPTH:
            raise Invalid("JSON nesting exceeds limit")
        if isinstance(value, dict):
            for key, member in value.items():
                key.encode("utf-8")
                inspect(member, depth + 1)
        elif isinstance(value, list):
            for member in value:
                inspect(member, depth + 1)
        elif isinstance(value, str):
            value.encode("utf-8")  # Reject lone UTF-16 surrogates.

    inspect(data, 0)
    return data


def encode(value):
    return (json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ) + "\n").encode("utf-8")


def closed_object(value, required, optional=()):
    return isinstance(value, dict) and required <= value.keys() <= required.union(optional)


def array(value, minimum):
    if not isinstance(value, list) or len(value) < minimum:
        raise Invalid("invalid or empty coordinate array")
    return value


def map_stats(collection):
    if not closed_object(collection, {"type", "features"}) or collection["type"] != "FeatureCollection":
        raise Invalid("expected a FeatureCollection with only type and features")
    features = collection["features"]
    if not isinstance(features, list) or not 1 <= len(features) <= MAX_FEATURES:
        raise Invalid("feature count outside supported range")

    west, south, east, north = 180, 90, -180, -90
    positions = 0

    def point(value):
        nonlocal west, south, east, north, positions
        if not isinstance(value, list) or len(value) != 2:
            raise Invalid("each position must contain exactly longitude and latitude")
        lon, lat = value
        if (type(lon) not in (int, float) or type(lat) not in (int, float)
                or not -180 <= lon <= 180 or not -90 <= lat <= 90):
            raise Invalid("position must use numeric longitude/latitude within WGS 84 bounds")
        positions += 1
        if positions > MAX_POSITIONS:
            raise Invalid("position count exceeds limit")
        west, south = min(west, lon), min(south, lat)
        east, north = max(east, lon), max(north, lat)

    def line(value, minimum=2):
        points = array(value, minimum)
        for position in points:
            point(position)
        if any(abs(a[0] - b[0]) > 180 for a, b in zip(points, points[1:])):
            raise Invalid("split segments crossing the antimeridian before packaging")

    def polygon(value):
        for ring in array(value, 1):
            line(ring, 4)
            if ring[0] != ring[-1]:
                raise Invalid("polygon rings must be closed")

    def many(value, operation):
        for item in array(value, 1):
            operation(item)

    handlers = {
        "Point": point,
        "MultiPoint": lambda value: many(value, point),
        "LineString": line,
        "MultiLineString": lambda value: many(value, line),
        "Polygon": polygon,
        "MultiPolygon": lambda value: many(value, polygon),
    }
    for feature in features:
        if (not closed_object(feature, {"type", "properties", "geometry"}, {"id"})
                or feature["type"] != "Feature"):
            raise Invalid("expected a GeoJSON Feature with supported members")
        if feature["properties"] is not None and not isinstance(feature["properties"], dict):
            raise Invalid("properties must be an object or null")
        if "id" in feature and type(feature["id"]) not in (str, int, float):
            raise Invalid("feature id must be a string or number")
        geometry = feature["geometry"]
        if not closed_object(geometry, {"type", "coordinates"}):
            raise Invalid("expected a geometry with type and coordinates")
        geometry_type = geometry["type"]
        if not isinstance(geometry_type, str) or geometry_type not in handlers:
            raise Invalid("unsupported geometry type")
        handlers[geometry_type](geometry["coordinates"])

    return {"feature_count": len(features), "position_count": positions,
            "bounds": [west, south, east, north]}


def provenance(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 400
            or value != value.strip() or not value.isprintable()):
        raise Invalid("source and license must each be 1–400 printable characters without edge spaces")
    return value


def coverage_note(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 400
            or value != value.strip() or not value.isprintable()):
        raise Invalid("coverage notes must each be 1–400 printable characters without edge spaces")
    return value


def manifest_for(stats, data, source, license_text, coverage=None):
    manifest = {
        "schema_version": 1 if coverage is None else 2,
        "source": provenance(source),
        "license": provenance(license_text),
        "sha256": hashlib.sha256(data).hexdigest(),
        **stats,
    }
    if coverage is not None:
        if not closed_object(coverage, {"included", "gaps"}):
            raise Invalid("coverage requires both included and gaps notes")
        manifest["coverage"] = {key: coverage_note(value) for key, value in coverage.items()}
    return manifest


def write_new(path, raw):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as output:
        output.write(raw)


def build(input_path, output_path, source, license_text, *, coverage=None):
    collection = decode(read_regular(input_path, MAX_MAP))
    stats = map_stats(collection)
    data = encode(collection)
    if len(data) > MAX_MAP:
        raise Invalid("normalized map exceeds size limit")
    manifest = manifest_for(stats, data, source, license_text, coverage)
    manifest_bytes = encode(manifest)
    if len(manifest_bytes) > MAX_MANIFEST:
        raise Invalid("manifest exceeds size limit")
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for name, raw in ((MAP_NAME, data), (MANIFEST_NAME, manifest_bytes)):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_STORED
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, raw)
    write_new(output_path, buffer.getvalue())
    return manifest


def verify(pack_path, *, expected_sha256=None):
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or len(expected_sha256) != 64
        or any(ch not in "0123456789abcdefABCDEF" for ch in expected_sha256)
    ):
        raise Invalid("expected pack SHA-256 must contain exactly 64 hexadecimal characters")
    raw = read_regular(pack_path, MAX_PACK)
    if (expected_sha256 is not None
            and hashlib.sha256(raw).hexdigest() != expected_sha256.lower()):
        raise Invalid("whole-pack SHA-256 does not match the expected digest")
    limits = {MAP_NAME: MAX_MAP, MANIFEST_NAME: MAX_MANIFEST}
    with ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) != len(limits) or {e.filename for e in entries} != limits.keys():
            raise Invalid("pack must contain exactly map.geojson and manifest.json")
        parts = {}
        for entry in entries:
            limit = limits[entry.filename]
            mode = entry.external_attr >> 16
            if (entry.orig_filename != entry.filename or entry.is_dir()
                    or entry.compress_type != ZIP_STORED or entry.flag_bits & 1
                    or (entry.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or not 0 < entry.file_size <= limit or entry.file_size != entry.compress_size):
                raise Invalid("unsupported ZIP member metadata")
            with archive.open(entry) as handle:
                data = handle.read(limit + 1)
            if len(data) != entry.file_size:
                raise Invalid("ZIP member length does not match")
            parts[entry.filename] = data
    map_data, manifest_data = parts[MAP_NAME], parts[MANIFEST_NAME]
    manifest = decode(manifest_data)
    required = {"schema_version", "source", "license", "sha256", "feature_count", "position_count", "bounds"}
    if not closed_object(manifest, required, {"coverage"}):
        raise Invalid("unexpected manifest structure")
    collection = decode(map_data)
    stats = map_stats(collection)
    if encode(collection) != map_data:
        raise Invalid("map is not in normalized form")
    expected = manifest_for(stats, map_data, manifest["source"], manifest["license"],
                            manifest.get("coverage"))
    if encode(expected) != manifest_data:
        raise Invalid("manifest does not match the map or supported schema")
    return {"geojson": collection, "manifest": expected}


def view(pack_path, output_path, *, expected_sha256=None):
    payload = verify(pack_path, expected_sha256=expected_sha256)
    template = read_regular(Path(__file__).with_name("viewer.html"), MAX_TEMPLATE).decode("utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise Invalid("viewer template must contain exactly one payload slot")
    data = base64.b64encode(encode(payload)).decode("ascii")
    write_new(output_path, template.replace(PLACEHOLDER, data).encode("utf-8"))
    return payload["manifest"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    package = subcommands.add_parser("build", help="validate and package a local GeoJSON file")
    package.add_argument("input")
    package.add_argument("--source", required=True)
    package.add_argument("--license", required=True)
    package.add_argument("--out", required=True)
    package.add_argument("--coverage-included", help="what the pack includes (use with --coverage-gaps)")
    package.add_argument("--coverage-gaps", help="known gaps or unassessed coverage (use with --coverage-included)")
    check = subcommands.add_parser("verify", help="check a pack without extracting it")
    check.add_argument("pack")
    render = subcommands.add_parser("view", help="generate a standalone offline HTML viewer")
    render.add_argument("pack")
    render.add_argument("--out", required=True)
    for command in (check, render):
        command.add_argument(
            "--expect-pack-sha256", metavar="HEX",
            help="require this independently obtained whole-archive SHA-256 before proceeding",
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            coverage = None
            if args.coverage_included is not None or args.coverage_gaps is not None:
                if args.coverage_included is None or args.coverage_gaps is None:
                    raise Invalid("use --coverage-included and --coverage-gaps together")
                coverage = {"included": args.coverage_included, "gaps": args.coverage_gaps}
            manifest = build(args.input, args.out, args.source, args.license, coverage=coverage)
        elif args.command == "verify":
            manifest = verify(args.pack, expected_sha256=args.expect_pack_sha256)["manifest"]
        else:
            manifest = view(args.pack, args.out, expected_sha256=args.expect_pack_sha256)
    except FileExistsError:
        print("Error: output already exists; choose a new path.", file=sys.stderr)
        return 1
    except Invalid as error:
        print(f"Error: {error}.", file=sys.stderr)
        return 1
    except (ValueError, TypeError, OSError, RuntimeError, BadZipFile):
        print("Error: invalid or unreadable input, pack, template, or output path.", file=sys.stderr)
        return 1
    # Do not allow terminal escapes or other control syntax in JSON output.
    print(json.dumps({"operation": args.command, "manifest": manifest}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
