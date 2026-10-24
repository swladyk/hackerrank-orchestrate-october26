"""Global evidence index: parse every file once, then answer per-case questions."""

from __future__ import annotations

import re
from collections import defaultdict

from ..identity import Person, identifier_keys
from ..loaders import Activity, load_activity, load_manifest
from .model import Observation, system_for
from .multimodal import load_multimodal
from .parsers import parse_file

# "Data catalog linked tombstone_17 (user_17) to x@y and user_17." / "... confirmed object_19 (file.pdf) belongs
# only to x@y and user_07; ..." -> resource, displayed alias, email, user
LINK_RE = re.compile(r"(?:linked|confirmed) (\S+) \(([^)]+)\) (?:to|belongs only to) (\S+) and (user_\d+)")


class EvidenceIndex:
    def __init__(self, offline: bool = True, llm=None, fresh: bool = False) -> None:
        self.files = {ev.file_id: ev for ev in load_manifest()}
        self.activity = load_activity()
        self.catalog_links: list[tuple[str, str, Activity]] = []  # (resource_id, identity key, activity)
        self.aliases: dict[str, str] = {}  # displayed reference (user_17, NST-..., file name) -> resource_id
        for a in self.activity:
            if a.event_type == "LINK_RESOURCE_IDENTITY" and (m := LINK_RE.search(a.details)):
                self.aliases[m.group(2)] = m.group(1)
                for raw in (m.group(3), m.group(4)):
                    for key in identifier_keys(raw.rstrip(".;,")):
                        self.catalog_links.append((m.group(1), key, a))
        self.observations: list[Observation] = []
        for ev in sorted(self.files.values(), key=lambda e: e.file_id):
            self.observations.extend(parse_file(ev))
        self.observations.extend(load_multimodal(self.files, self.aliases, llm=llm, offline=offline, fresh=fresh))
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.by_key: dict[str, set[str]] = defaultdict(set)
        self.coverage: dict[str, list[Observation]] = defaultdict(list)  # system -> complete listings
        for o in self.observations:
            if o.resource_id == "*":
                self.coverage[o.system_id].append(o)
                continue
            self.by_resource[o.resource_id].append(o)
            for key in o.all_keys():
                self.by_key[key].add(o.resource_id)

    def linked_resources(self, person: Person, audit_at: str) -> dict[str, set[str]]:
        """resource_id -> the person keys that link it (only from evidence visible at audit time)."""
        keys = person.keys()
        out: dict[str, set[str]] = defaultdict(set)
        for key in keys:
            for resource_id in self.by_key.get(key, ()):
                for o in self.by_resource[resource_id]:
                    if visible(o, audit_at) and key in o.all_keys():
                        out[resource_id].add(key)
                        break
        for resource_id, key, act in self.catalog_links:
            if key in keys and act.event_time <= audit_at:
                out[resource_id].add(key)
        return dict(out)

    def observations_for(self, resource_id: str, audit_at: str) -> list[Observation]:
        return sorted((o for o in self.by_resource.get(resource_id, ()) if visible(o, audit_at)),
                      key=lambda o: (o.observed_at, o.file_captured_at, o.file_id))

    def absence_proof(self, resource_id: str, system_id: str, audit_at: str) -> Observation | None:
        """Newest citable complete listing of the system that does not contain the resource."""
        proofs = []
        for o in self.coverage.get(system_id, ()):
            if not citable(o, audit_at) or resource_id in o.attrs["_coverage"]:
                continue
            # A SQL dump that names the system and inserts nothing is not an enumerated population.
            # An explicit empty JSON collection still is.
            if not o.attrs["_coverage"] and self.files[o.file_id].path.suffix.lower() == ".sql":
                continue
            proofs.append(o)
        return max(proofs, key=lambda o: (o.observed_at, o.file_captured_at, o.file_id), default=None)

    def system_of(self, resource_id: str) -> str:
        systems = {o.system_id for o in self.by_resource.get(resource_id, ())}
        guess = system_for(resource_id)
        return guess if guess in systems or not systems else sorted(systems)[0]


def visible(o: Observation, audit_at: str) -> bool:
    """Facts recorded at or before audit time count, even if the file was exported later."""
    return o.observed_at <= audit_at


def citable(o: Observation, audit_at: str) -> bool:
    """Only files captured at or before audit time may be cited as evidence."""
    return o.file_captured_at <= audit_at and o.observed_at <= audit_at
