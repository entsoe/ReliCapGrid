"""
Generate TestConfigurationRelease packages (level 1 of the release process, issue #332).

Each test configuration is defined by buildScripts/test_configurations/<Name>.toml
(include/exclude globs relative to Instance/). The matching source files are
collected, whatever their place in the authority-based Instance/ tree, and
written to the standard release structure:

  TestConfigurationRelease/<Name>/
    manifest.xml, manifest.trig, manifest.json   # DCAT manifest (CimPal)
    sources.json                                  # release path -> Instance/ source path
    instance/<area>/<serialisation>/<timestamp>_<file>

  area           Grid, NetworkCode, boundaryData, commonData or referenceData
  serialisation  cimxml / ttl: the source file, copied byte for byte so that
                 intentionally invalid (negative) test data is kept as authored;
                 trig / jsonld: re-serialised with rdflib (relative CIM ids
                 "#_<uuid>" become urn:uuid:<uuid>, as tools/cim-urn-uuid.pl does;
                 any other relative id is reported and keeps the parse base)

Previous output of a configuration is removed before it is regenerated. The
release is the input for validation (validate_instances.py --release) and the
scenario tests; it is generated, never edited by hand.

Usage:
    python buildScripts/create_test_configuration_release.py
    python buildScripts/create_test_configuration_release.py FullReliCapGridBaseCase
    python buildScripts/create_test_configuration_release.py --cimpal-jar tools/CimPal-CLI.jar
    python buildScripts/create_test_configuration_release.py --timestamp 20261118T1030Z
    python buildScripts/create_test_configuration_release.py --list
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import rdflib

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = REPO_ROOT / "Instance"
CONFIG_DIR = Path(__file__).resolve().parent / "test_configurations"
DEFAULT_OUTPUT = REPO_ROOT / "TestConfigurationRelease"

# Instance/<area>/... for shared data, Instance/<TSO>/<folder>/... otherwise
SHARED_AREAS = {"boundaryData", "commonData", "referenceData"}
AREA_BY_FOLDER = {"Grid": "Grid", "ChangeSet": "Grid", "NetworkCode": "NetworkCode", "GridSituation": "NetworkCode"}

# source suffix -> (serialisation folder, rdflib parser)
SOURCE_FORMATS = {".xml": ("cimxml", "xml"), ".ttl": ("ttl", "turtle")}
# derived serialisation folder -> (suffix, rdflib serialiser)
DERIVED_FORMATS = {"trig": (".trig", "trig"), "jsonld": (".jsonld", "json-ld")}
MEDIA_TYPES = {
    "trig": "http://www.iana.org/assignments/media-types/application/trig",
    "jsonld": "http://www.iana.org/assignments/media-types/application/ld+json",
}
DCAT = rdflib.Namespace("http://www.w3.org/ns/dcat#")

# placeholder base for relative ids (rdf:ID="_x", rdf:about="#_x"); rdflib
# cannot resolve fragments against a urn: base
PARSE_BASE = "http://relicap.invalid/base"
TIMESTAMP_FORMAT = "%Y%m%dT%H%MZ"
DATASET_TYPES = {
    DCAT.Dataset,
    rdflib.URIRef("http://iec.ch/TC57/61970-552/ModelDescription/1#FullModel"),
}


def glob_regex(pattern: str) -> re.Pattern:
    """Glob -> regex over posix paths: "**/" spans zero or more folders, "*" stays within one."""
    parts = []
    for token in re.split(r"(\*\*/|\*\*|\*|\?)", pattern):
        parts.append({"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token, re.escape(token)))
    return re.compile("".join(parts) + r"\Z")


def load_configs(names: list[str] | None) -> dict[str, dict]:
    available = {p.stem: p for p in sorted(CONFIG_DIR.glob("*.toml"))}
    unknown = [n for n in names or [] if n not in available]
    if unknown:
        sys.exit(f"ERROR: unknown test configuration(s): {', '.join(unknown)}. Available: {', '.join(available)}")
    return {n: tomllib.loads(available[n].read_text(encoding="utf-8")) for n in names or available}


def select_sources(config: dict) -> list[Path]:
    """Instance/-relative paths matched by the configuration's include and not by its exclude globs."""
    candidates = sorted(p.relative_to(INSTANCE_DIR) for p in INSTANCE_DIR.rglob("*") if p.is_file())
    includes = {g: glob_regex(g) for g in config["include"]}
    excludes = [glob_regex(g) for g in config.get("exclude", [])]

    unmatched = [g for g, rx in includes.items() if not any(rx.match(p.as_posix()) for p in candidates)]
    if unmatched:
        raise ValueError(f"include glob(s) match no file: {', '.join(unmatched)}")

    return [p for p in candidates
            if any(rx.match(p.as_posix()) for rx in includes.values())
            and not any(rx.match(p.as_posix()) for rx in excludes)]


