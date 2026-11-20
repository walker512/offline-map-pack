# Offline Map Pack Studio

**Hand someone a small map they can open without a connection. Give them a
separate way to verify the pack.**

Turn local vector features into an integrity-checked archive with a source and
license note. Export it as a single local HTML file for a field review or team
handoff. No account, tiles, API keys, external scripts, or internet connection
are needed. The viewer shows only supplied features.

## Try it

Use Python 3.10 or newer from the source folder; no dependencies:

```sh
python omap.py build examples/ridge-reserve.geojson --source "Fictional example by walker512" --license "MIT (included example)" --out ridge.omap
python omap.py verify ridge.omap
python omap.py view ridge.omap --out ridge-view.html
```

Open **ridge-view.html** in a browser. Fit or zoom the map and select a feature
to inspect it. The five features are fictional and not for navigation. Use new
output filenames on repeat runs.

## Try a coverage-aware handoff

The fictional lake pack includes a pier and footpath on the west bank; the east
bank outline identifies an unsurveyed area, not a hazard. Use both flags to
show what is included and what is missing prominently above the map:

```sh
python omap.py build examples/lake-handoff.geojson --source "Fictional lake by walker512" --license "MIT (included example)" --coverage-included "West bank pier and access path" --coverage-gaps "East bank not surveyed; no points or routes included there" --out lake.omap
python omap.py verify lake.omap
python omap.py view lake.omap --out lake-view.html
```

Coverage text is sender-declared, not inferred from geometry or independently
verified. A blank area does not imply an absence of features. The upgraded tool
still checks and renders older packs without coverage statements. Builds
without either flag retain the original format; builds with both use
schema version 2, which requires this upgraded tool.

## Prepare your own handoff

1. Export a small [WGS 84 GeoJSON](https://www.rfc-editor.org/rfc/rfc7946.html)
   FeatureCollection with two-dimensional `[longitude, latitude]` coordinates.
   Supply a truthful source and license that permit the sharing you intend.
2. Send the `.omap` archive for verification and the `.html` file for viewing.
   Recipients can regenerate the viewer from the pack with a trusted copy of
   the tool instead of opening HTML from an unfamiliar sender.
3. Review all locations and properties first. The viewer embeds the dataset;
   information not immediately visible can still be present in the file.

## Check against a known pack digest

To catch changes to the entire archive, including its source, license and
coverage notes, the sender can compute the archive's SHA-256:

```sh
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('lake.omap').read_bytes()).hexdigest())"
```

The recipient obtains that digest through a separately trusted channel and
replaces `HEX` below with its 64 hexadecimal characters:

```sh
python omap.py verify lake.omap --expect-pack-sha256 HEX
python omap.py view lake.omap --expect-pack-sha256 HEX --out lake-checked.html
```

A mismatch stops before parsing the archive or writing the viewer. This check
does not verify who sent the pack or whether its notes and locations are
accurate. A digest delivered with the pack through the same untrusted channel
does not establish authenticity. This option works with both pack versions.

## Boundaries

- Supported geometries: Point, MultiPoint, LineString, MultiLineString, Polygon
  and MultiPolygon. This is a bounded GeoJSON profile, not a full GIS validator.
  It neither reprojects nor repairs topology. Use a local area that does not
  cross the antimeridian. There is no basemap, routing, tracking or precision
  measurement. The limit is 2,000 features, 50,000 positions and 4 MiB of map
  JSON. Remove extra GeoJSON members such as `bbox` before building.
- The SHA-256 binds the normalized map bytes to the digest in the manifest.
  Matching bytes do not establish authenticity, coordinate accuracy, or the
  validity of a stated license. The map digest does not cover the source,
  license, or coverage notes; use a known whole-archive digest to check those
  bytes too. The pack and viewer are not encrypted. Treat
  exact property values as data in the archive, not as values guaranteed to
  retain precision in a JavaScript viewer. CLI output includes the manifest,
  with geographic bounds and any coverage, source and license text. Avoid
  publishing job logs with private data.
  New files use private permissions on POSIX; Windows ACLs are not verified.
- [MapLibre Martin](https://github.com/maplibre/martin) serves map tiles from
  PMTiles, MBTiles and other sources. This tool instead targets small vector
  handoffs with no tiles or server. It is not a replacement for a tile pipeline.

Code and fictional samples: MIT, walker512.
