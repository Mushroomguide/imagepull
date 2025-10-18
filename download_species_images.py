#!/usr/bin/env python3
"""Download checklist images for selected mushroom species.

This script queries the Danmarks Svampeatlas checklist API for the
specified species names and downloads all associated taxon images.
Images are stored under ``species_images/<species-slug>/`` where the
slug is derived from the scientific name.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://svampe.databasen.org/api"
IMAGE_INCLUDE_PARAMS = {
    "include[0][model]": "TaxonImages",
    "include[0][as]": "images",
}
SPECIES_NAMES = [
    "Psilocybe semilanceata",
    "Psilocybe cyanescens",
    "Panaeolus cinctulus",
    "Amanita pantherina",
    "Amanita muscaria",
    "Psilocybe strictipes",
    "Panaeolina foenisecii",
    "Panaeolus papilionaceus",
    "Panaeolus semiovatus",
    "Panaeolus acuminatus",
    "Panaeolus subfirmus",
    "Amanita excelsa (syn. Amanita spissa)",
    "Amanita rubescens",
    "Amanita caesarea",
    "Amanita regalis",
]
OUTPUT_ROOT = Path("species_images")
REQUEST_TIMEOUT = 30


@dataclass
class TaxonMatch:
    """Represents a resolved taxon entry from the API."""

    id: int
    full_name: str
    accepted_id: int

    @property
    def effective_id(self) -> int:
        return self.accepted_id or self.id


def slugify(name: str) -> str:
    """Convert a scientific name into a filesystem-friendly slug."""

    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_candidate_names(raw_name: str) -> List[str]:
    """Generate candidate scientific names from the provided label."""

    raw_name = raw_name.strip()
    candidates: List[str] = []
    base = raw_name

    if "(" in raw_name and ")" in raw_name:
        base = raw_name.split("(", 1)[0].strip()
        if base:
            candidates.append(base)

        inside = raw_name.split("(", 1)[1].split(")", 1)[0]
        cleaned = inside.replace("syn.", "")
        for part in re.split(r",|/|;", cleaned):
            part = part.strip()
            if part:
                candidates.append(part)
    else:
        candidates.append(base)

    if raw_name not in candidates:
        candidates.append(raw_name)

    # Deduplicate while preserving order
    seen = set()
    ordered: List[str] = []
    for name in candidates:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(name)
    return ordered


def query_taxa(where: Dict) -> List[Dict]:
    params = {
        "nocount": "true",
        "where": json.dumps(where, separators=(",", ":")),
    }
    return fetch_json(f"{BASE_URL}/taxa", params)


def resolve_taxon(name: str) -> Optional[TaxonMatch]:
    """Find a taxon by name, falling back to partial matches."""

    candidates = parse_candidate_names(name)
    for candidate in candidates:
        where = {"RankID": 10000, "FullName": candidate}
        results = query_taxa(where)
        match = _select_best_match(results, candidate)
        if match:
            return match

    for candidate in candidates:
        where = {"RankID": 10000, "FullName": {"like": f"%{candidate}%"}}
        results = query_taxa(where)
        match = _select_best_match(results, candidate)
        if match:
            return match

    return None


def _select_best_match(results: Iterable[Dict], candidate: str) -> Optional[TaxonMatch]:
    candidate_lower = candidate.lower()
    best: Optional[TaxonMatch] = None

    for item in results:
        full_name = item.get("FullName", "")
        match = TaxonMatch(
            id=item.get("_id"),
            full_name=full_name,
            accepted_id=item.get("accepted_id") or item.get("_id"),
        )
        if full_name.lower() == candidate_lower:
            return match
        if best is None:
            best = match
    return best


def fetch_taxon_images(taxon_id: int) -> List[Dict]:
    payload = fetch_json(
        f"{BASE_URL}/taxa/{taxon_id}",
        IMAGE_INCLUDE_PARAMS,
    )
    return payload.get("images", [])


def download_image(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_bytes(url)
    destination.write_bytes(data)


def filename_from_url(url: str, index: int) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if not name:
        name = f"image_{index}"
    return name


def fetch_json(url: str, params: Optional[Dict[str, str]] = None) -> Dict:
    data = fetch_bytes(url, params, headers={"Accept": "application/json"})
    return json.loads(data.decode("utf-8"))


def fetch_bytes(
    url: str,
    params: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    if params:
        query = urlencode(params, doseq=True)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def main() -> int:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    failures: List[str] = []

    for species in SPECIES_NAMES:
        print(f"Processing: {species}")
        taxon = resolve_taxon(species)
        if not taxon:
            print(f"  ! Unable to resolve taxon for '{species}'")
            failures.append(species)
            continue

        print(f"  Resolved to taxon #{taxon.effective_id} ({taxon.full_name})")
        images = fetch_taxon_images(taxon.effective_id)
        if not images:
            print("  ! No images found")
            continue

        target_dir = OUTPUT_ROOT / slugify(taxon.full_name)
        existing_names = set()
        for index, image in enumerate(images, start=1):
            url = image.get("uri")
            if not url:
                continue
            filename = filename_from_url(url, index)
            if filename in existing_names:
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                filename = f"{stem}_{index}{suffix}"
            existing_names.add(filename)
            destination = target_dir / filename
            try:
                download_image(url, destination)
            except HTTPError as exc:  # pragma: no cover - network issues
                print(f"    ! Failed to download {url}: {exc}")
                continue
            except URLError as exc:  # pragma: no cover - network issues
                print(f"    ! Error downloading {url}: {exc}")
                continue
            print(f"    Saved {destination}")

    if failures:
        print("\nThe following species could not be resolved:")
        for name in failures:
            print(f"  - {name}")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