def release_area(source: Path) -> str:
    if source.parts[0] in SHARED_AREAS:
        return source.parts[0]
    folder = source.parts[1] if len(source.parts) > 2 else None
    if folder not in AREA_BY_FOLDER:
        raise ValueError(f"no release area for Instance/{source.as_posix()}")
    return AREA_BY_FOLDER[folder]


def to_urn_uuid(term):
    if isinstance(term, rdflib.URIRef) and term.startswith(PARSE_BASE + "#_"):
        return rdflib.URIRef("urn:uuid:" + term[len(PARSE_BASE) + 2:])
    return term


def as_dataset(path: Path, parser: str) -> rdflib.Dataset:
    """Parse one source file into a Dataset whose named graph is the file's own
    dataset header (dcat:Dataset / md:FullModel), the default graph otherwise."""
    graph = rdflib.Graph()
    graph.parse(path, format=parser, publicID=PARSE_BASE)
    headers = {to_urn_uuid(s) for s, o in graph.subject_objects(rdflib.RDF.type) if o in DATASET_TYPES}

    dataset = rdflib.Dataset()
    for prefix, namespace in graph.namespaces():
        dataset.bind(prefix, namespace, replace=True)
    target = dataset.graph(headers.pop()) if len(headers) == 1 else dataset.default_graph
    for triple in graph:
        target.add(tuple(to_urn_uuid(t) for t in triple))
    unresolved = sorted({str(t) for triple in target for t in triple if str(t).startswith(PARSE_BASE)})
    return dataset, unresolved


