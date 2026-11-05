"""Persistent production stages; reported progress is never completion evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from .store import Problem, Store, confined, now, uid

STAGES: Final = ('intake', 'story', 'characters', 'voice', 'storyboard', 'images', 'render', 'review')
DEPENDENCIES: Final = {'intake': (), 'story': ('intake',), 'characters': ('story',),
                       'voice': ('story', 'characters'), 'storyboard': ('voice',),
                       'images': ('storyboard',), 'render': ('images',), 'review': ('render',)}
RECIPES: Final = ('narrated_stills', 'footage_edit', 'generated_motion')


class Artifact(TypedDict):
    path: str
    sha256: str


class Node(TypedDict):
    id: str
    state: str
    attempt: int
    blocker: str | None
    artifacts: list[Artifact]


class Snapshot(TypedDict):
    recipe: str
    version: int
    nodes: list[Node]
    basis: str


def migrate(db: sqlite3.Connection) -> None:
    """Add only new tables; existing projects/status strings remain untouched."""
    db.execute('CREATE TABLE IF NOT EXISTS workflow_runs (project_id TEXT PRIMARY KEY, recipe TEXT NOT NULL, version INTEGER NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS workflow_nodes (
        project_id TEXT NOT NULL, node_id TEXT NOT NULL, position INTEGER NOT NULL,
        state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease TEXT,
        input_hash TEXT, artifacts TEXT NOT NULL DEFAULT '[]', blocker TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id,node_id))''')


def initialize(db: sqlite3.Connection, project: str, recipe: str) -> None:
    """Create a single stage graph atomically with its project, never a second queue."""
    if recipe not in RECIPES:
        raise Problem('지원하지 않는 제작 경로입니다.')
    db.execute('INSERT INTO workflow_runs VALUES(?,?,1)', (project, recipe))
    for position, stage in enumerate(STAGES):
        db.execute('INSERT INTO workflow_nodes(project_id,node_id,position,state,updated_at) VALUES(?,?,?,?,?)',
                   (project, stage, position, 'pending', now()))


@dataclass(frozen=True, slots=True)
class Workflow:
    store: Store
    project: str

    def snapshot(self) -> Snapshot:
        with self.store.db() as db:
            run = db.execute('SELECT * FROM workflow_runs WHERE project_id=?', (self.project,)).fetchone()
            if not run:
                raise Problem('이 프로젝트에는 영속 워크플로우가 없습니다.', 404)
            rows = db.execute('SELECT * FROM workflow_nodes WHERE project_id=? ORDER BY position', (self.project,)).fetchall()
        return {'recipe': run['recipe'], 'version': run['version'], 'basis': 'artifact_hashes_only',
                'nodes': [{'id': row['node_id'], 'state': row['state'], 'attempt': row['attempt'],
                           'blocker': row['blocker'], 'artifacts': json.loads(row['artifacts'])} for row in rows]}

    def inputs(self, db: sqlite3.Connection, node: str) -> str:
        """Rehash prerequisites; a changed artifact cannot authorize its dependants."""
        if node not in STAGES:
            raise Problem('올바르지 않은 워크플로우 단계입니다.')
        hashes: list[str] = []
        root = self.store.project_root(self.project)
        for prerequisite in DEPENDENCIES[node]:
            row = db.execute('SELECT state,artifacts FROM workflow_nodes WHERE project_id=? AND node_id=?', (self.project, prerequisite)).fetchone()
            if row is None or row['state'] not in ('succeeded', 'skipped'):
                raise Problem('선행 제작 단계가 완료되지 않았습니다: ' + prerequisite, 409)
            for artifact in json.loads(row['artifacts']):
                path = confined(root, artifact['path'])
                actual = digest(path)
                if actual != artifact['sha256']:
                    raise Problem('선행 산출물이 변경됐습니다. 관련 단계를 다시 검증하세요.', 409)
                hashes.append(actual)
        return hashlib.sha256(json.dumps(hashes).encode()).hexdigest()

    def start(self, node: str) -> str:
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint = self.inputs(db, node)
            lease = uid('lease')
            changed = db.execute("UPDATE workflow_nodes SET state='running',attempt=attempt+1,lease=?,input_hash=?,blocker=NULL,updated_at=? WHERE project_id=? AND node_id=? AND state IN ('pending','failed')",
                                 (lease, fingerprint, now(), self.project, node)).rowcount
            if changed != 1:
                raise Problem('이미 실행/완료됐거나 원격 상태 대조가 필요한 단계입니다.', 409)
        self.store.event(self.project, 'workflow', '제작 단계 시작: ' + node, {'node': node, 'state': 'running'})
        return lease

    def complete(self, node: str, lease: str, files: list[str]) -> None:
        if not files or len(files) > 500 or any(not isinstance(path, str) for path in files):
            raise Problem('단계 완료에는 실제 산출물 경로가 필요합니다.')
        root = self.store.project_root(self.project)
        artifacts: list[Artifact] = [{'path': path, 'sha256': digest(confined(root, path))} for path in dict.fromkeys(files)]
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint = self.inputs(db, node)
            changed = db.execute("UPDATE workflow_nodes SET state='succeeded',artifacts=?,updated_at=? WHERE project_id=? AND node_id=? AND state='running' AND lease=? AND input_hash=?",
                                 (json.dumps(artifacts), now(), self.project, node, lease, fingerprint)).rowcount
            if changed != 1:
                raise Problem('현재 시도의 lease/입력과 일치하지 않습니다.', 409)
        self.store.event(self.project, 'workflow', '산출물 확인: ' + node, {'node': node, 'state': 'succeeded'})

    def block(self, node: str, lease: str, reason: str) -> None:
        if not reason.strip() or len(reason) > 2000:
            raise Problem('구체적인 중단 이유가 필요합니다.')
        with self.store.db() as db:
            changed = db.execute("UPDATE workflow_nodes SET state='failed',blocker=?,updated_at=? WHERE project_id=? AND node_id=? AND state='running' AND lease=?",
                                 (reason, now(), self.project, node, lease)).rowcount
            if changed != 1:
                raise Problem('현재 실행 시도와 일치하지 않습니다.', 409)

    def reconcile(self) -> None:
        """Trusted operator checked remote state; old leases never become valid again."""
        with self.store.db() as db:
            db.execute("UPDATE workflow_nodes SET state='failed',lease=NULL,blocker='operator checked remote state; existing provider receipts remain binding',updated_at=? WHERE project_id=? AND state='reconciling'", (now(), self.project))

    def skip(self, node: str, reason: str) -> None:
        if node != 'characters' or not reason.strip() or len(reason) > 2000:
            raise Problem('캐릭터 단계만 실제 사유와 함께 생략할 수 있습니다.')
        with self.store.db() as db:
            changed = db.execute("UPDATE workflow_nodes SET state='skipped',blocker=?,updated_at=? WHERE project_id=? AND node_id=? AND state='pending'", (reason, now(), self.project, node)).rowcount
            if changed != 1:
                raise Problem('생략할 수 없는 단계 상태입니다.', 409)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
