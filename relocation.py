"""Recoverable, explicitly bounded skill/package relocation transactions."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import uuid


def identity(path):
    value = Path(path).lstat()
    return [value.st_dev, value.st_ino]


def exists(path):
    return path.exists() or path.is_symlink()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Relocator:
    def __init__(self, lib, api):
        self.lib, self.api = lib, api
        self.error = api.LibraryError

    @contextlib.contextmanager
    def lock(self):
        with open(str(self.lib.path) + '.relocation.lock', 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def manifest(self, path):
        return self.api.tree_manifest(path)

    def save(self, directory, record, status):
        record['status'] = status
        self.api.write_record(directory / 'relocation.json', record)

    def owned(self, path, expected, manifest=None):
        if path.is_symlink() or not path.is_dir() or (expected is not None and identity(path) != expected):
            raise self.error('Relocation directory ownership changed: ' + str(path))
        if manifest is not None and self.manifest(path) != manifest:
            raise self.error('Relocation directory changed: ' + str(path))

    def move(self, source, target):
        if exists(target):
            raise self.error('Relocation destination is occupied: ' + str(target))
        missing, parent = [], target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        for folder in [*missing, parent]:
            sync_directory(folder)
        os.rename(source, target)
        sync_directory(source.parent)
        sync_directory(target.parent)

    def validate_links(self, root, manifest):
        for relative, value in manifest.items():
            if not value.startswith('link:'):
                continue
            pointed = Path(value[5:])
            if pointed.is_absolute():
                if self.api.absolute_link_depends_on_root(pointed, root):
                    raise self.error('Absolute link would break after moving: ' + relative)
            else:
                parent = (root / relative).parent
                normalized = Path(os.path.normpath(parent / pointed))
                if (normalized != root and root not in normalized.parents) or \
                        self.api.relative_link_depends_on_root(parent, pointed, root):
                    raise self.error('Relative link would break after moving: ' + relative)

    def relocate(self, sid, into=None, *, package_root=None, self_contained=False):
        if (package_root is None) == (not self_contained):
            raise self.error('Relocation requires exactly one explicit boundary: --package-root or --self-contained')
        with self.lock():
            state = self.lib.read()
            entry = self.lib.entry(state, sid)
            if not entry['enabled'] or entry['placement'] != 'library':
                raise self.error('Only enabled Library-managed skills can be relocated: ' + sid)
            self.lib.require_current(entry)
            skill_root = Path(self.api.read_skill(entry['path'])['skill_root'])
            if entry['scope'] == 'plugin' and package_root is None:
                raise self.error('Plugin skills require an explicit complete --package-root')
            root = self.api.canonical_path(package_root) if package_root is not None else skill_root
            if root != skill_root and root not in skill_root.parents:
                raise self.error('Package root must contain the selected skill')
            if entry['scope'] == 'plugin' and root == skill_root:
                raise self.error('Plugin package root must include the complete enclosing package')
            # The caller confirms the dependency boundary; inspect all filesystem
            # entries, preserving modes and links, without guessing dynamic imports.
            manifest = self.manifest(root)
            self.validate_links(root, manifest)
            if any((p / '.git').exists() for p in (root, *root.parents)) or any(
                    p.name == '.git' for p in root.rglob('.git')):
                raise self.error('Skill folder belongs to the git checkout; remove the host link or copy instead')
            if root == self.lib.path.parent or root in self.lib.path.parents:
                raise self.error('Package root cannot contain the registry or relocation records')
            if root.stat().st_dev != self.lib.path.parent.stat().st_dev:
                raise self.error('Source and registry must share a filesystem for atomic archival')
            into = self.api.canonical_path(into or self.lib.path.parent / 'sources' /
                                           ('plugins' if entry['scope'] == 'plugin' else 'personal'))
            target = into / root.name
            if into == root or root in into.parents:
                raise self.error('Destination is inside the skill folder: ' + str(into))
            if into in root.parents:
                raise self.error('Skill folder is already inside ' + str(into))
            if exists(target):
                raise self.error('Destination already exists: ' + str(target))
            ancestor = next(p for p in (into, *into.parents) if p.exists())
            if ancestor.stat().st_dev != root.stat().st_dev:
                raise self.error('Destination must share the source filesystem')
            mappings = []
            for other in state['skills']:
                path = self.api.canonical_path(other['path'])
                if path.parent in root.parents:
                    raise self.error('Package overlaps registered skill ' + other['id'])
                if path == root or root in path.parents:
                    if other['path'] != str(path):
                        raise self.error('Rebind the skill to its canonical path before relocation: ' + other['id'])
                    if other['placement'] != 'library':
                        raise self.error('Package contains a global skill; its native entry must remain')
                    mappings.append({'id': other['id'], 'from': other['path'],
                                     'to': str(target / path.relative_to(root))})
                if target == path.parent or target in path.parents or path.parent in target.parents:
                    raise self.error('Destination overlaps registered skill ' + other['id'])
            if not package_root and len(mappings) != 1:
                raise self.error('Skill folder overlaps registered skill; declare the complete package root')
            directory = self.lib.path.parent / 'relocations' / uuid.uuid4().hex
            directory.mkdir(parents=True, mode=0o700)
            sync_directory(directory.parent)
            sync_directory(self.lib.path.parent)
            record = {'version': 2, 'registry': str(self.lib.path), 'id': sid,
                      'from': str(root), 'to': str(target), 'manifest': manifest,
                      'source_identity': identity(root), 'mappings': mappings,
                      'record_identity': identity(directory)}
            self.save(directory, record, 'prepared')
            stage = directory / 'staging'
            try:
                shutil.copytree(root, stage, symlinks=True)
                self.owned(root, record['source_identity'], manifest)
                if self.manifest(stage) != manifest:
                    raise self.error('Copied folder differs from the original')
                record['target_identity'] = identity(stage)
                # Sync copied content before persisting the publish intent.
                for path in stage.rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        with path.open('rb') as stream:
                            os.fsync(stream.fileno())
                for path in sorted((p for p in stage.rglob('*') if p.is_dir() and not p.is_symlink()),
                                   key=lambda p: len(p.parts), reverse=True):
                    sync_directory(path)
                sync_directory(stage)
                self.save(directory, record, 'publishing')
                self.move(stage, target)
                self.save(directory, record, 'published')
                with self.lib.transaction() as live:
                    self.check_membership(live, mappings, root, target)
                    self.check_paths(live, mappings, 'from')
                    self.owned(root, record['source_identity'], manifest)
                    self.owned(target, record['target_identity'], manifest)
                    for mapping in mappings:
                        item = self.lib.entry(live, mapping['id'])
                        if item['placement'] != 'library':
                            raise self.error('Skill placement changed during relocation')
                        item['path'] = mapping['to']
                    def finish():
                        self.save(directory, record, 'rebound')
                        self.owned(root, record['source_identity'], manifest)
                        self.move(root, directory / 'original' / root.name)
                        self.save(directory, record, 'applied')
                    live.after_write.append(finish)
            except (OSError, self.error) as exc:
                raise self.error(str(exc) + '; recover with relocate-rollback ' + str(directory)) from exc
            return {'id': sid, 'from': str(root), 'to': str(target),
                    'archive': str(directory / 'original' / root.name),
                    'relocation': str(directory), 'skills': len(mappings)}

    def check_paths(self, state, mappings, field):
        if any(self.lib.entry(state, m['id'])['path'] != m[field] for m in mappings):
            raise self.error('Skill was rebound concurrently; relocation paths conflict')

    def check_membership(self, state, mappings, root, target, allow_source_additions=False,
                         check_target=True):
        actual = set()
        expected = {m['id'] for m in mappings}
        boundaries = (root, target) if check_target else (root,)
        for item in state['skills']:
            path = self.api.canonical_path(item['path'])
            if (allow_source_additions and item['id'] not in expected and root in path.parents
                    and target not in path.parents):
                continue
            if any(boundary in path.parents or path.parent == boundary or
                   path.parent in boundary.parents for boundary in boundaries):
                actual.add(item['id'])
        if actual != expected:
            raise self.error('Package registry membership changed; nothing else moved')

    def load_record(self, directory):
        if directory.is_symlink() or directory.parent != self.lib.path.parent / 'relocations':
            raise self.error('Relocation record must belong to this registry')
        record = json.loads((directory / 'relocation.json').read_text())
        if isinstance(record, dict) and record.get('version') == 1:
            record = self.upgrade_legacy(directory, record)
        if (not isinstance(record, dict) or record.get('version') != 2
                or record.get('registry') != str(self.lib.path)
                or record.get('record_identity') != identity(directory)
                or not isinstance(record.get('mappings'), list) or not record['mappings']):
            raise self.error('Invalid relocation record or registry identity')
        root, target = Path(record['from']), Path(record['to'])
        if (not root.is_absolute() or not target.is_absolute() or root == target
                or root in target.parents or target in root.parents
                or not isinstance(record.get('manifest'), dict)):
            raise self.error('Invalid relocation boundary')
        ids = set()
        for mapping in record['mappings']:
            before, after = Path(mapping['from']), Path(mapping['to'])
            if (mapping['id'] in ids or root not in before.parents
                    or after != target / before.relative_to(root)):
                raise self.error('Invalid relocation path mapping')
            ids.add(mapping['id'])
        return record

    def upgrade_legacy(self, directory, record):
        """Validate old journals against live registry and retained bytes first."""
        root, target = Path(record['from']), Path(record['to'])
        archive = directory / 'original' / root.name
        if (not root.is_absolute() or not target.is_absolute()
                or record.get('archive') != str(archive) or root == target
                or root in target.parents or target in root.parents):
            raise self.error('Invalid legacy relocation boundary')
        entry = self.lib.entry(self.lib.read(), record['id'])
        if (entry['source'] != record.get('source')
                or entry['path'] not in (str(root / 'SKILL.md'), str(target / 'SKILL.md'))):
            raise self.error('Legacy relocation does not match this registry')
        original = archive if exists(archive) else root
        copy = target if exists(target) else directory / 'library-copy'
        self.owned(original, None)
        manifest = self.manifest(original)
        def legacy(value):
            return {k: ('dir' if v.startswith('dir:') else v.split(':', 2)[2]
                        if v.startswith('file:') else v) for k, v in value.items() if k}
        if legacy(manifest) != record.get('manifest'):
            raise self.error('Legacy original changed; retained data needs review')
        target_identity = None
        if exists(copy):
            self.owned(copy, None, manifest)
            target_identity = identity(copy)
        upgraded = {'version': 2, 'registry': str(self.lib.path), 'id': record['id'],
                    'from': str(root), 'to': str(target), 'manifest': manifest,
                    'source_identity': identity(original), 'record_identity': identity(directory),
                    'mappings': [{'id': entry['id'], 'from': str(root / 'SKILL.md'),
                                  'to': str(target / 'SKILL.md')}]}
        if target_identity is not None:
            upgraded['target_identity'] = target_identity
        self.save(directory, upgraded, record.get('status', 'prepared'))
        return upgraded

    def rollback(self, record_dir):
        # Do not resolve a symlink here: a replacement record is not owned.
        raw = Path(record_dir).expanduser().absolute()
        if raw.is_symlink():
            raise self.error('Relocation record cannot be a symlink')
        directory = raw.parent.resolve() / raw.name
        with self.lock():
            record = self.load_record(directory)
            if record.get('status') == 'rolled_back':
                return {'id': record['id'], 'changed': False, 'status': 'already_rolled_back'}
            root, target = Path(record['from']), Path(record['to'])
            archive = directory / 'original' / root.name
            stage, saved = directory / 'staging', directory / 'library-copy'
            mappings, manifest = record['mappings'], record['manifest']
            with self.lib.transaction() as state:
                paths = [self.lib.entry(state, m['id'])['path'] for m in mappings]
                before = all(p == m['from'] for p, m in zip(paths, mappings))
                after = all(p == m['to'] for p, m in zip(paths, mappings))
                unpublished = bool(record.get('unpublished') or (
                    before and not exists(archive)
                    and (exists(stage)
                         or (record.get('target_identity') is None and not exists(saved)))))
                self.check_membership(state, mappings, root, target,
                                      allow_source_additions=before and not exists(archive),
                                      check_target=not unpublished)
                if not before and not after:
                    raise self.error('Skill was rebound concurrently; nothing restored')
                # Validate every owned tree before changing any directory or registry.
                if exists(archive):
                    if exists(root):
                        raise self.error('Original location is occupied: ' + str(root))
                    self.owned(archive, record['source_identity'], manifest)
                elif exists(root):
                    # Before publication the source is untouched: preserve user edits.
                    self.owned(root, record['source_identity'], manifest if after else None)
                else:
                    raise self.error('Original and archive are both unavailable; nothing restored')
                for path in ((saved,) if unpublished else (target, saved)):
                    if exists(path):
                        if path == target and record.get('target_identity') is None:
                            raise self.error('Library target ownership is unavailable; nothing restored')
                        self.owned(path, record.get('target_identity'),
                                   manifest if record.get('target_identity') is not None else None)
                if not unpublished and exists(target) and exists(saved):
                    raise self.error('Both Library target and saved copy exist; nothing restored')
                if after and not exists(target):
                    raise self.error('Active Library target is unavailable; nothing restored')
                if unpublished:
                    record['unpublished'] = True
                self.save(directory, record, 'rolling_back')
                if exists(archive):
                    self.move(archive, root)
                current = [self.lib.entry(state, m['id'])['path'] for m in mappings]
                if current == [m['to'] for m in mappings]:
                    for mapping in mappings:
                        self.lib.entry(state, mapping['id'])['path'] = mapping['from']
                elif current != [m['from'] for m in mappings]:
                    raise self.error('Skill was rebound during rollback; original retained at ' + str(root))
                def finish():
                    if not unpublished and exists(target):
                        self.owned(target, record.get('target_identity'), manifest)
                        self.move(target, saved)
                    elif exists(stage):
                        # A partial, never-published copy stays inside the owned record.
                        if stage.is_symlink():
                            raise self.error('Staging directory ownership changed')
                        self.move(stage, saved)
                    self.save(directory, record, 'rolled_back')
                state.after_write.append(finish)
            return {'id': record['id'], 'restored': str(root), 'changed': True, 'status': 'rolled_back'}
