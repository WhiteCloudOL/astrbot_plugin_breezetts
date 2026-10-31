"""Bound conversation-level speech frequency with monotonic timestamps."""

from dataclasses import dataclass
from time import monotonic


@dataclass
class SessionSpeech:
    """Retain only timestamps and opaque turn identities for a conversation."""

    last_sent: float | None = None
    last_turn: object | None = None
    pending_turn: object | None = None


class SpeechFrequency:
    """Share cooldown and in-flight reservations between automatic voice paths."""

    def __init__(self):
        self.sessions: dict[str, SessionSpeech] = {}
        self.max_sessions = 2048

    def can_speak(self, session: str, turn: object, interval: int) -> bool:
        """Check a turn without creating a conversation entry.

        Args:
            session: Full AstrBot conversation origin, or empty for an unscoped call.
            turn: Opaque identity shared by speech paths within this reply.
            interval: Minimum seconds between successful speech generations.

        Returns:
            Whether the turn can reserve speech generation now.
        """
        now = monotonic()
        for key, entry in list(self.sessions.items()):
            if entry.pending_turn is None and (
                entry.last_sent is None or now - entry.last_sent >= 3600
            ):
                del self.sessions[key]
        if not session:
            return True
        entry = self.sessions.get(session)
        if entry is None:
            return len(self.sessions) < self.max_sessions
        if entry.pending_turn is not None and entry.pending_turn is not turn:
            return False
        return (
            entry.last_turn is turn
            or entry.last_sent is None
            or now - entry.last_sent >= interval
        )

    def acquire(self, session: str, turn: object, interval: int) -> bool:
        """Reserve a conversation synchronously before awaiting synthesis.

        Args:
            session: Conversation origin.
            turn: Current reply's opaque identity.
            interval: Configured cooldown in seconds.

        Returns:
            Whether generation is reserved for this turn.
        """
        if not self.can_speak(session, turn, interval):
            return False
        if session:
            self.sessions.setdefault(session, SessionSpeech()).pending_turn = turn
        return True

    def finish(self, session: str, turn: object, generated: bool) -> None:
        """Release a reservation and count successfully generated speech.

        Args:
            session: Conversation origin.
            turn: Identity that acquired the reservation.
            generated: Whether audio was generated, including delivery failures.
        """
        entry = self.sessions.get(session)
        if entry is None or entry.pending_turn is not turn:
            return
        entry.pending_turn = None
        if generated:
            entry.last_sent = monotonic()
            entry.last_turn = turn
        elif entry.last_sent is None:
            del self.sessions[session]