def write_manifest(release_dir: Path, models: list[Path], converted: set[Path], cimpal_jar: Path) -> None:
    """CimPal writes the DCAT manifest of the source-format copies as Turtle. Its
    accessURLs are re-pointed at the release paths and the trig / jsonld copies
    are added as further distributions of the same datasets; the result is
    published as RDF/XML, TriG and JSON-LD."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_ttl = Path(tmp) / "manifest.ttl"
        subprocess.run(
            ["java", "-jar", str(cimpal_jar), "manifest",
             "--files", ",".join(str(m) for m in models),
             "--access-url", "instance", "--output", str(manifest_ttl)],
            cwd=release_dir, check=True, stdout=subprocess.DEVNULL,
        )
        graph = rdflib.Graph().parse(manifest_ttl, format="turtle")

    by_name = {m.name: m for m in models}
    for distribution, url in list(graph.subject_objects(DCAT.accessURL)):
        model = by_name[str(url).rsplit("/", 1)[-1]]
        graph.set((distribution, DCAT.accessURL, rdflib.URIRef(f"file:{model.as_posix()}")))
        if model not in converted:
            continue
        for derived, (suffix, _) in DERIVED_FORMATS.items():
            copy = rdflib.URIRef(f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{distribution}/{derived}')}")
            for p, o in graph.predicate_objects(distribution):
                graph.add((copy, p, o))
            path = model.parent.parent / derived / (model.stem + suffix)
            graph.set((copy, DCAT.accessURL, rdflib.URIRef(f"file:{path.as_posix()}")))
            graph.set((copy, DCAT.mediaType, rdflib.URIRef(MEDIA_TYPES[derived])))
            for dataset in graph.objects(distribution, DCAT.isDistributionOf):
                graph.add((dataset, DCAT.distribution, copy))
    graph.serialize(release_dir / "manifest.xml", format="pretty-xml")
    graph.serialize(release_dir / "manifest.trig", format="trig")
    graph.serialize(release_dir / "manifest.json", format="json-ld")


def build_release(name: str, config: dict, output_dir: Path, timestamp: str, cimpal_jar: Path | None) -> int:
    """Generate one test configuration; returns the number of conversion warnings."""
    sources = select_sources(config)
    release_dir = output_dir / name
    if release_dir.exists():
        shutil.rmtree(release_dir)

    written: dict[str, str] = {}      # release-relative path -> repo-relative source path
    models: list[Path] = []           # source-format copies, described by the manifest
    converted: set[Path] = set()      # ... of which trig / jsonld copies were written
    warnings = 0
    for source in sources:
        if source.suffix not in SOURCE_FORMATS:
            raise ValueError(f"unsupported source format: Instance/{source.as_posix()}")
        folder, parser = SOURCE_FORMATS[source.suffix]
        area_dir = Path("instance") / release_area(source)
        stamped = f"{timestamp}_{source.stem}"
        source_path = INSTANCE_DIR / source
        source_rel = source_path.relative_to(REPO_ROOT).as_posix()

        targets = {area_dir / folder / (stamped + source.suffix): None}
        try:
            dataset, unresolved = as_dataset(source_path, parser)
            targets |= {area_dir / derived / (stamped + suffix): fmt for derived, (suffix, fmt) in DERIVED_FORMATS.items()}
        except Exception as error:  # negative test data may not parse; ship it as authored
            warnings += 1
            print(f"  WARNING: {source_rel}: not converted ({type(error).__name__}: {error})")
        else:
            if unresolved:
                warnings += 1
                ids = ", ".join(i[len(PARSE_BASE):] for i in unresolved)
                print(f"  WARNING: {source_rel}: relative id(s) not of the form #_<uuid>, left as {PARSE_BASE}<id>: {ids}")

        for target, fmt in targets.items():
            if target.as_posix() in written:
                raise ValueError(f"{target.as_posix()} produced by both {written[target.as_posix()]} and {source_rel}")
            written[target.as_posix()] = source_rel
            (release_dir / target).parent.mkdir(parents=True, exist_ok=True)
            if fmt is None:
                shutil.copyfile(source_path, release_dir / target)
            else:
                dataset.serialize(release_dir / target, format=fmt)
        models.append(area_dir / folder / (stamped + source.suffix))
        if len(targets) > 1:
            converted.add(models[-1])

    (release_dir / "sources.json").write_text(json.dumps(written, indent=2) + "\n", encoding="utf-8")
    if cimpal_jar:
        write_manifest(release_dir, models, converted, cimpal_jar)
    else:
        print("  WARNING: no CimPal jar (--cimpal-jar / CIMPAL_JAR): manifests not written")
        warnings += 1

    print(f"{name}: {len(sources)} source file(s) -> {len(written)} release file(s) in {release_dir}")
    return warnings


def main():
    parser = argparse.ArgumentParser(description="Generate TestConfigurationRelease packages from Instance/.")
    parser.add_argument("configs", nargs="*", metavar="NAME", help="test configurations to build (default: all)")
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT,
                        help="release root folder (default: TestConfigurationRelease/)")
    parser.add_argument("--timestamp", default=datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT),
                        help="filename prefix, e.g. 20261118T1030Z (default: current UTC time)")
    parser.add_argument("--cimpal-jar", type=Path, default=os.environ.get("CIMPAL_JAR"),
                        help="CimPal CLI jar used to write the manifests (default: $CIMPAL_JAR)")
    parser.add_argument("--list", action="store_true", help="print the available test configurations and exit")
    args = parser.parse_args()

    if not re.fullmatch(r"\d{8}T\d{4}Z", args.timestamp):
        sys.exit(f"ERROR: --timestamp must look like 20261118T1030Z, got {args.timestamp!r}")
    configs = load_configs(args.configs)
    if args.list:
        for name, config in configs.items():
            print(f"  {name}: {config.get('description', '')}")
        return
    cimpal_jar = args.cimpal_jar.resolve() if args.cimpal_jar else None

    warnings = 0
    for name, config in configs.items():
        try:
            warnings += build_release(name, config, args.output.resolve(), args.timestamp, cimpal_jar)
        except ValueError as error:
            sys.exit(f"ERROR: {name}: {error}")
    print(f"\nDone ({warnings} warning(s)).")


if __name__ == "__main__":
    main()
