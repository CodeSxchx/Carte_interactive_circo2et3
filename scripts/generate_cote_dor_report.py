from __future__ import annotations

import argparse
import csv
import dataclasses
import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DIST_DIR = ROOT / "dist"
CACHE_DIR = ROOT / "cache"


DEFAULT_DEPT_CODE = "21"
DEFAULT_GEOJSON_URL = (
    "https://geo.api.gouv.fr/departements/{dept}/communes?format=geojson&geometry=contour"
)


@dataclasses.dataclass(frozen=True)
class Commune:
    insee: str
    name: str


@dataclasses.dataclass(frozen=True)
class PageCommune:
    page_id: str  # HTML section id (without #)
    insee: str
    name: str
    label: str  # display label (list/select)
    legis_circo: str | None  # "C2"/"C3" to filter legislatives, None = all


@dataclasses.dataclass(frozen=True)
class ResultRow:
    candidate: str
    votes: int | None
    pct: float | None
    tour: str | None
    bureau: str | None
    circo: str | None
    raw: dict[str, str]


def _read_text_best_effort(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        class _Default(csv.Dialect):
            delimiter = ";"
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL

        return _Default()


def _normalize_header(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_%]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def _pick_column(headers: list[str], patterns: list[str]) -> str | None:
    norm = {_normalize_header(h): h for h in headers}
    for pat in patterns:
        rx = re.compile(pat)
        for nh, original in norm.items():
            if rx.search(nh):
                return original
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    s = s.replace(" ", "").replace("\u00a0", "")
    s = s.replace(".", "").replace(",", ".")
    # Keep only digits
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    s = s.replace(" ", "").replace("\u00a0", "")
    s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _infer_tour_from_text(value: str) -> str | None:
    s = _normalize_header(value)
    if not s:
        return None
    if re.search(r"(^|_)(t1|tour_?1|1er|premier)($|_)", s) or s in {"1", "01"}:
        return "T1"
    if re.search(r"(^|_)(t2|tour_?2|2e|second|deuxieme)($|_)", s) or s in {"2", "02"}:
        return "T2"
    # Try to extract a standalone number
    m = re.search(r"(^|_)(\d{1,2})($|_)", s)
    if m and m.group(2) in {"1", "2"}:
        return f"T{m.group(2)}"
    return None


def _infer_tour_from_filename(stem: str) -> str | None:
    return _infer_tour_from_text(stem)


def _infer_insee_from_text(value: str) -> str | None:
    # INSEE commune codes are 5 digits for metropolitan France (e.g. 21231)
    m = re.search(r"(?<!\d)(\d{5})(?!\d)", value)
    return m.group(1) if m else None


def _infer_circo_from_path(relative_path: Path) -> str | None:
    # Try to infer circonscription from directory names like:
    # circ5, circo_5, circonscription-05, c5, C05, etc.
    for part in relative_path.parts[:-1]:
        p = part.strip()
        m = re.search(r"(?:circ|circo|circonscription|c)[\s_\-]*0*(\d{1,2})", p, flags=re.IGNORECASE)
        if m:
            return f"C{int(m.group(1))}"
        # Also accept directories like "2e", "3eme", "3ème"
        m2 = re.fullmatch(r"0*(\d{1,2})(?:\s*(?:e|eme|ème))?", p, flags=re.IGNORECASE)
        if m2:
            return f"C{int(m2.group(1))}"
    return None


def load_communes_selection(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    text = _read_text_best_effort(path)
    dialect = _sniff_dialect(text[:5000])
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        return None
    insee_col = _pick_column(
        list(reader.fieldnames),
        patterns=[r"(^|_)insee($|_)", r"codgeo", r"code_commune", r"commune_code"],
    )
    if not insee_col:
        raise ValueError(
            f"Impossible de trouver une colonne INSEE dans {path.name}. Colonnes: {reader.fieldnames}"
        )
    selected: set[str] = set()
    for row in reader:
        code = (row.get(insee_col) or "").strip()
        if code:
            selected.add(code)
    return selected or None


def load_insee_list(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    text = _read_text_best_effort(path)
    dialect = _sniff_dialect(text[:5000])
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        return None
    insee_col = _pick_column(
        list(reader.fieldnames),
        patterns=[r"(^|_)insee($|_)", r"codgeo", r"code_commune", r"commune_code"],
    )
    if not insee_col:
        raise ValueError(
            f"Impossible de trouver une colonne INSEE dans {path.name}. Colonnes: {reader.fieldnames}"
        )
    out: set[str] = set()
    for row in reader:
        code = (row.get(insee_col) or "").strip()
        if code:
            out.add(code)
    return out or None


def _download_geojson(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "site-suivi-municipales/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read()
    dest.write_bytes(content)


def load_geojson_communes(dept: str, geojson_path: Path | None) -> dict[str, Any]:
    if geojson_path and geojson_path.exists():
        return json.loads(_read_text_best_effort(geojson_path))

    candidate_local = DATA_DIR / f"communes-{dept}.geojson"
    if candidate_local.exists():
        return json.loads(_read_text_best_effort(candidate_local))

    cached = CACHE_DIR / f"communes-{dept}.geojson"
    if cached.exists():
        return json.loads(_read_text_best_effort(cached))

    url = DEFAULT_GEOJSON_URL.format(dept=dept)
    try:
        _download_geojson(url, cached)
        return json.loads(_read_text_best_effort(cached))
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(
            "Impossible de récupérer le GeoJSON des communes.\n"
            f"- URL: {url}\n"
            f"- Erreur: {e}\n"
            "Solutions:\n"
            f"- Mettre un fichier GeoJSON local: {candidate_local}\n"
            f"- Ou relancer quand tu as internet (le cache ira dans {cached})"
        ) from e


def _iter_geojson_coords(geometry: dict[str, Any]) -> Iterable[tuple[float, float]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not gtype or coords is None:
        return []

    def walk(obj: Any) -> Iterable[tuple[float, float]]:
        if isinstance(obj, (list, tuple)) and len(obj) == 2 and all(
            isinstance(x, (int, float)) for x in obj
        ):
            yield (float(obj[0]), float(obj[1]))
        elif isinstance(obj, (list, tuple)):
            for it in obj:
                yield from walk(it)

    return walk(coords)


def _bbox_for_features(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for f in features:
        geom = f.get("geometry") or {}
        for x, y in _iter_geojson_coords(geom):
            minx = min(minx, x)
            miny = min(miny, y)
            maxx = max(maxx, x)
            maxy = max(maxy, y)
    if not math.isfinite(minx):
        raise ValueError("GeoJSON vide ou sans coordonnées.")
    return (minx, miny, maxx, maxy)


def _project(
    lon: float,
    lat: float,
    *,
    bbox: tuple[float, float, float, float],
    width: float,
    height: float,
    margin: float,
) -> tuple[float, float]:
    minx, miny, maxx, maxy = bbox
    sx = (width - 2 * margin) / (maxx - minx)
    sy = (height - 2 * margin) / (maxy - miny)
    s = min(sx, sy)
    dx = margin + (width - 2 * margin - s * (maxx - minx)) / 2.0
    dy = margin + (height - 2 * margin - s * (maxy - miny)) / 2.0

    x = dx + (lon - minx) * s
    y = dy + (maxy - lat) * s  # invert Y
    return (x, y)


def _ring_to_path(
    ring: list[list[float]],
    *,
    bbox: tuple[float, float, float, float],
    width: float,
    height: float,
    margin: float,
) -> str:
    if not ring:
        return ""
    parts: list[str] = []
    for idx, (lon, lat) in enumerate(ring):
        x, y = _project(lon, lat, bbox=bbox, width=width, height=height, margin=margin)
        cmd = "M" if idx == 0 else "L"
        parts.append(f"{cmd}{x:.2f},{y:.2f}")
    parts.append("Z")
    return " ".join(parts)


def geometry_to_svg_path(
    geometry: dict[str, Any],
    *,
    bbox: tuple[float, float, float, float],
    width: float,
    height: float,
    margin: float,
) -> str:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not gtype or coords is None:
        return ""

    path_parts: list[str] = []
    if gtype == "Polygon":
        # coords: [ring1, ring2, ...]
        for ring in coords:
            path_parts.append(
                _ring_to_path(ring, bbox=bbox, width=width, height=height, margin=margin)
            )
    elif gtype == "MultiPolygon":
        # coords: [polygon1, polygon2, ...] where polygon: [ring1, ...]
        for poly in coords:
            for ring in poly:
                path_parts.append(
                    _ring_to_path(ring, bbox=bbox, width=width, height=height, margin=margin)
                )
    else:
        # Not expected for communes contours
        return ""

    return " ".join(p for p in path_parts if p)


def load_results_from_folder(folder: Path, election_label: str) -> dict[str, list[ResultRow]]:
    return load_results_from_folder_with_overrides(
        folder,
        election_label,
        force_insee_col=None,
        force_name_col=None,
        force_votes_col=None,
        force_pct_col=None,
        force_candidate_col=None,
        verbose=False,
    )


def load_results_from_folder_with_overrides(
    folder: Path,
    election_label: str,
    *,
    force_insee_col: str | None,
    force_name_col: str | None,
    force_votes_col: str | None,
    force_pct_col: str | None,
    force_candidate_col: str | None,
    verbose: bool,
) -> dict[str, list[ResultRow]]:
    results_by_insee: dict[str, list[ResultRow]] = defaultdict(list)
    if not folder.exists():
        return results_by_insee

    def should_skip(p: Path) -> bool:
        name = p.name
        return name.startswith("_") or name.startswith("~") or name.lower().endswith(".tmp.csv")

    for csv_path in sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"
    ):
        if should_skip(csv_path):
            continue
        text = _read_text_best_effort(csv_path)
        dialect = _sniff_dialect(text[:5000])
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        if not reader.fieldnames:
            continue

        headers = list(reader.fieldnames)
        insee_col = force_insee_col or _pick_column(
            headers,
            patterns=[
                r"(^|_)insee($|_)",
                r"codgeo",
                r"code_commune",
                r"commune_code",
                r"code_insee",
            ],
        )
        name_col = force_name_col or _pick_column(
            headers, patterns=[r"(^|_)commune($|_)", r"libelle", r"nom"]
        )
        votes_col = force_votes_col or _pick_column(headers, patterns=[r"voix", r"votes", r"suffrages"])
        pct_col = force_pct_col or _pick_column(headers, patterns=[r"pourcentage", r"pct", r"(^|_)%($|_)"])
        candidate_col = force_candidate_col or _pick_column(
            headers,
            patterns=[
                r"libelle_etendu_liste",
                r"libelle_liste",
                r"nom_liste",
                r"candidat",
                r"nom_candidat",
                r"liste",
            ],
        )
        tour_col = _pick_column(headers, patterns=[r"(^|_)tour($|_)", r"(^|_)round($|_)", r"(^|_)manche($|_)"])
        bureau_col = _pick_column(
            headers,
            patterns=[
                r"(^|_)code_bv($|_)",
                r"(^|_)bv($|_)",
                r"(^|_)bureau($|_)",
                r"num_bv",
                r"bureau_vote",
            ],
        )

        candidate = csv_path.stem.strip()
        rel = csv_path.relative_to(folder)
        circo = _infer_circo_from_path(rel)
        file_tour = _infer_tour_from_filename(candidate)
        if verbose:
            print(
                f"[{election_label}] {rel.as_posix()}: insee={insee_col!r} commune={name_col!r} bv={bureau_col!r} voix={votes_col!r} pct={pct_col!r} candidat={candidate_col!r} tour={tour_col!r} circo={circo!r}"
            )

        if not insee_col:
            raise ValueError(
                f"[{election_label}] {csv_path.name}: colonne INSEE introuvable. Colonnes: {headers}"
            )
        if not votes_col and not pct_col:
            raise ValueError(
                f"[{election_label}] {csv_path.name}: colonne 'voix' ou '%' introuvable. Colonnes: {headers}"
            )

        # Optional: candidate name from column (takes the first non-empty)
        if candidate_col:
            for row in reader:
                v = (row.get(candidate_col) or "").strip()
                if v:
                    candidate = v
                    break
            # Recreate reader after consuming
            reader = csv.DictReader(text.splitlines(), dialect=dialect)

        for row in reader:
            insee = (row.get(insee_col) or "").strip()
            if not insee:
                continue
            raw = {k: (v or "").strip() for k, v in row.items() if k}
            votes = _parse_int(row.get(votes_col) if votes_col else None)
            pct = _parse_float(row.get(pct_col) if pct_col else None)
            tour = None
            if tour_col:
                tour = _infer_tour_from_text((row.get(tour_col) or "").strip()) or (row.get(tour_col) or "").strip()
            tour = tour or file_tour
            bureau = (row.get(bureau_col) or "").strip() if bureau_col else ""
            bureau = bureau or None
            # Attach commune name if available (not used for grouping but useful later)
            if name_col and raw.get(name_col):
                raw["commune_name"] = raw.get(name_col, "")
            results_by_insee[insee].append(
                ResultRow(
                    candidate=candidate,
                    votes=votes,
                    pct=pct,
                    tour=tour,
                    bureau=bureau,
                    circo=circo,
                    raw=raw,
                )
            )

    # Sort by votes desc when present
    for insee, rows in results_by_insee.items():
        def tour_rank(t: str | None) -> int:
            if t == "T2":
                return 0
            if t == "T1":
                return 1
            return 2

        rows.sort(
            key=lambda r: (
                tour_rank(r.tour),
                r.bureau is None,
                _parse_int(r.bureau) if r.bureau and re.fullmatch(r"\d+", r.bureau) else (10**9),
                (r.bureau or ""),
                r.votes is None,
                -(r.votes or 0),
                (r.pct is None, -(r.pct or 0.0)),
                r.candidate.lower(),
            )
        )
        results_by_insee[insee] = rows

    return results_by_insee


def load_results_per_commune_files(
    folder: Path,
    election_label: str,
    *,
    force_insee_col: str | None,
    force_name_col: str | None,
    force_votes_col: str | None,
    force_pct_col: str | None,
    force_candidate_col: str | None,
    verbose: bool,
) -> dict[str, list[ResultRow]]:
    """
    Layout: 1 CSV per commune per tour (ex: 21231_T1.csv, 21231_T2.csv).
    Each CSV contains one row per candidate (optionally per bureau via code_bv).
    """
    results_by_insee: dict[str, list[ResultRow]] = defaultdict(list)
    if not folder.exists():
        return results_by_insee

    def should_skip(p: Path) -> bool:
        name = p.name
        return name.startswith("_") or name.startswith("~") or name.lower().endswith(".tmp.csv")

    for csv_path in sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"
    ):
        if should_skip(csv_path):
            continue
        stem = csv_path.stem.strip()
        file_tour = _infer_tour_from_filename(stem)
        file_insee = _infer_insee_from_text(stem)

        text = _read_text_best_effort(csv_path)
        dialect = _sniff_dialect(text[:5000])
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        if not reader.fieldnames:
            continue

        headers = list(reader.fieldnames)
        insee_col = force_insee_col or _pick_column(
            headers,
            patterns=[
                r"(^|_)insee($|_)",
                r"codgeo",
                r"code_commune",
                r"commune_code",
                r"code_insee",
            ],
        )
        name_col = force_name_col or _pick_column(
            headers, patterns=[r"(^|_)commune($|_)", r"libelle", r"nom"]
        )
        votes_col = force_votes_col or _pick_column(headers, patterns=[r"voix", r"votes", r"suffrages"])
        pct_col = force_pct_col or _pick_column(headers, patterns=[r"pourcentage", r"pct", r"(^|_)%($|_)"])
        candidate_col = force_candidate_col or _pick_column(
            headers,
            patterns=[
                r"libelle_etendu_liste",
                r"libelle_liste",
                r"nom_liste",
                r"candidat",
                r"nom_candidat",
                r"liste",
            ],
        )
        tour_col = _pick_column(headers, patterns=[r"(^|_)tour($|_)", r"(^|_)round($|_)", r"(^|_)manche($|_)"])
        bureau_col = _pick_column(
            headers,
            patterns=[
                r"(^|_)code_bv($|_)",
                r"(^|_)bv($|_)",
                r"(^|_)bureau($|_)",
                r"num_bv",
                r"bureau_vote",
            ],
        )

        if verbose:
            rel = csv_path.relative_to(folder)
            print(
                f"[{election_label}] {rel.as_posix()}: file_insee={file_insee!r} file_tour={file_tour!r} insee={insee_col!r} candidat={candidate_col!r} voix={votes_col!r} pct={pct_col!r} bv={bureau_col!r} tour={tour_col!r}"
            )

        if not votes_col and not pct_col:
            raise ValueError(
                f"[{election_label}] {csv_path.name}: colonne 'voix' ou '%' introuvable. Colonnes: {headers}"
            )
        if not candidate_col:
            raise ValueError(
                f"[{election_label}] {csv_path.name}: colonne candidat/liste introuvable. Colonnes: {headers}"
            )
        if not insee_col and not file_insee:
            raise ValueError(
                f"[{election_label}] {csv_path.name}: colonne INSEE introuvable et le nom de fichier ne contient pas de code INSEE.\n"
                "Solutions:\n"
                "- Renomme le fichier comme 21231_T1.csv\n"
                "- ou ajoute/indique une colonne INSEE (ex: insee, code_commune)\n"
                '- ou force le nom de colonne avec --force-insee-col "Nom de ta colonne"'
            )

        for row in reader:
            insee = (
                ((row.get(insee_col) or "").strip() if insee_col else "") or (file_insee or "")
            ).strip()
            if not insee:
                continue
            candidate = (row.get(candidate_col) or "").strip()
            if not candidate:
                continue
            votes = _parse_int(row.get(votes_col) if votes_col else None)
            pct = _parse_float(row.get(pct_col) if pct_col else None)
            tour = None
            if tour_col:
                tour = _infer_tour_from_text((row.get(tour_col) or "").strip()) or (row.get(tour_col) or "").strip()
            tour = tour or file_tour
            bureau = (row.get(bureau_col) or "").strip() if bureau_col else ""
            bureau = bureau or None

            raw = {k: (v or "").strip() for k, v in row.items() if k}
            if name_col and raw.get(name_col):
                raw["commune_name"] = raw.get(name_col, "")

            results_by_insee[insee].append(
                ResultRow(
                    candidate=candidate,
                    votes=votes,
                    pct=pct,
                    tour=tour,
                    bureau=bureau,
                    circo=None,
                    raw=raw,
                )
            )

    # Sort by tour (T2 first), then bureau, then votes
    for insee, rows in results_by_insee.items():
        def tour_rank(t: str | None) -> int:
            if t == "T2":
                return 0
            if t == "T1":
                return 1
            return 2

        rows.sort(
            key=lambda r: (
                tour_rank(r.tour),
                r.bureau is None,
                _parse_int(r.bureau) if r.bureau and re.fullmatch(r"\d+", r.bureau) else (10**9),
                (r.bureau or ""),
                r.votes is None,
                -(r.votes or 0),
                (r.pct is None, -(r.pct or 0.0)),
                r.candidate.lower(),
            )
        )
        results_by_insee[insee] = rows

    return results_by_insee


def filter_results_by_insee(
    results_by_insee: dict[str, list[ResultRow]], allowed_insee: set[str] | None
) -> dict[str, list[ResultRow]]:
    if not allowed_insee:
        return results_by_insee
    return {k: v for k, v in results_by_insee.items() if k in allowed_insee}


def keep_latest_tour_only(
    results_by_insee: dict[str, list[ResultRow]],
    *,
    prefer_order: tuple[str, ...] = ("T2", "T1"),
) -> dict[str, list[ResultRow]]:
    """
    For each commune:
    - if at least one row exists for the preferred latest tour (ex: T2), keep only that tour
    - else keep all rows as-is
    """
    out: dict[str, list[ResultRow]] = {}
    for insee, rows in results_by_insee.items():
        tours_present = {r.tour for r in rows if r.tour}
        chosen: str | None = None
        for t in prefer_order:
            if t in tours_present:
                chosen = t
                break
        if chosen:
            out[insee] = [r for r in rows if r.tour == chosen]
        else:
            out[insee] = rows
    return out


def _fmt_votes(v: int | None) -> str:
    if v is None:
        return ""
    return f"{v:,}".replace(",", " ")


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return ""
    # Keep 2 decimals max, trim trailing zeros
    s = f"{p:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def build_html(
    *,
    dept: str,
    geojson: dict[str, Any],
    selected_insee: set[str] | None,
    circo2_insee: set[str] | None,
    legislatives: dict[str, list[ResultRow]],
    municipales: dict[str, list[ResultRow]],
) -> str:
    features: list[dict[str, Any]] = list(geojson.get("features") or [])
    if not features:
        raise ValueError("GeoJSON sans 'features'.")

    bbox = _bbox_for_features(features)
    width = 820.0
    height = 900.0
    margin = 20.0

    # Build commune lookup (insee -> name)
    communes: dict[str, Commune] = {}
    for f in features:
        props = f.get("properties") or {}
        insee = str(props.get("code") or props.get("code_commune") or props.get("insee") or "").strip()
        name = str(props.get("nom") or props.get("name") or props.get("nom_commune") or "").strip()
        if insee:
            communes[insee] = Commune(insee=insee, name=name or insee)

    if selected_insee is None:
        selected_codes = set(legislatives.keys()) | set(municipales.keys())
    else:
        selected_codes = set(selected_insee)

    def is_selected(insee: str) -> bool:
        return insee in selected_codes

    # INSEE -> set of circo codes found in legislatives inputs (C2/C3/...)
    circo_by_insee: dict[str, set[str]] = defaultdict(set)
    for insee_code, rows in legislatives.items():
        for r in rows:
            if r.circo:
                circo_by_insee[insee_code].add(r.circo)

    circo2_codes = set(circo2_insee or set())

    # SVG paths
    commune_paths: list[str] = []
    for f in features:
        props = f.get("properties") or {}
        insee = str(props.get("code") or props.get("code_commune") or props.get("insee") or "").strip()
        if not insee:
            continue
        geom = f.get("geometry") or {}
        d = geometry_to_svg_path(geom, bbox=bbox, width=width, height=height, margin=margin)
        if not d:
            continue

        css_class = "commune"
        if is_selected(insee):
            has_c2 = ("C2" in circo_by_insee.get(insee, set())) or (insee in circo2_codes)
            has_c3 = "C3" in circo_by_insee.get(insee, set())
            if has_c2 and has_c3:
                css_class = "commune selected both"
            elif has_c2:
                css_class = "commune selected circo2"
            else:
                css_class = "commune selected circo3"
        title = communes.get(insee).name if insee in communes else insee
        if is_selected(insee):
            # Special case: Dijon (21231) can be split across C2/C3 in legislatives.
            circo_set = circo_by_insee.get(insee, set())
            if insee == "21231" and ("C2" in circo_set and "C3" in circo_set):
                href = "#commune-21231-split"
            else:
                href = f"#commune-{html.escape(insee)}"
            commune_paths.append(
                f'<a href="{href}" class="commune-link">'
                f'<path id="c{html.escape(insee)}" class="{css_class}" d="{d}"><title>{html.escape(title)}</title></path>'
                f"</a>"
            )
        else:
            commune_paths.append(
                f'<path id="c{html.escape(insee)}" class="{css_class}" d="{d}"><title>{html.escape(title)}</title></path>'
            )

    # Build list of selected pages (for deterministic sections)
    base_selected = [c for code, c in communes.items() if is_selected(code)]
    base_selected.sort(key=lambda c: (c.name.lower(), c.insee))

    selected_pages: list[PageCommune] = []
    for c in base_selected:
        if c.insee == "21231":
            circo_set = circo_by_insee.get("21231", set())
            if "C2" in circo_set and "C3" in circo_set:
                selected_pages.append(
                    PageCommune(
                        page_id="commune-21231-c3",
                        insee="21231",
                        name=c.name,
                        label=f"{c.name} (21231) 3",
                        legis_circo="C3",
                    )
                )
                selected_pages.append(
                    PageCommune(
                        page_id="commune-21231-c2",
                        insee="21231",
                        name=c.name,
                        label=f"{c.name} (21231) 2",
                        legis_circo="C2",
                    )
                )
                continue
        selected_pages.append(
            PageCommune(
                page_id=f"commune-{c.insee}",
                insee=c.insee,
                name=c.name,
                label=f"{c.name} ({c.insee})",
                legis_circo=None,
            )
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _tour_key(t: str | None) -> str:
        return t or ""

    def split_by_tour(rows: list[ResultRow]) -> dict[str, list[ResultRow]]:
        groups: dict[str, list[ResultRow]] = defaultdict(list)
        for r in rows:
            groups[_tour_key(r.tour)].append(r)
        # Prefer T2 then T1 when present
        ordered: dict[str, list[ResultRow]] = {}
        for k in ("T2", "T1"):
            if k in groups:
                ordered[k] = groups.pop(k)
        for k in sorted(groups.keys()):
            ordered[k] = groups[k]
        return ordered

    def split_by_circo(rows: list[ResultRow]) -> dict[str, list[ResultRow]]:
        groups: dict[str, list[ResultRow]] = defaultdict(list)
        for r in rows:
            groups[r.circo or ""].append(r)

        def circo_sort_key(k: str) -> tuple[int, int]:
            if not k:
                return (2, 999)
            m = re.fullmatch(r"C(\d{1,2})", k)
            if m:
                return (0, int(m.group(1)))
            return (1, 999)

        ordered: dict[str, list[ResultRow]] = {}
        for k in sorted(groups.keys(), key=circo_sort_key):
            ordered[k] = groups[k]
        return ordered

    def aggregate_candidate_votes(rows: list[ResultRow]) -> list[tuple[str, int]]:
        totals: dict[str, int] = defaultdict(int)
        for r in rows:
            if r.votes is None:
                continue
            totals[r.candidate] += r.votes
        items = list(totals.items())
        items.sort(key=lambda it: (-it[1], it[0].lower()))
        return items

    def bureau_top2(rows: list[ResultRow]) -> list[tuple[str, tuple[str, int] | None, tuple[str, int] | None]]:
        # bureau -> candidate -> votes
        bureau_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            if not r.bureau or r.votes is None:
                continue
            bureau_totals[r.bureau][r.candidate] += r.votes

        def bureau_sort_key(b: str) -> tuple[int, str]:
            if re.fullmatch(r"\d+", b):
                return (0, f"{int(b):05d}")
            return (1, b.lower())

        out: list[tuple[str, tuple[str, int] | None, tuple[str, int] | None]] = []
        for bv in sorted(bureau_totals.keys(), key=bureau_sort_key):
            items = list(bureau_totals[bv].items())
            items.sort(key=lambda it: (-it[1], it[0].lower()))
            first = items[0] if len(items) >= 1 else None
            second = items[1] if len(items) >= 2 else None
            out.append((bv, first, second))
        return out

    def election_summary(rows: list[ResultRow]) -> dict[str, str]:
        if not rows:
            return {"winner": "—", "best": "—", "total": "—", "count": "0"}

        if len({r.circo for r in rows if r.circo}) > 1:
            # Avoid misleading KPI when mixing multiple circonscriptions (e.g. Dijon split).
            return {"winner": "—", "best": "Multi-circo", "total": "—", "count": "—"}

        tours = split_by_tour(rows)
        # Pick the first tour in split order (T2 > T1 > others)
        tour_rows = next(iter(tours.values()))

        totals = aggregate_candidate_votes(tour_rows)
        if totals:
            winner_name, winner_votes = totals[0]
            best_value = f"{_fmt_votes(winner_votes)} voix"
            total_votes = sum(v for _, v in totals)
            count = len(totals)
            return {
                "winner": winner_name,
                "best": best_value,
                "total": _fmt_votes(total_votes) if total_votes else "—",
                "count": str(count),
            }

        # Fallback to pct when votes absent
        winner: ResultRow | None = None
        if any(r.pct is not None for r in tour_rows):
            winner = max(tour_rows, key=lambda r: (r.pct is None, r.pct or -1.0))
        if winner is None:
            return {"winner": tour_rows[0].candidate, "best": "—", "total": "—", "count": str(len(tour_rows))}
        return {
            "winner": winner.candidate,
            "best": f"{_fmt_pct(winner.pct)} %" if winner.pct is not None else "—",
            "total": "—",
            "count": str(len({r.candidate for r in tour_rows})),
        }

    def results_table(rows: list[ResultRow], *, aggregate: bool) -> str:
        if not rows:
            return "<p class=\"muted\">Aucun résultat trouvé dans les CSV pour cette commune.</p>"
        show_tour = any(r.tour for r in rows) and not aggregate
        out = [
            "<table>",
            "<thead><tr>"
            + ("<th>Tour</th>" if show_tour else "")
            + "<th>Candidat</th><th class=\"num\">Voix</th><th class=\"num\">%</th></tr></thead>",
            "<tbody>",
        ]
        if aggregate and any(r.votes is not None for r in rows):
            # Sum votes across bureaux for a "commune total"
            for cand, v in aggregate_candidate_votes(rows):
                out.append(
                    "<tr>"
                    + f"<td>{html.escape(cand)}</td>"
                    + f"<td class=\"num\">{html.escape(_fmt_votes(v))}</td>"
                    + "<td class=\"num\"></td>"
                    + "</tr>"
                )
        else:
            for r in rows:
                out.append(
                    "<tr>"
                    + (f"<td>{html.escape(r.tour or '')}</td>" if show_tour else "")
                    + f"<td>{html.escape(r.candidate)}</td>"
                    + f"<td class=\"num\">{html.escape(_fmt_votes(r.votes))}</td>"
                    + f"<td class=\"num\">{html.escape(_fmt_pct(r.pct))}</td>"
                    + "</tr>"
                )
        out.append("</tbody></table>")
        return "\n".join(out)

    def election_body(rows: list[ResultRow]) -> list[str]:
        tour_groups = split_by_tour(rows) if rows else {}
        body: list[str] = []

        has_bureaux = any(r.bureau for r in rows)
        if has_bureaux:
            for tour, tour_rows in (tour_groups.items() if tour_groups else [("", rows)]):
                if tour:
                    body.append(f"<div class=\"tour-title\">{html.escape(tour)}</div>")

                tops = bureau_top2(tour_rows)
                if tops:
                    body.append("<div class=\"block-title\">Par bureau de vote</div>")
                    body.append("<table>")
                    body.append(
                        "<thead><tr><th>Bureau</th><th>1er</th><th class=\"num\">Voix</th><th>2e</th><th class=\"num\">Voix</th></tr></thead>"
                    )
                    body.append("<tbody>")
                    for bv, first, second in tops:
                        f_name, f_votes = first if first else ("—", 0)
                        s_name, s_votes = second if second else ("—", 0)
                        body.append(
                            "<tr>"
                            f"<td>{html.escape(bv)}</td>"
                            f"<td>{html.escape(f_name)}</td>"
                            f"<td class=\"num\">{html.escape(_fmt_votes(f_votes) if first else '')}</td>"
                            f"<td>{html.escape(s_name)}</td>"
                            f"<td class=\"num\">{html.escape(_fmt_votes(s_votes) if second else '')}</td>"
                            "</tr>"
                        )
                    body.append("</tbody></table>")
                body.append("<div class=\"block-title\">Total commune (somme des bureaux)</div>")
                body.append(results_table(tour_rows, aggregate=True))
        else:
            if len(tour_groups) >= 2:
                for tour, tour_rows in tour_groups.items():
                    body.append(f"<div class=\"tour-title\">{html.escape(tour)}</div>")
                    body.append(results_table(tour_rows, aggregate=False))
            else:
                body.append(results_table(rows, aggregate=False))
        return body

    def election_block(title: str, subtitle: str, rows: list[ResultRow]) -> str:
        s = election_summary(rows)
        body: list[str] = []

        circo_groups = split_by_circo(rows) if rows else {}
        if len([k for k in circo_groups.keys() if k]) >= 2:
            # Special case: same commune (e.g. Dijon 21231) appears in multiple circonscriptions.
            for k, group_rows in circo_groups.items():
                if not k:
                    continue
                body.append(f"<div class=\"circo-title\">Circonscription {html.escape(k[1:])}</div>")
                body.extend(election_body(group_rows))
        else:
            body = election_body(rows)
        return "\n".join(
            [
                "<article class=\"election\">",
                "<div class=\"election-head\">",
                f"<h3>{html.escape(title)}</h3>",
                f"<div class=\"muted\">{html.escape(subtitle)}</div>",
                "</div>",
                "<div class=\"kpis\">",
                "<div class=\"kpi\">"
                "<div class=\"kpi-label\">1er</div>"
                f"<div class=\"kpi-value\">{html.escape(s['winner'])}</div>"
                f"<div class=\"kpi-sub\">{html.escape(s['best'])}</div>"
                "</div>",
                "<div class=\"kpi\">"
                "<div class=\"kpi-label\">Candidats</div>"
                f"<div class=\"kpi-value\">{html.escape(s['count'])}</div>"
                "<div class=\"kpi-sub\">dans tes CSV</div>"
                "</div>",
                "<div class=\"kpi\">"
                "<div class=\"kpi-label\">Total voix</div>"
                f"<div class=\"kpi-value\">{html.escape(s['total'])}</div>"
                "<div class=\"kpi-sub\">si colonne voix</div>"
                "</div>",
                "</div>",
                "<div class=\"table-wrap\">",
                "\n".join(body),
                "</div>",
                "</article>",
            ]
        )

    # HTML doc
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="fr"><head><meta charset="utf-8">')
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append(f"<title>Côte-d'Or — Résultats par commune</title>")
    parts.append("<script>document.documentElement.classList.add('js');</script>")
    parts.append(
        "<style>"
        ":root{"
        "--bg:#f6f7fb;"
        "--card:#ffffff;"
        "--text:#0f172a;"
        "--muted:#64748b;"
        "--border:rgba(15,23,42,.10);"
        "--shadow:0 12px 30px rgba(2,6,23,.08);"
        "--shadow2:0 6px 18px rgba(2,6,23,.06);"
        "--primary:#2563eb;"
        "--primary-2:#1d4ed8;"
        "--focus:0 0 0 4px rgba(37,99,235,.18);"
        "}"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;color:var(--text);background:var(--bg);}"
        "header{padding:20px 24px;border-bottom:1px solid var(--border);background:linear-gradient(180deg,#fff,rgba(255,255,255,.92));backdrop-filter:saturate(1.2) blur(6px);}"
        "h1{margin:0;font-size:18px;letter-spacing:-0.01em;}"
        ".sub{margin-top:6px;color:var(--muted);font-size:12px;}"
        ".page{padding:18px 24px;max-width:1200px;margin:0 auto;}"
        "#map-view{display:block;}"
        "#viewer{display:block;}"
        ".js #viewer{display:none;}"
        ".map-wrap{display:grid;grid-template-columns:1fr 320px;gap:18px;align-items:start;}"
        ".legend{border:1px solid var(--border);border-radius:16px;padding:14px;background:var(--card);box-shadow:var(--shadow2);}"
        ".legend h2{margin:0 0 10px 0;font-size:14px;}"
        ".legend ul{margin:0;padding-left:16px;}"
        ".legend li{margin:6px 0;font-size:12px;}"
        ".pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;background:#f2f2f2;font-size:11px;color:#334155;border:1px solid rgba(2,6,23,.10);}"
        ".pill.c3{background:#ffd54a;border-color:#e0bf3e;}"
        ".pill.c2{background:#5bbcff;border-color:#2ea8ff;color:#00324f;}"
        ".pill.both{background:#b388ff;border-color:#9a66ff;color:#2a0a5a;}"
        ".pill.gray{background:#f4f4f4;border-color:#dddddd;color:#333;}"
        "svg{display:block;width:100%;height:auto;max-height:72vh;border:1px solid var(--border);border-radius:18px;background:radial-gradient(1200px 800px at 15% 20%,#ffffff,#f8fafc 60%,#f1f5f9);box-shadow:var(--shadow2);}"
        ".commune{fill:#f4f4f4;stroke:rgba(100,116,139,.55);stroke-width:0.7;}"
        ".commune.selected{stroke-width:1.0;}"
        ".commune.selected.circo3{fill:#ffd54a;stroke:#8a6b00;}"
        ".commune.selected.circo2{fill:#5bbcff;stroke:#004c7a;}"
        ".commune.selected.both{fill:#b388ff;stroke:#4a1a8a;}"
        ".commune-link{cursor:pointer;}"
        ".commune-link:hover .commune.selected.circo3{fill:#ffbf00;}"
        ".commune-link:hover .commune.selected.circo2{fill:#2ea8ff;}"
        ".commune-link:hover .commune.selected.both{fill:#9a66ff;}"
        ".toc{margin-top:12px;font-size:12px;border-top:1px dashed rgba(2,6,23,.12);padding-top:10px;}"
        ".toc summary{cursor:pointer;list-style:none;font-weight:700;color:#334155;user-select:none;}"
        ".toc summary::-webkit-details-marker{display:none;}"
        ".toc .list{margin-top:10px;max-height:460px;overflow:auto;}"
        ".toc a{color:var(--primary);text-decoration:none;}"
        ".toc a:hover{text-decoration:underline;}"
        "section.commune-page{page-break-before:always;}"
        ".js section.commune-page{display:none;}"
        ".js section.commune-page.active{display:block;}"
        "h2{margin:0;font-size:18px;}"
        "h3{margin:0;font-size:14px;}"
        ".muted{color:var(--muted);font-size:12px;}"
        ".commune-top{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:12px;}"
        ".commune-top .meta{margin-top:4px;}"
        ".commune-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}"
        ".btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:9px 12px;border:1px solid var(--border);border-radius:12px;background:var(--card);font-size:12px;color:var(--primary);text-decoration:none;box-shadow:0 1px 0 rgba(2,6,23,.04);transition:transform .08s ease,background .12s ease,border-color .12s ease,box-shadow .12s ease;}"
        ".btn:hover{border-color:rgba(37,99,235,.35);background:#f7f9ff;box-shadow:var(--shadow2);}"
        ".btn:active{transform:translateY(1px);}"
        ".btn:focus{outline:none;box-shadow:var(--shadow2),var(--focus);border-color:rgba(37,99,235,.45);}"
        ".select{padding:9px 12px;border:1px solid var(--border);border-radius:12px;background:var(--card);font-size:12px;min-width:260px;box-shadow:0 1px 0 rgba(2,6,23,.04);}"
        ".select:focus{outline:none;box-shadow:var(--shadow2),var(--focus);border-color:rgba(37,99,235,.45);}"
        ".viewer-bar{position:sticky;top:0;z-index:5;background:rgba(246,247,251,.88);backdrop-filter:blur(10px) saturate(1.15);border-bottom:1px solid var(--border);padding:12px 24px;}"
        ".viewer-bar .inner{max-width:1200px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;}"
        ".viewer-bar .title{font-size:12px;color:#334155;font-weight:700;letter-spacing:.02em;text-transform:uppercase;}"
        ".elections{display:grid;grid-template-columns:1fr 1fr;gap:16px;}"
        ".election{border:1px solid var(--border);border-radius:18px;background:var(--card);overflow:hidden;box-shadow:var(--shadow2);}"
        ".election-head{padding:12px 12px 0 12px;}"
        ".kpis{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:12px;}"
        ".kpi{border:1px solid rgba(2,6,23,.08);border-radius:14px;padding:10px;background:linear-gradient(180deg,#f8fafc,#ffffff);min-height:64px;}"
        ".kpi-label{font-size:11px;color:#666;}"
        ".kpi-value{margin-top:4px;font-size:13px;font-weight:650;line-height:1.2;}"
        ".kpi-sub{margin-top:3px;font-size:11px;color:#666;}"
        ".table-wrap{padding:0 12px 12px 12px;}"
        ".circo-title{margin:10px 0 6px 0;font-size:12px;color:#111;font-weight:750;}"
        ".block-title{margin:10px 0 6px 0;font-size:11px;color:#333;font-weight:650;}"
        ".tour-title{margin:10px 0 6px 0;font-size:11px;color:#666;font-weight:650;text-transform:uppercase;letter-spacing:.02em;}"
        "table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px;border:1px solid rgba(2,6,23,.08);border-radius:14px;overflow:hidden;background:#fff;}"
        "th,td{padding:8px 10px;border-bottom:1px solid rgba(2,6,23,.06);}"
        "tr:last-child td{border-bottom:none;}"
        "th{text-align:left;color:#334155;background:#f8fafc;border-bottom:1px solid rgba(2,6,23,.08);}"
        "td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}"
        "@media (max-width: 860px){"
        "header{padding:16px 14px;}"
        ".page{padding:14px 14px;}"
        ".map-wrap{grid-template-columns:1fr;gap:12px;}"
        ".legend{border-radius:16px;}"
        "svg{border-radius:16px;}"
        "svg{max-height:58vh;}"
        ".legend ul{columns:2;column-gap:14px;}"
        ".toc .list{max-height:260px;}"
        ".viewer-bar{padding:10px 12px;}"
        ".viewer-bar .inner{gap:8px;}"
        ".select{min-width:unset;width:100%;}"
        ".commune-actions{width:100%;}"
        ".btn{width:100%;}"
        ".elections{grid-template-columns:1fr;}"
        ".kpis{grid-template-columns:1fr;}"
        "th,td{padding:8px 8px;}"
        "}"
        "@media (max-width: 420px){"
        "h1{font-size:16px;}"
        ".legend ul{padding-left:14px;}"
        "}"
        "@media print{"
        "header{position:running(header)}"
        "body{background:#fff;}"
        ".page{padding:0.6cm 0.8cm;max-width:none;}"
        ".map-wrap{grid-template-columns:1fr 6.8cm;gap:0.5cm;}"
        ".elections{grid-template-columns:1fr;gap:0.35cm;}"
        ".btn{border-color:#ddd;background:#fff;}"
        "#map-view{display:block !important;}"
        "#viewer{display:block !important;}"
        "section.commune-page{display:block !important;}"
        "svg{max-height:none;border:1px solid #ddd;background:#fff;box-shadow:none;}"
        ".toc .list{max-height:none;overflow:visible;}"
        "a{color:inherit;text-decoration:none;}"
        "}"
        "</style>"
    )
    parts.append("</head><body>")
    parts.append("<div id=\"map-view\">")
    parts.append("<header id=\"carte\">")
    parts.append("<h1>Côte-d'Or (21) — Carte cliquable + fiches communes</h1>")
    parts.append(
        f"<div class=\"sub\">Généré le {html.escape(now)}. Clique une commune ou choisis dans la liste.</div>"
    )
    parts.append("</header>")

    parts.append("<div class=\"page\">")
    parts.append("<div class=\"map-wrap\">")
    parts.append(
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" xmlns="http://www.w3.org/2000/svg">'
        + "\n".join(commune_paths)
        + "</svg>"
    )
    parts.append("<aside class=\"legend\">")
    parts.append("<h2>Légende</h2>")
    parts.append("<ul>")
    if selected_insee is None:
        parts.append("<li><span class=\"pill\">Couleur</span> = circonscription (communes cliquables)</li>")
    else:
        parts.append("<li><span class=\"pill\">Couleur</span> = circonscription (communes sélectionnées)</li>")
    parts.append("<li><span class=\"pill c3\">Circo 3</span></li>")
    parts.append("<li><span class=\"pill c2\">Circo 2</span></li>")
    parts.append("<li><span class=\"pill both\">Commune partagée</span> (ex: Dijon)</li>")
    parts.append("<li><span class=\"pill gray\">Autres</span> = non cliquables</li>")
    parts.append("</ul>")
    parts.append("<details class=\"toc\" id=\"tocDetails\" open>")
    parts.append(f"<summary>Communes ({len(selected_pages)})</summary>")
    parts.append("<div class=\"list\">")
    if not selected_pages:
        parts.append(
            "<p class=\"muted\">Aucune commune cliquable (pas de sélection et/ou pas de résultats CSV chargés).</p>"
        )
        parts.append(
            f"<p class=\"muted\">Astuce: crée {html.escape(str(DATA_DIR / 'communes_selection.csv'))} (colonne INSEE) pour forcer la liste.</p>"
        )
    else:
        parts.append("<ul>")
        for p in selected_pages:
            parts.append(f"<li><a href=\"#{html.escape(p.page_id)}\">{html.escape(p.label)}</a></li>")
        parts.append("</ul>")
    parts.append("</div>")
    parts.append("</details>")
    parts.append("</aside>")
    parts.append("</div>")
    parts.append("</div>")
    parts.append("</div>")

    # Viewer (single commune at a time in screen mode)
    parts.append("<div id=\"viewer\">")
    parts.append("<div class=\"viewer-bar\">")
    parts.append("<div class=\"inner\">")
    parts.append("<div class=\"title\">Fiche commune</div>")
    parts.append("<div class=\"commune-actions\">")
    parts.append("<select id=\"communeSelect\" class=\"select\"></select>")
    parts.append("<a class=\"btn\" href=\"#carte\" id=\"backToMap\">Retour à la carte</a>")
    parts.append("</div>")
    parts.append("</div>")
    parts.append("</div>")

    # Optional Dijon split chooser section
    dijon_split = (
        any(p.insee == "21231" and p.legis_circo in {"C2", "C3"} for p in selected_pages)
        and ("C2" in circo_by_insee.get("21231", set()) and "C3" in circo_by_insee.get("21231", set()))
    )
    if dijon_split:
        parts.append("<section class=\"commune-page\" id=\"commune-21231-split\">")
        parts.append("<div class=\"page\">")
        parts.append("<div class=\"commune-top\">")
        parts.append(
            "<div><h2>Dijon <span class=\"muted\">(21231)</span></h2>"
            "<div class=\"meta muted\">Dijon est partagée entre les circonscriptions 2 et 3. Choisis la fiche législatives à afficher (les municipales restent “tout Dijon”).</div></div>"
        )
        parts.append("<div class=\"commune-actions\">")
        parts.append("<a class=\"btn\" href=\"#carte\">Retour à la carte</a>")
        parts.append("</div>")
        parts.append("</div>")
        parts.append("<div class=\"elections\">")
        parts.append(
            "<article class=\"election\"><div class=\"election-head\"><h3>Fiches Dijon</h3>"
            "<div class=\"muted\">Clique une fiche :</div></div>"
            "<div class=\"table-wrap\">"
            "<p><a class=\"btn\" href=\"#commune-21231-c3\">Dijon (21231) 3</a> "
            "<a class=\"btn\" href=\"#commune-21231-c2\">Dijon (21231) 2</a></p>"
            "</div></article>"
        )
        parts.append("</div>")
        parts.append("</div>")
        parts.append("</section>")

    # Commune pages
    for p in selected_pages:
        parts.append(f"<section class=\"commune-page\" id=\"{html.escape(p.page_id)}\">")
        parts.append("<div class=\"page\">")
        parts.append("<div class=\"commune-top\">")
        circo_info = ""
        if p.legis_circo:
            circo_info = f" Circonscription: {html.escape(p.legis_circo)}."
        else:
            circo_set = {r.circo for r in legislatives.get(p.insee, []) if r.circo}
            if len(circo_set) == 1:
                circo_info = f" Circonscription: {html.escape(next(iter(circo_set)))}."
            elif len(circo_set) >= 2:
                circo_nums = sorted(
                    [c[1:] for c in circo_set if c and re.fullmatch(r"C\d{1,2}", c)],
                    key=lambda x: int(x),
                )
                if circo_nums:
                    circo_info = f" Circonscriptions: {html.escape(', '.join(circo_nums))}."
                else:
                    circo_info = " Circonscriptions multiples."
        parts.append(
            f"<div><h2>{html.escape(p.name)} <span class=\"muted\">({html.escape(p.insee)})</span></h2>"
            f"<div class=\"meta muted\">Fiche commune — données organisées par élection."
            + circo_info
            + "</div></div>"
        )
        parts.append("<div class=\"commune-actions\">")
        parts.append("<a class=\"btn\" href=\"#carte\">Retour à la carte</a>")
        parts.append("</div>")
        parts.append("</div>")

        parts.append("<div class=\"elections\">")
        legis_rows = legislatives.get(p.insee, [])
        if p.legis_circo:
            legis_rows = [r for r in legis_rows if r.circo == p.legis_circo]
        parts.append(
            election_block(
                "Législatives 2024",
                "Classement des candidats (trié par voix si dispo).",
                legis_rows,
            )
        )
        parts.append(
            election_block(
                "Municipales 2026",
                "Classement des candidats (trié par voix si dispo).",
                municipales.get(p.insee, []),
            )
        )
        parts.append("</div>")
        parts.append("</div>")
        parts.append("</section>")

    parts.append("</div>")  # viewer

    # JS: show only one fiche at a time (screen). Print still shows everything.
    parts.append("<script>")
    parts.append("(function(){")
    parts.append("const mapView=document.getElementById('map-view');")
    parts.append("const viewer=document.getElementById('viewer');")
    parts.append("const select=document.getElementById('communeSelect');")
    parts.append("const sections=[...document.querySelectorAll('section.commune-page')];")
    parts.append("const ids=sections.map(s=>s.id);")
    parts.append("function labelFor(id){const h=document.querySelector('#'+CSS.escape(id)+' h2');return h?h.textContent.trim():id;}")
    parts.append("function fillSelect(){if(!select) return; select.innerHTML='';")
    parts.append("const opt0=document.createElement('option'); opt0.value='carte'; opt0.textContent='— choisir une commune —'; select.appendChild(opt0);")
    parts.append("ids.filter(id=>!id.endsWith('-split')).forEach(id=>{const o=document.createElement('option'); o.value=id; o.textContent=labelFor(id); select.appendChild(o);});")
    parts.append("}")
    parts.append("function showMap(){sections.forEach(s=>s.classList.remove('active')); if(viewer) viewer.style.display='none'; if(mapView) mapView.style.display='block'; if(select) select.value='carte'; window.scrollTo(0,0);}")
    parts.append("function showCommune(id){sections.forEach(s=>s.classList.toggle('active', s.id===id)); if(mapView) mapView.style.display='none'; if(viewer) viewer.style.display='block'; if(select) select.value=id; window.scrollTo(0,0);}")
    parts.append("function onHash(){const h=(location.hash||'').replace('#',''); if(h && h.startsWith('commune-') && ids.includes(h)){showCommune(h);} else {showMap();}}")
    parts.append("fillSelect();")
    parts.append("const toc=document.getElementById('tocDetails');")
    parts.append("function syncToc(){if(!toc) return; if(window.matchMedia('(max-width: 860px)').matches){toc.removeAttribute('open');} else {toc.setAttribute('open','');}}")
    parts.append("window.addEventListener('resize', syncToc);")
    parts.append("syncToc();")
    parts.append("if(select){select.addEventListener('change', ()=>{const v=select.value; location.hash=(v==='carte'?'#carte':'#'+v);});}")
    parts.append("window.addEventListener('hashchange', onHash);")
    parts.append("onHash();")
    parts.append("})();")
    parts.append("</script>")

    parts.append("</body></html>")
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Génère un HTML imprimable en PDF: carte des communes (cliquable) + fiches résultats."
    )
    parser.add_argument("--dept", default=DEFAULT_DEPT_CODE, help="Code département (défaut: 21)")
    parser.add_argument("--geojson", default="", help="Chemin GeoJSON local (optionnel)")
    parser.add_argument(
        "--selection",
        default=str(DATA_DIR / "communes_selection.csv"),
        help="CSV des communes sélectionnées (colonne INSEE).",
    )
    parser.add_argument(
        "--circo2-insee",
        default=str(DATA_DIR / "circo2_communes.csv"),
        help="CSV listant les communes (INSEE) à colorer en circo 2 sur la carte (optionnel).",
    )
    parser.add_argument(
        "--legislatives",
        default=str(DATA_DIR / "legislatives2024"),
        help="Dossier contenant 1 CSV par candidat (Législatives 2024).",
    )
    parser.add_argument(
        "--municipales",
        default=str(DATA_DIR / "municipales2026"),
        help="Dossier contenant 1 CSV par candidat (Municipales 2026).",
    )
    parser.add_argument(
        "--municipales-layout",
        default="auto",
        choices=["auto", "per_candidate", "per_commune"],
        help="Organisation des CSV Municipales: auto, per_candidate (1 CSV par candidat), per_commune (1 CSV par commune et tour).",
    )
    parser.add_argument(
        "--municipales-scope",
        default="auto",
        choices=["auto", "selection", "legislatives", "all"],
        help="Filtre les municipales aux communes d'intérêt: auto (selection sinon communes présentes en législatives), selection, legislatives, all.",
    )
    parser.add_argument(
        "--municipales-tour-policy",
        default="latest",
        choices=["latest", "both"],
        help="Affichage des municipales quand T1 et T2 existent: latest (ne garde que T2), both (garde T1+T2).",
    )
    parser.add_argument(
        "--out",
        default=str(DIST_DIR / "index.html"),
        help="Chemin de sortie HTML.",
    )
    parser.add_argument(
        "--force-insee-col",
        default="",
        help="Force le nom de la colonne INSEE (appliqué à tous les CSV).",
    )
    parser.add_argument(
        "--force-commune-col",
        default="",
        help="Force le nom de la colonne commune (optionnel, appliqué à tous les CSV).",
    )
    parser.add_argument(
        "--force-voix-col",
        default="",
        help="Force le nom de la colonne voix (optionnel, appliqué à tous les CSV).",
    )
    parser.add_argument(
        "--force-pct-col",
        default="",
        help="Force le nom de la colonne pourcentage (optionnel, appliqué à tous les CSV).",
    )
    parser.add_argument(
        "--force-candidat-col",
        default="",
        help="Force le nom de la colonne candidat (optionnel, sinon nom de fichier).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Affiche les colonnes détectées pour chaque CSV.",
    )
    args = parser.parse_args(argv)

    dept = str(args.dept).strip()
    geojson_path = Path(args.geojson) if args.geojson else None
    force_insee_col = (args.force_insee_col or "").strip() or None
    force_name_col = (args.force_commune_col or "").strip() or None
    force_votes_col = (args.force_voix_col or "").strip() or None
    force_pct_col = (args.force_pct_col or "").strip() or None
    force_candidate_col = (args.force_candidat_col or "").strip() or None

    try:
        selected = load_communes_selection(Path(args.selection))
        circo2_insee = load_insee_list(Path(args.circo2_insee))
        geojson = load_geojson_communes(dept, geojson_path)
        legislatives = load_results_from_folder_with_overrides(
            Path(args.legislatives),
            "Législatives 2024",
            force_insee_col=force_insee_col,
            force_name_col=force_name_col,
            force_votes_col=force_votes_col,
            force_pct_col=force_pct_col,
            force_candidate_col=force_candidate_col,
            verbose=bool(args.verbose),
        )
        municipales_folder = Path(args.municipales)
        municipales_layout = str(args.municipales_layout)
        if municipales_layout == "auto":
            # If filenames look like INSEE + T1/T2, assume per_commune
            sample = next(iter(sorted(p for p in municipales_folder.rglob("*.csv") if p.is_file())), None)
            if sample and _infer_insee_from_text(sample.stem) and _infer_tour_from_filename(sample.stem):
                municipales_layout = "per_commune"
            else:
                municipales_layout = "per_candidate"

        if municipales_layout == "per_commune":
            municipales = load_results_per_commune_files(
                municipales_folder,
                "Municipales 2026",
                force_insee_col=force_insee_col,
                force_name_col=force_name_col,
                force_votes_col=force_votes_col,
                force_pct_col=force_pct_col,
                force_candidate_col=force_candidate_col,
                verbose=bool(args.verbose),
            )
        else:
            municipales = load_results_from_folder_with_overrides(
                municipales_folder,
                "Municipales 2026",
                force_insee_col=force_insee_col,
                force_name_col=force_name_col,
                force_votes_col=force_votes_col,
                force_pct_col=force_pct_col,
                force_candidate_col=force_candidate_col,
                verbose=bool(args.verbose),
            )

        municipales_scope = str(args.municipales_scope)
        if municipales_scope == "auto":
            allowed_munic = selected if selected is not None else set(legislatives.keys())
        elif municipales_scope == "selection":
            allowed_munic = selected
        elif municipales_scope == "legislatives":
            allowed_munic = set(legislatives.keys())
        else:
            allowed_munic = None
        municipales = filter_results_by_insee(municipales, allowed_munic)
        if str(args.municipales_tour_policy) == "latest":
            municipales = keep_latest_tour_only(municipales, prefer_order=("T2", "T1"))

        html_doc = build_html(
            dept=dept,
            geojson=geojson,
            selected_insee=selected,
            circo2_insee=circo2_insee,
            legislatives=legislatives,
            municipales=municipales,
        )
    except Exception as e:
        print(f"Erreur: {e}", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"OK: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

