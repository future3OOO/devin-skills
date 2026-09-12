"""Private SQLite implementation for the repository workflow Module.
This is not a selectable backend or public persistence Interface.  It is the
workflow Module's local-runtime implementation: one on-disk database per
repository slot, a temporary legacy importer, and deterministic recovery of the
active-event pointer from the event ledger.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, NoReturn, Sequence
from .preflight_document import validate_document
from .repo_identity import RepoIdentity
from .state_store import _active_candidate_tree, estate_home, read_json, repo_state_dir, utc_timestamp
DATABASE_NAME = "workflow.sqlite3"
DATABASE_FILES = frozenset({DATABASE_NAME, *(f"{DATABASE_NAME}{suffix}" for suffix in ("-journal", "-wal", "-shm"))})
AUTHORITY = "sqlite-event-ledger-v1"
STATE_SCHEMA_VERSION = 1
POLICY_VERSION = 1
BUSY_TIMEOUT_MS = 2500
JsonObject = dict[str, object]
class LedgerError(RuntimeError):
    """The authoritative workflow ledger could not be read or changed."""
class LedgerBusy(LedgerError):
    """A bounded SQLite wait expired."""
class LegacyImportError(LedgerError):
    """Legacy state could not be imported without guessing."""
@dataclass(frozen=True)
class EvidenceWrite:
    evidence_id: str
    workflow_id: str
    kind: str
    schema_version: int
    recorded_at: str
    document: JsonObject
@dataclass(frozen=True)
class ManifestWrite:
    manifest_id: str
    workflow_id: str
    kind: str
    schema_version: int
    recorded_at: str
    document: dict[str, str]
@dataclass(frozen=True)
class WorkflowRetentionItem:
    workflow_id: str
    slug: str
    latest_event_id: int
    active: bool
@dataclass(frozen=True)
class RetentionApplyResult:
    status: str
    current: tuple[WorkflowRetentionItem, ...] = ()
    error: str | None = None
def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def _logical_id(prefix: str, workflow_id: str, kind: str, document: object) -> str:
    payload = "\0".join((workflow_id, kind, _canonical(document))).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"
def evidence_write(
    workflow_id: str,
    kind: str,
    document: JsonObject,
    *,
    schema_version: int = 1,
    recorded_at: str | None = None,
) -> EvidenceWrite:
    return EvidenceWrite(
        _logical_id("evidence", workflow_id, kind, document),
        workflow_id,
        kind,
        schema_version,
        recorded_at or utc_timestamp(),
        document,
    )
def manifest_write(
    workflow_id: str,
    kind: str,
    document: dict[str, str],
    *,
    schema_version: int = 1,
    recorded_at: str | None = None,
) -> ManifestWrite:
    return ManifestWrite(
        _logical_id("manifest", workflow_id, kind, document),
        workflow_id,
        kind,
        schema_version,
        recorded_at or utc_timestamp(),
        document,
    )
def database_path(identity: RepoIdentity) -> Path:
    override = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
    root = Path(override).expanduser() if override else estate_home() / "state"
    return root / identity.key / DATABASE_NAME
def _store_exists(identity: RepoIdentity) -> bool:
    path = database_path(identity)
    return path.exists() or (path.parent / "workflow.json").exists()
def _private_sidecars(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        try:
            if candidate.exists():
                candidate.chmod(0o600)
        except OSError:
            pass
def _locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text
def _raise_operational(exc: sqlite3.OperationalError) -> NoReturn:
    if _locked(exc):
        raise LedgerBusy("workflow database is busy; no transition was recorded") from exc
    raise LedgerError(f"workflow database failure: {exc}") from exc
def _open_connection(path: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open one ledger path with the Module's complete SQLite contract."""
    mode = "ro" if read_only else "rw"
    target = path.resolve(strict=False).as_uri() + f"?mode={mode}"
    connection = sqlite3.connect(
        target, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
    return connection
@contextlib.contextmanager
def _path_connection(path: Path, *, read_only: bool) -> Iterator[sqlite3.Connection]:
    """Close one configured connection and preserve private write artifacts."""
    previous_umask = os.umask(0o077) if not read_only else None
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_connection(path, read_only=read_only)
        yield connection
    finally:
        if connection is not None:
            connection.close()
        if previous_umask is not None:
            os.umask(previous_umask)
            _private_sidecars(path)
@contextlib.contextmanager
def _connection(
    identity: RepoIdentity, *, prepare_authority: bool = True,
) -> Iterator[sqlite3.Connection]:
    try:
        path = repo_state_dir(identity) / DATABASE_NAME
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
    except OSError as exc:
        raise LedgerError(f"workflow database failure: {exc}") from exc
    try:
        with _path_connection(path, read_only=False) as connection:
            _schema(connection)
            if prepare_authority:
                _ensure_authority(connection, identity)
            yield connection
    except sqlite3.OperationalError as exc:
        _raise_operational(exc)
    except sqlite3.DatabaseError as exc:
        raise LedgerError(f"workflow database failure: {exc}") from exc
def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflows (
            workflow_id TEXT PRIMARY KEY,
            repo_key TEXT NOT NULL,
            slug TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
            kind TEXT NOT NULL, schema_version INTEGER NOT NULL,
            recorded_at TEXT NOT NULL, document_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_manifests (
            manifest_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
            kind TEXT NOT NULL, recorded_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL, manifest_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            state_schema_version INTEGER NOT NULL,
            policy_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            activates_workflow INTEGER NOT NULL DEFAULT 0 CHECK (activates_workflow IN (0, 1)),
            UNIQUE(event_id, workflow_id)
        );
        CREATE TABLE IF NOT EXISTS event_evidence (
            event_id INTEGER NOT NULL REFERENCES workflow_events(event_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE RESTRICT,
            PRIMARY KEY(event_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS event_manifests (
            event_id INTEGER NOT NULL REFERENCES workflow_events(event_id) ON DELETE CASCADE,
            manifest_id TEXT NOT NULL REFERENCES review_manifests(manifest_id) ON DELETE RESTRICT,
            PRIMARY KEY(event_id, manifest_id)
        );
        CREATE TABLE IF NOT EXISTS active_projection (
            slot INTEGER PRIMARY KEY CHECK(slot = 1),
            workflow_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            FOREIGN KEY(event_id, workflow_id)
                REFERENCES workflow_events(event_id, workflow_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS migration_records (
            name TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS workflow_events_by_workflow
            ON workflow_events(workflow_id, event_id);
        CREATE INDEX IF NOT EXISTS evidence_by_workflow
            ON evidence(workflow_id, recorded_at);
        """
    )
def _begin_write(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        _raise_operational(exc)
def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None
def _validate_repository_identity(connection: sqlite3.Connection, identity: RepoIdentity) -> None:
    expected = {"repo_key": identity.key, "repo_root": str(identity.root)}
    for key, value in expected.items():
        stored = _metadata(connection, key)
        if stored != value:
            raise LedgerError("workflow database repository identity does not match this checkout")
def _validate_state_identity(identity: RepoIdentity, state: JsonObject) -> None:
    if state.get("repo") != identity.as_dict():
        raise LedgerError("canonical workflow state repository identity does not match this checkout")
def _strict_document(path: Path) -> JsonObject:
    value = read_json(path)
    if not isinstance(value, dict):
        raise LegacyImportError(f"legacy document is missing or malformed: {path}")
    return value
def _legacy_kind(name: str) -> str | None:
    for prefix, kind in (
        ("disposition-preflight-", "advisor-disposition-preflight"),
        ("disposition-final-", "advisor-disposition-final"), ("preflight-", "preflight"),
        ("gate-", "production-code"), ("verification-", "verification"), ("tdd-", "tdd"),
        ("review-", "code-review"),
    ):
        if name.startswith(prefix) and name.endswith(".json") and len(name) > len(prefix) + 5:
            return kind
    return None
def _legacy_evidence(
    state: JsonObject, slot: Path,
) -> tuple[JsonObject, list[EvidenceWrite], list[ManifestWrite]]:
    converted = json.loads(_canonical(state))
    workflow_id = converted.get("workflowId")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise LegacyImportError("legacy workflow has no workflowId")
    writes: dict[str, EvidenceWrite] = {}
    manifests: list[ManifestWrite] = []
    latest: dict[str, tuple[datetime, str, str]] = {}
    def import_evidence(kind: str, path: Path, document: JsonObject) -> EvidenceWrite:
        if kind == "preflight":
            try:
                validate_document(document.get("document"))
            except ValueError as exc:
                raise LegacyImportError(
                    f"legacy preflight evidence is incomplete: {path}: {exc}") from exc
        recorded_at: str | None = None
        parsed_at: datetime | None = None
        for field in ("recordedAt", "updatedAt", "createdAt"):
            value = document.get(field)
            if not isinstance(value, str):
                continue
            try:
                candidate = datetime.fromisoformat(value)
            except ValueError:
                continue
            if candidate.tzinfo is not None:
                recorded_at, parsed_at = value, candidate
                break
        if recorded_at is None or parsed_at is None:
            raise LegacyImportError(f"legacy evidence has no orderable timestamp: {path}")
        write = evidence_write(workflow_id, kind, document, recorded_at=recorded_at)
        writes[write.evidence_id] = write
        order = (parsed_at, path.name, write.evidence_id)
        if kind not in latest or order > latest[kind]:
            latest[kind] = order
        return write
    fields = {
        "preflightEvidence": "preflight", "productionCodeEvidence": "production-code",
        "verificationEvidence": "verification",
    }
    referenced: set[Path] = set()
    for field, kind in fields.items():
        reference = converted.get(field)
        if reference is None:
            continue
        if not isinstance(reference, str) or not reference:
            raise LegacyImportError(f"legacy {field} is not a path")
        path = Path(reference)
        referenced.add(path.resolve(strict=False))
        document = _strict_document(path)
        if type(document.get("schemaVersion")) is not int or document.get("schemaVersion") != 1:
            raise LegacyImportError(f"legacy {field} has an unsupported schema")
        owner = document.get("workflowId")
        if not isinstance(owner, str) or not owner:
            raise LegacyImportError(f"legacy {field} has no workflowId")
        if owner != workflow_id:
            raise LegacyImportError(f"legacy {field} belongs to a different workflow")
        write = import_evidence(kind, path, document)
        converted[field] = write.evidence_id
    for path in sorted(slot.iterdir()):
        kind = _legacy_kind(path.name)
        if kind is None or path.resolve(strict=False) in referenced:
            continue
        if path.is_symlink() or not path.is_file():
            raise LegacyImportError(f"legacy evidence is not a regular file: {path}")
        document = _strict_document(path)
        owner = document.get("workflowId")
        if not isinstance(owner, str) or not owner:
            raise LegacyImportError(f"legacy evidence has no workflowId: {path}")
        if owner != workflow_id:
            # Retained history for a superseded pass has no trustworthy state
            # snapshot to import. Keep the file untouched rather than inventing
            # a workflow event for it.
            continue
        if type(document.get("schemaVersion")) is not int or document.get("schemaVersion") != 1:
            raise LegacyImportError(f"legacy evidence has an unsupported schema: {path}")
        import_evidence(kind, path, document)
    latest_fields = {
        "preflight": "preflightLatestEvidence", "production-code": "productionCodeLatestEvidence",
        "verification": "verificationLatestEvidence", "tdd": "tddEvidence",
    }
    for kind, field in latest_fields.items():
        if kind in latest:
            converted[field] = latest[kind][2]
    if "code-review" in latest:
        review = converted.get("codeReview")
        if isinstance(review, dict) and review.get("status") in {"passed", "not-required"}:
            converted["codeReviewEvidence"] = latest["code-review"][2]
    for kind, field in (
        ("advisor-disposition-preflight", "advisorPreflight"),
        ("advisor-disposition-final", "finalReview"),
    ):
        review = converted.get(field)
        if kind in latest and isinstance(review, dict) and review.get("findings") == "addressed":
            review["dispositionEvidence"] = latest[kind][2]
    manifest = converted.pop("reviewManifest", None)
    if manifest is not None:
        validated = _manifest_value(manifest)
        if validated is None:
            raise LegacyImportError("legacy reviewManifest is malformed")
        write = manifest_write(workflow_id, "lead-review-tree", validated)
        manifests.append(write)
        converted["reviewManifestId"] = write.manifest_id
    return converted, list(writes.values()), manifests
def _manifest_value(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(path, str) and isinstance(digest, str) for path, digest in value.items()):
        return None
    return value
def _manifest_from(connection: sqlite3.Connection, manifest_id: str | None) -> dict[str, str] | None:
    if not manifest_id:
        return None
    row = connection.execute(
        "SELECT manifest_json FROM review_manifests WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    return _manifest_value(json.loads(str(row["manifest_json"]))) if row is not None else None
def _insert_workflow(connection: sqlite3.Connection, identity: RepoIdentity, state: JsonObject) -> None:
    _validate_state_identity(identity, state)
    workflow_id = state.get("workflowId")
    slug = state.get("slug")
    created_at = state.get("createdAt")
    if not all(isinstance(value, str) and value for value in (workflow_id, slug, created_at)):
        raise LedgerError("canonical workflow state is missing workflow identity")
    connection.execute(
        "INSERT OR IGNORE INTO workflows(workflow_id, repo_key, slug, created_at) VALUES (?, ?, ?, ?)",
        (workflow_id, identity.key, slug, created_at),
    )
def _insert_evidence(connection: sqlite3.Connection, writes: Sequence[EvidenceWrite]) -> None:
    for write in writes:
        connection.execute(
            """INSERT INTO evidence(
                   evidence_id, workflow_id, kind, schema_version, recorded_at, document_json
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(evidence_id) DO NOTHING""",
            (
                write.evidence_id,
                write.workflow_id,
                write.kind,
                write.schema_version,
                write.recorded_at,
                _canonical(write.document),
            ),
        )
def _insert_manifests(connection: sqlite3.Connection, writes: Sequence[ManifestWrite]) -> None:
    for write in writes:
        connection.execute(
            """INSERT INTO review_manifests(
                   manifest_id, workflow_id, kind, schema_version, recorded_at, manifest_json
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(manifest_id) DO NOTHING""",
            (
                write.manifest_id,
                write.workflow_id,
                write.kind,
                write.schema_version,
                write.recorded_at,
                _canonical(write.document),
            ),
        )
def _append_event(
    connection: sqlite3.Connection,
    state: JsonObject,
    kind: str,
    *, evidence: Sequence[EvidenceWrite] = (),
    manifests: Sequence[ManifestWrite] = (), activate: bool = False,
) -> int:
    workflow_id = state.get("workflowId")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise LedgerError("event state has no workflowId")
    _insert_evidence(connection, evidence)
    _insert_manifests(connection, manifests)
    recorded_at = str(state.get("updatedAt") or utc_timestamp())
    cursor = connection.execute(
        """INSERT INTO workflow_events(
               workflow_id, kind, recorded_at, state_schema_version,
               policy_version, state_json, activates_workflow
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            workflow_id,
            kind,
            recorded_at,
            STATE_SCHEMA_VERSION,
            POLICY_VERSION,
            _canonical(state),
            1 if activate else 0,
        ),
    )
    event_id = int(cursor.lastrowid)
    connection.executemany(
        "INSERT INTO event_evidence(event_id, evidence_id) VALUES (?, ?)",
        ((event_id, write.evidence_id) for write in evidence),
    )
    connection.executemany(
        "INSERT INTO event_manifests(event_id, manifest_id) VALUES (?, ?)",
        ((event_id, write.manifest_id) for write in manifests),
    )
    return event_id
def _set_projection(connection: sqlite3.Connection, workflow_id: str, event_id: int) -> None:
    connection.execute(
        """INSERT INTO active_projection(slot, workflow_id, event_id)
           VALUES (1, ?, ?)
           ON CONFLICT(slot) DO UPDATE SET
               workflow_id = excluded.workflow_id,
               event_id = excluded.event_id""",
        (workflow_id, event_id),
    )
def _ensure_authority(connection: sqlite3.Connection, identity: RepoIdentity) -> None:
    if _metadata(connection, "authority") == AUTHORITY:
        _validate_repository_identity(connection, identity)
        return
    _begin_write(connection)
    try:
        _apply_authority(connection, identity)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

def _apply_authority(connection: sqlite3.Connection, identity: RepoIdentity) -> None:
    if _metadata(connection, "authority") == AUTHORITY:
        _validate_repository_identity(connection, identity)
        return
    legacy_path = database_path(identity).parent / "workflow.json"
    if legacy_path.exists():
        legacy = _strict_document(legacy_path)
        if (type(legacy.get("schemaVersion")) is not int
                or legacy.get("schemaVersion") != 1
                or legacy.get("repo") != identity.as_dict()):
            raise LegacyImportError("legacy workflow identity or schema is unsupported")
        converted, evidence, manifests = _legacy_evidence(legacy, legacy_path.parent)
        _insert_workflow(connection, identity, converted)
        event_id = _append_event(
            connection,
            converted,
            "legacy-imported",
            evidence=evidence,
            manifests=manifests,
            activate=True,
        )
        _set_projection(connection, str(converted["workflowId"]), event_id)
        stored = json.loads(
            connection.execute(
                "SELECT state_json FROM workflow_events WHERE event_id = ?", (event_id,)
            ).fetchone()["state_json"]
        )
        if stored != converted:
            raise LegacyImportError("legacy import public status mismatch")
        details = {
            "legacyPath": str(legacy_path),
            "workflowId": converted["workflowId"],
            "evidenceIds": [write.evidence_id for write in evidence],
            "manifestIds": [write.manifest_id for write in manifests],
        }
    else:
        details = {"legacyPath": None, "workflowId": None, "evidenceIds": [], "manifestIds": []}
    now = utc_timestamp()
    connection.execute(
        "INSERT INTO migration_records(name, completed_at, details_json) VALUES (?, ?, ?)",
        ("legacy-json-v1", now, _canonical(details)),
    )
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        (("repo_key", identity.key), ("repo_root", str(identity.root)),
            ("authority", AUTHORITY),),
    )
def _event_state(row: sqlite3.Row, workflow_id: str | None = None) -> JsonObject:
    state_version, policy_version = row["state_schema_version"], row["policy_version"]
    if (type(state_version) is not int or state_version != STATE_SCHEMA_VERSION
            or type(policy_version) is not int or not 1 <= policy_version <= POLICY_VERSION):
        raise LedgerError("authoritative event schema or policy is unsupported")
    try:
        state = json.loads(str(row["state_json"]))
    except (TypeError, ValueError) as exc:
        raise LedgerError("authoritative event contains invalid state JSON") from exc
    if (not isinstance(state, dict) or type(state.get("schemaVersion")) is not int
            or state.get("schemaVersion") != STATE_SCHEMA_VERSION):
        raise LedgerError("authoritative event contains invalid state schema")
    if workflow_id is not None and state.get("workflowId") != workflow_id:
        raise LedgerError("authoritative event workflowId does not match ledger identity")
    return state
def _repair_projection(connection: sqlite3.Connection) -> JsonObject | None:
    activation = connection.execute(
        """SELECT workflow_id
           FROM workflow_events
           WHERE activates_workflow = 1
           ORDER BY event_id DESC
           LIMIT 1"""
    ).fetchone()
    if activation is None:
        connection.execute("DELETE FROM active_projection WHERE slot = 1")
        return None
    workflow_id = str(activation["workflow_id"])
    latest = connection.execute(
        """SELECT event_id, state_schema_version, policy_version, state_json
           FROM workflow_events
           WHERE workflow_id = ?
           ORDER BY event_id DESC
           LIMIT 1""",
        (workflow_id,),
    ).fetchone()
    if latest is None:
        connection.execute("DELETE FROM active_projection WHERE slot = 1")
        return None
    event_id = int(latest["event_id"])
    pointer = connection.execute(
        "SELECT workflow_id, event_id FROM active_projection WHERE slot = 1"
    ).fetchone()
    if pointer is None or str(pointer["workflow_id"]) != workflow_id or int(pointer["event_id"]) != event_id:
        _set_projection(connection, workflow_id, event_id)
    return _event_state(latest, workflow_id)
class LedgerMutation:
    """One private transaction over the active workflow facts."""
    def __init__(self, connection: sqlite3.Connection, identity: RepoIdentity) -> None:
        self.connection = connection
        self.identity = identity
        self.state = _repair_projection(connection)
        if self.state is not None:
            _validate_state_identity(identity, self.state)
    def append(
        self,
        state: JsonObject,
        kind: str,
        *,
        evidence: Sequence[EvidenceWrite] = (),
        manifests: Sequence[ManifestWrite] = (),
        activate: bool = False,
    ) -> JsonObject:
        _insert_workflow(self.connection, self.identity, state)
        workflow_id = str(state["workflowId"])
        if not activate:
            active = _repair_projection(self.connection)
            if active is None or active.get("workflowId") != workflow_id:
                raise LedgerError("workflow instance is no longer active")
        event_id = _append_event(
            self.connection,
            state,
            kind,
            evidence=evidence,
            manifests=manifests,
            activate=activate,
        )
        _set_projection(self.connection, workflow_id, event_id)
        self.state = state
        return state
    def evidence(self, evidence_id: str | None) -> JsonObject | None:
        if not evidence_id:
            return None
        row = self.connection.execute(
            "SELECT document_json FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["document_json"]))
        return value if isinstance(value, dict) else None
    def manifest(self, manifest_id: str | None) -> dict[str, str] | None:
        return _manifest_from(self.connection, manifest_id)
@contextlib.contextmanager
def mutation(
    identity: RepoIdentity, *, expected_candidate_tree: str | None = None,
) -> Iterator[LedgerMutation]:
    with _connection(identity, prepare_authority=False) as connection:
        _begin_write(connection)
        try:
            _apply_authority(connection, identity)
            transaction = LedgerMutation(connection, identity)
            yield transaction
            if (expected_candidate_tree is not None
                    and _active_candidate_tree(identity) != expected_candidate_tree):
                raise LedgerError("active candidate changed during workflow mutation")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
def read_active(identity: RepoIdentity) -> JsonObject | None:
    if not _store_exists(identity):
        return None
    with _connection(identity) as connection:
        _begin_write(connection)
        try:
            state = _repair_projection(connection)
            if state is not None:
                _validate_state_identity(identity, state)
            connection.commit()
            return state
        except Exception:
            connection.rollback()
            raise
def read_evidence(identity: RepoIdentity, evidence_id: str) -> JsonObject | None:
    if _store_exists(identity):
        with _connection(identity) as connection:
            row = connection.execute(
                """SELECT evidence_id, workflow_id, kind, schema_version, recorded_at, document_json
                   FROM evidence WHERE evidence_id = ?""",
                (evidence_id,),
            ).fetchone()
            if row is not None:
                document = json.loads(str(row["document_json"]))
                return {
                    "evidenceId": str(row["evidence_id"]),
                    "workflowId": str(row["workflow_id"]),
                    "kind": str(row["kind"]),
                    "schemaVersion": int(row["schema_version"]),
                    "recordedAt": str(row["recorded_at"]),
                    "document": document,
                }
    return None
def read_manifest(identity: RepoIdentity, manifest_id: str) -> dict[str, str] | None:
    if _store_exists(identity):
        with _connection(identity) as connection:
            return _manifest_from(connection, manifest_id)
    return None
def history(identity: RepoIdentity, workflow_id: str | None = None) -> JsonObject:
    if not _store_exists(identity):
        return {"events": []}
    with _connection(identity) as connection:
        _begin_write(connection)
        try:
            _repair_projection(connection)
            where = "WHERE event.workflow_id = ?" if workflow_id else ""
            params: tuple[object, ...] = (workflow_id,) if workflow_id else ()
            rows = connection.execute(
                f"""SELECT event.event_id, event.workflow_id, event.kind, event.recorded_at,
                           event.state_schema_version, event.policy_version, event.state_json,
                           event.activates_workflow
                    FROM workflow_events AS event
                    {where}
                    ORDER BY event.event_id""",
                params,
            ).fetchall()
            events = []
            for row in rows:
                event_id = int(row["event_id"])
                evidence_ids = [
                    str(item["evidence_id"])
                    for item in connection.execute(
                        "SELECT evidence_id FROM event_evidence WHERE event_id = ? ORDER BY evidence_id",
                        (event_id,),
                    )
                ]
                manifest_ids = [
                    str(item["manifest_id"])
                    for item in connection.execute(
                        "SELECT manifest_id FROM event_manifests WHERE event_id = ? ORDER BY manifest_id",
                        (event_id,),
                    )
                ]
                events.append({
                    "eventId": event_id,
                    "workflowId": str(row["workflow_id"]),
                    "kind": str(row["kind"]),
                    "recordedAt": str(row["recorded_at"]),
                    "stateSchemaVersion": int(row["state_schema_version"]),
                    "policyVersion": int(row["policy_version"]),
                    "activatesWorkflow": bool(row["activates_workflow"]),
                    "evidenceIds": evidence_ids,
                    "manifestIds": manifest_ids,
                    "state": _event_state(row, str(row["workflow_id"])),
                })
            connection.commit()
            return {"events": events}
        except Exception:
            connection.rollback()
            raise
def _retention_inventory_connection(
    connection: sqlite3.Connection, expected_repo_key: str,
) -> tuple[WorkflowRetentionItem, ...] | None:
    if _metadata(connection, "authority") != AUTHORITY:
        return None
    if _metadata(connection, "repo_key") != expected_repo_key:
        raise LedgerError("database repository identity does not match its state slot")
    activation = connection.execute(
        "SELECT workflow_id FROM workflow_events WHERE activates_workflow = 1 "
        "ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    active = str(activation["workflow_id"]) if activation else None
    rows = connection.execute(
        """SELECT workflow.workflow_id, workflow.repo_key, workflow.slug,
                  event.event_id AS latest_event, event.state_schema_version,
                  event.policy_version, event.state_json
           FROM workflows AS workflow JOIN workflow_events AS event
             ON event.event_id = (SELECT MAX(latest.event_id) FROM workflow_events AS latest
                                  WHERE latest.workflow_id = workflow.workflow_id)
           ORDER BY event.event_id DESC, workflow.workflow_id DESC"""
    ).fetchall()
    if len(rows) != int(connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]):
        raise LedgerError("authoritative workflow history is incomplete")
    items = []
    for row in rows:
        workflow_id = str(row["workflow_id"])
        if str(row["repo_key"]) != expected_repo_key:
            raise LedgerError("workflow row repository identity does not match its state slot")
        _event_state(row, workflow_id)
        items.append(WorkflowRetentionItem(
            workflow_id, str(row["slug"]), int(row["latest_event"]), workflow_id == active))
    known = {item.workflow_id for item in items}
    if (items and active is None) or (active is not None and active not in known):
        raise LedgerError("authoritative active workflow history is missing")
    return tuple(items)
def retention_inventory(
    database: Path, expected_repo_key: str,
) -> tuple[tuple[WorkflowRetentionItem, ...] | None, str | None]:
    try:
        with _path_connection(database, read_only=True) as connection:
            return _retention_inventory_connection(connection, expected_repo_key), None
    except (OSError, sqlite3.DatabaseError, LedgerError, ValueError) as exc:
        return None, str(exc)
def apply_retention(
    database: Path, expected_repo_key: str, expected: Sequence[WorkflowRetentionItem],
    remove_ids: set[str],
) -> RetentionApplyResult:
    try:
        with _path_connection(database, read_only=False) as connection:
            try:
                _begin_write(connection)
            except LedgerBusy as exc:
                return RetentionApplyResult("busy", error=str(exc))
            try:
                current = _retention_inventory_connection(connection, expected_repo_key)
                if current is None:
                    connection.rollback()
                    return RetentionApplyResult("not-authoritative")
                if tuple(expected) != current:
                    connection.rollback()
                    return RetentionApplyResult("changed", current=current)
                known = {item.workflow_id for item in current}
                active = {item.workflow_id for item in current if item.active}
                if not remove_ids <= known or remove_ids & active:
                    raise LedgerError("retention selection is stale or includes the active workflow")
                connection.executemany(
                    "DELETE FROM workflows WHERE workflow_id = ?",
                    ((workflow_id,) for workflow_id in sorted(remove_ids)),
                )
                connection.commit()
                return RetentionApplyResult("applied", current=current)
            except (sqlite3.DatabaseError, LedgerError, ValueError) as exc:
                connection.rollback()
                return RetentionApplyResult("failed", error=str(exc))
    except (OSError, sqlite3.DatabaseError) as exc:
        return RetentionApplyResult("failed", error=str(exc))
