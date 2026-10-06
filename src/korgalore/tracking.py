"""Thread tracking management for korgalore.

This module provides functionality to track email threads via lei queries,
storing metadata in a separate manifest file from the main configuration.
"""

import json
import logging
import shutil
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from korgalore import run_lei_command

logger = logging.getLogger('korgalore')

# Auto-expire threads with no new messages after this many days
EXPIRE_DAYS = 30


class TrackStatus(Enum):
    """Status of a tracked thread."""

    ACTIVE = 'active'  # Updated during pull
    INACTIVE = 'inactive'  # Auto-expired, skipped during pull
    PAUSED = 'paused'  # User-requested pause, skipped during pull


@dataclass
class TrackedThread:
    """Represents a tracked email thread."""

    track_id: str
    msgid: str
    subject: str
    target: str
    labels: list[str]
    lei_path: Path
    created: datetime
    last_update: datetime
    last_new_message: datetime
    status: TrackStatus
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'msgid': self.msgid,
            'subject': self.subject,
            'target': self.target,
            'labels': self.labels,
            'lei_path': str(self.lei_path),
            'created': self.created.isoformat(),
            'last_update': self.last_update.isoformat(),
            'last_new_message': self.last_new_message.isoformat(),
            'status': self.status.value,
            'message_count': self.message_count,
        }

    @classmethod
    def from_dict(cls, track_id: str, data: dict[str, Any]) -> 'TrackedThread':
        """Create TrackedThread from dictionary."""
        return cls(
            track_id=track_id,
            msgid=data['msgid'],
            subject=data['subject'],
            target=data['target'],
            labels=data['labels'],
            lei_path=Path(data['lei_path']),
            created=datetime.fromisoformat(data['created']),
            last_update=datetime.fromisoformat(data['last_update']),
            last_new_message=datetime.fromisoformat(data['last_new_message']),
            status=TrackStatus(data['status']),
            message_count=data['message_count'],
        )


