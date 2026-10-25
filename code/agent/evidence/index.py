"""Global evidence index: parse every file once, then answer per-case questions."""

from __future__ import annotations

import re
from collections import defaultdict

from ..identity import Person, identifier_keys, user_ref_pattern
from ..loaders import Activity, load_activity, load_manifest
from .model import EMAIL_RE, Observation, SystemMap
from .multimodal import load_multimodal
from .parsers import parse_file

LINK_EVENT = "LINK_RESOURCE_IDENTITY"


class EvidenceIndex:
    def __init__(self, offline: bool = True, llm=None, fresh: bool = False) -> None:
        self.files = {ev.file_id: ev for ev in load_manifest()}
        self.activity = load_activity()
        self.catalog_links: list[tuple[str, str, Activity]] = []  # (resource_id, identity key, activity)
        self.aliases: dict[str, str] = {}  # displayed reference (user_17, NST-..., file name) -> resource_id
        user_re = re.compile(rf"\b{user_ref_pattern()}\b")
        for a in self.activity:
            if a.event_type != LINK_EVENT or not a.resource_id:
                continue
            # "<resource> (<displayed alias>) ... <email> ... <user id>": the alias is how artifacts show it
            if m := re.search(re.escape(a.resource_id) + r" \(([^)]+)\)", a.details):
                self.aliases[m.group(1)] = a.resource_id
            for raw in EMAIL_RE.findall(a.details) + user_re.findall(a.details):
                for key in identifier_keys(raw):
                    self.catalog_links.append((a.resource_id, key, a))
        self.observations: list[Observation] = []
        for ev in sorted(self.files.values(), key=lambda e: e.file_id):
            self.observations.extend(parse_file(ev))
        self.systems = SystemMap(self.observations, self.activity)
        self.observations = resolve_systems(self.observations, self.systems)
        self.observations.extend(load_multimodal(self.files, self.aliases, self.systems, llm=llm, offline=offline,
                                                 fresh=fresh))
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
        guess = self.systems(resource_id)
        return guess if guess in systems or not systems else sorted(systems)[0]


def resolve_systems(observations: list[Observation], systems: SystemMap) -> list[Observation]:
    """Fill in owning systems the evidence left implicit, then settle deferred SQL listings: an unscoped dump
    is a complete listing only when all of its rows belong to one system."""
    rows_by_file: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        if o.resource_id != "*":
            o.system_id = o.system_id or systems(o.resource_id)
            rows_by_file[o.file_id].append(o)
    out = []
    for o in observations:
        if o.resource_id == "*" and o.attrs["_coverage"] is None:
            rows = rows_by_file.get(o.file_id, [])
            if o.system_id is None:
                owners = {r.system_id for r in rows}
                if len(owners) != 1:
                    continue
                o.system_id = owners.pop()
            o.attrs["_coverage"] = frozenset(r.resource_id for r in rows if r.system_id == o.system_id)
        out.append(o)
    return out


def visible(o: Observation, audit_at: str) -> bool:
    """Facts recorded at or before audit time count, even if the file was exported later."""
    return o.observed_at <= audit_at


def citable(o: Observation, audit_at: str) -> bool:
    """Only files captured at or before audit time may be cited as evidence."""
    return o.file_captured_at <= audit_at and o.observed_at <= audit_at
