#!/usr/bin/env python3
"""Download GBIF images for selected mushroom species.

This script queries the GBIF API for a curated set of mushroom
species and downloads up to ``IMAGES_PER_SPECIES`` images for each
species. Images are saved below ``gbif_images/<species-slug>/`` with
slugified scientific names derived from the matched GBIF entry.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

GBIF_API_ROOT = "https://api.gbif.org/v1"
REQUEST_TIMEOUT = 30
IMAGES_PER_SPECIES = 25
OCCURRENCE_PAGE_SIZE = 300
OUTPUT_ROOT = Path("gbif_images")

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


@dataclass
class SpeciesMatch:
    """Represents a matched GBIF taxon."""

    key: int
    scientific_name: str


def slugify(name: str) -> str:
    """Create a filesystem-friendly slug from a scientific name."""

    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_candidate_names(raw_name: str) -> List[str]:
    """Expand compound species labels into candidate scientific names."""

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

    seen = set()
    ordered: List[str] = []
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def fetch_json(url: str, params: Optional[Dict[str, str]] = None) -> Dict:
    data = fetch_bytes(url, params=params, headers={"Accept": "application/json"})
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


def match_species(name: str) -> Optional[SpeciesMatch]:
    """Resolve the provided label to a GBIF taxon."""

    candidates = parse_candidate_names(name)
    for candidate in candidates:
        payload = fetch_json(
            f"{GBIF_API_ROOT}/species/match",
            {"name": candidate},
        )
        key = payload.get("acceptedUsageKey") or payload.get("usageKey")
        match_type = payload.get("matchType")
        if key and match_type and match_type.upper() != "NONE":
            scientific = payload.get("scientificName") or candidate
            return SpeciesMatch(key=key, scientific_name=scientific)
    return None


def fetch_media_urls(taxon_key: int, limit: int) -> List[str]:
    """Collect up to ``limit`` media URLs for the given taxon."""

    urls: List[str] = []
    seen: Set[str] = set()
    offset = 0

    while len(urls) < limit:
        params = {
            "taxonKey": str(taxon_key),
            "mediaType": "StillImage",
            "limit": str(OCCURRENCE_PAGE_SIZE),
            "offset": str(offset),
        }
        payload = fetch_json(f"{GBIF_API_ROOT}/occurrence/search", params)
        results = payload.get("results", [])
        if not results:
            break

        for record in results:
            for media in record.get("media", []) or []:
                url = media.get("identifier") or media.get("references")
                if not url:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
                if len(urls) >= limit:
                    break
            if len(urls) >= limit:
                break

        offset += len(results)
        if offset >= payload.get("count", 0):
            break

    return urls


def filename_from_url(url: str, index: int) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if not name:
        name = f"image_{index}.jpg"
    return name


def download_image(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_bytes(url)
    destination.write_bytes(data)


def save_images_for_species(species: str) -> bool:
    print(f"Processing: {species}")
    match = match_species(species)
    if not match:
        print(f"  ! Unable to resolve species '{species}' in GBIF")
        return False

    print(f"  Resolved to taxon {match.key} ({match.scientific_name})")
    urls = fetch_media_urls(match.key, IMAGES_PER_SPECIES)
    if not urls:
        print("  ! No images found")
        return True

    target_dir = OUTPUT_ROOT / slugify(match.scientific_name)
    used_names: Set[str] = set()
    for index, url in enumerate(urls, start=1):
        filename = filename_from_url(url, index)
        if filename in used_names:
            stem = Path(filename).stem
            suffix = Path(filename).suffix or ".jpg"
            filename = f"{stem}_{index}{suffix}"
        used_names.add(filename)
        destination = target_dir / filename
        try:
            download_image(url, destination)
        except HTTPError as exc:  # pragma: no cover - network
            print(f"    ! Failed to download {url}: {exc}")
            continue
        except URLError as exc:  # pragma: no cover - network
            print(f"    ! Error downloading {url}: {exc}")
            continue
        print(f"    Saved {destination}")

    if len(urls) < IMAGES_PER_SPECIES:
        print(
            f"  ! Only {len(urls)} images were available (requested {IMAGES_PER_SPECIES})"
        )
    return True


def main() -> int:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    failures: List[str] = []

    for species in SPECIES_NAMES:
        success = save_images_for_species(species)
        if not success:
            failures.append(species)

    if failures:
        print("\nSpecies that could not be resolved:")
        for species in failures:
            print(f"  - {species}")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