class TrackingManifest:
    """Manages the tracking manifest for monitored email threads.

    The manifest is stored as a JSON file separate from the main korgalore
    configuration, since tracked threads are ephemeral and user-driven.
    """

    MANIFEST_VERSION = 1

    def __init__(self, data_dir: Path) -> None:
        """Initialize the tracking manifest.

        Args:
            data_dir: The korgalore data directory (e.g., ~/.local/share/korgalore).
        """
        self.data_dir = data_dir
        self.manifest_path = data_dir / 'tracking.json'
        self.lei_base_dir = data_dir / 'lei'
        self._threads: dict[str, TrackedThread] = {}
        self._load()

    def _load(self) -> None:
        """Load the manifest from disk."""
        if not self.manifest_path.exists():
            logger.debug('No tracking manifest found, starting fresh')
            return

        try:
            with self.manifest_path.open(encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning('Failed to load tracking manifest: %s', e)
            return

        version = data.get('version', 1)
        if version != self.MANIFEST_VERSION:
            logger.warning('Tracking manifest version mismatch (got %d, expected %d)', version, self.MANIFEST_VERSION)

        threads_data = data.get('threads', {})
        for track_id, thread_data in threads_data.items():
            try:
                self._threads[track_id] = TrackedThread.from_dict(track_id, thread_data)
            except (KeyError, ValueError) as e:
                logger.warning('Failed to load tracked thread %s: %s', track_id, e)

        logger.debug('Loaded %d tracked threads from manifest', len(self._threads))

    def _save(self) -> None:
        """Save the manifest to disk."""
        data = {
            'version': self.MANIFEST_VERSION,
            'threads': {track_id: thread.to_dict() for track_id, thread in self._threads.items()},
        }

        # Ensure parent directory exists
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically via temp file
        tmp_path = self.manifest_path.with_suffix('.tmp')
        with tmp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        tmp_path.rename(self.manifest_path)

        logger.debug('Saved tracking manifest with %d threads', len(self._threads))

    def add_thread(
        self, track_id: str, msgid: str, subject: str, target: str, labels: list[str], lei_path: Path
    ) -> TrackedThread:
        """Add a new thread to track.

        Args:
            track_id: Unique identifier for this tracked thread.
            msgid: The message ID of the thread root.
            subject: Subject line of the thread.
            target: Name of the target for deliveries.
            labels: Labels to apply to delivered messages.
            lei_path: Path to the lei search directory.

        Returns:
            The newly created TrackedThread.
        """
        now = datetime.now(UTC)

        thread = TrackedThread(
            track_id=track_id,
            msgid=msgid,
            subject=subject,
            target=target,
            labels=labels,
            lei_path=lei_path,
            created=now,
            last_update=now,
            last_new_message=now,
            status=TrackStatus.ACTIVE,
            message_count=0,
        )

        self._threads[track_id] = thread
        self._save()

        logger.info('Started tracking thread %s: %s', track_id, subject)
        return thread

    def remove_thread(self, track_id: str, delete_data: bool = False) -> None:
        """Remove a thread from tracking.

        Args:
            track_id: The tracking ID to remove.
            delete_data: If True, also delete the lei search directory.

        Raises:
            KeyError: If the track_id is not found.
        """
        if track_id not in self._threads:
            raise KeyError(f"Tracked thread '{track_id}' not found")

        thread = self._threads[track_id]

        if delete_data and thread.lei_path.exists():
            logger.info('Deleting lei search data at %s', thread.lei_path)
            shutil.rmtree(thread.lei_path)

        del self._threads[track_id]
        self._save()

        logger.info('Stopped tracking thread %s', track_id)

    def pause_thread(self, track_id: str) -> None:
        """Pause tracking for a thread.

        Args:
            track_id: The tracking ID to pause.

        Raises:
            KeyError: If the track_id is not found.
        """
        if track_id not in self._threads:
            raise KeyError(f"Tracked thread '{track_id}' not found")

        self._threads[track_id].status = TrackStatus.PAUSED
        self._save()

        logger.info('Paused tracking for thread %s', track_id)

    def resume_thread(self, track_id: str) -> None:
        """Resume tracking for a paused or inactive thread.

        Args:
            track_id: The tracking ID to resume.

        Raises:
            KeyError: If the track_id is not found.
        """
        if track_id not in self._threads:
            raise KeyError(f"Tracked thread '{track_id}' not found")

        thread = self._threads[track_id]
        thread.status = TrackStatus.ACTIVE
        thread.last_new_message = datetime.now(UTC)
        self._save()

        logger.info('Resumed tracking for thread %s', track_id)

    def get_thread(self, track_id: str) -> TrackedThread:
        """Get a tracked thread by ID.

        Args:
            track_id: The tracking ID.

        Returns:
            The TrackedThread.

        Raises:
            KeyError: If the track_id is not found.
        """
        if track_id not in self._threads:
            raise KeyError(f"Tracked thread '{track_id}' not found")
        return self._threads[track_id]

    def get_thread_by_msgid(self, msgid: str) -> TrackedThread | None:
        """Find a tracked thread by message ID.

        Args:
            msgid: The message ID to search for.

        Returns:
            The TrackedThread if found, None otherwise.
        """
        for thread in self._threads.values():
            if thread.msgid == msgid:
                return thread
        return None

    def get_all_threads(self) -> list[TrackedThread]:
        """Get all tracked threads.

        Returns:
            List of all TrackedThread objects.
        """
        return list(self._threads.values())

    def get_active_threads(self) -> list[TrackedThread]:
        """Get only active tracked threads.

        Returns:
            List of TrackedThread objects with ACTIVE status.
        """
        return [t for t in self._threads.values() if t.status == TrackStatus.ACTIVE]

    def get_inactive_threads(self) -> list[TrackedThread]:
        """Get inactive and paused tracked threads.

        Returns:
            List of TrackedThread objects with INACTIVE or PAUSED status.
        """
        return [t for t in self._threads.values() if t.status in (TrackStatus.INACTIVE, TrackStatus.PAUSED)]

    def check_and_expire_threads(self) -> list[str]:
        """Check for threads that should be auto-expired.

        Threads with no new messages for EXPIRE_DAYS are marked inactive.

        Returns:
            List of track_ids that were expired.
        """
        expired: list[str] = []
        cutoff = datetime.now(UTC) - timedelta(days=EXPIRE_DAYS)

        for track_id, thread in self._threads.items():
            if thread.status == TrackStatus.ACTIVE and thread.last_new_message < cutoff:
                thread.status = TrackStatus.INACTIVE
                expired.append(track_id)
                logger.info('Auto-expired thread %s (no activity since %s)', track_id, thread.last_new_message.date())

        if expired:
            self._save()

        return expired

    def update_activity(self, track_id: str, new_messages: int) -> None:
        """Update activity timestamps after processing a thread.

        Args:
            track_id: The tracking ID.
            new_messages: Number of new messages delivered.

        Raises:
            KeyError: If the track_id is not found.
        """
        if track_id not in self._threads:
            raise KeyError(f"Tracked thread '{track_id}' not found")

        thread = self._threads[track_id]
        now = datetime.now(UTC)
        thread.last_update = now

        if new_messages > 0:
            thread.last_new_message = now
            thread.message_count += new_messages

        self._save()


def write_archive_description(archive_path: Path, description: str) -> None:
    """Name a lei-created v2 archive for public-inbox's benefit.

    lei's `-o v2:' writer never creates the `description' file public-inbox
    looks for, so archives served over the web are labelled
    "($INBOX_DIR/description missing)" instead of anything meaningful. Write
    one ourselves, the same way `lei mirror' does for the inboxes it clones.

    public-inbox reads this as a single line, collapsing runs of whitespace,
    so anything longer is flattened rather than wrapped.

    A description is cosmetic, so failing to write one is logged and
    otherwise ignored -- it must never take an archive down with it.

    Args:
        archive_path: The v2 archive directory (lei's -o v2: target).
        description: Free text; whitespace is collapsed and blanks skipped.
    """
    text = ' '.join(description.split())
    if not text:
        return

    try:
        (archive_path / 'description').write_text(f'{text}\n', encoding='utf-8')
    except OSError as e:
        logger.warning('Could not describe archive %s: %s', archive_path, e)


def create_lei_thread_search(msgid: str, output_path: Path) -> tuple[int, bytes]:
    """Create a new lei search for a thread by message ID.

    Uses: lei q "dt:19700101000000.." --only https://lore.kernel.org/all/<msgid> -o v2:<output_path>

    Args:
        msgid: The message ID to search for (without angle brackets).
        output_path: Path where the lei search will be created.

    Returns:
        Tuple of (return_code, output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    # Ensure output directory's parent exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # We use the dt:19700101000000.. query to mean "search for all", it's the Message-ID in the url that
    # returns the full thread of that message.
    qmsgid = urllib.parse.quote_plus(msgid)
    args = ['q', 'dt:19700101000000..', '--only', f'https://lore.kernel.org/all/{qmsgid}', '-o', f'v2:{output_path}']
    logger.debug('Creating lei thread search: lei %s', ' '.join(args))

    return run_lei_command(args)


def create_lei_query_search(query: str, output_path: Path, threads: bool = False) -> tuple[int, bytes]:
    """Create a new lei search with an arbitrary query string.

    Uses: lei q --stdin [--threads] --only https://lore.kernel.org/all -o v2:<output_path>

    The query goes to lei on standard input, not as an argument. lei turns
    each argument that contains whitespace into a phrase search, so a whole
    query passed as one argument matches nothing. A query read from stdin
    is used as written, and lei saves it that way for 'lei up' as well.

    Args:
        query: The lei query string (e.g., 'd:30.days.ago.. AND a:foo@bar.com').
        output_path: Path where the lei search will be created.
        threads: If True, include entire email threads when any message matches.
                 This can result in many more results but is useful for following
                 discussions.

    Returns:
        Tuple of (return_code, output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    # Ensure output directory's parent exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ['q', '--stdin']
    if threads:
        args.append('--threads')
    args.extend(['--only', 'https://lore.kernel.org/all', '-o', f'v2:{output_path}'])
    logger.debug('Creating lei query search: lei %s <<< %s', ' '.join(args), query)

    return run_lei_command(args, stdin=query.encode())


def update_lei_search(search_path: Path) -> tuple[int, bytes]:
    """Update an existing lei search.

    Args:
        search_path: Path to the lei search directory.

    Returns:
        Tuple of (return_code, output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    args = ['up', str(search_path)]
    return run_lei_command(args)


def forget_lei_search(search_path: Path) -> tuple[int, bytes]:
    """Forget a lei search and remove its data.

    Runs 'lei forget-search' to remove the search from lei's tracking
    and delete the associated data.

    Args:
        search_path: Path to the lei search directory.

    Returns:
        Tuple of (return_code, output).

    Raises:
        PublicInboxError: If the lei command is not found.
    """
    args = ['forget-search', str(search_path)]
    return run_lei_command(args)
