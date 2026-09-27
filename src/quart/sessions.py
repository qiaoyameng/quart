from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Collection
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import UTC
from typing import Any
from typing import cast
from typing import TYPE_CHECKING

from flask.sessions import NullSession as NullSession  # noqa: F401
from flask.sessions import SecureCookieSession as SecureCookieSession  # noqa: F401
from flask.sessions import (  # noqa: F401
    session_json_serializer as session_json_serializer,
)
from flask.sessions import SessionMixin as SessionMixin  # noqa: F401
from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer
from werkzeug.datastructures import CallbackDict
from werkzeug.wrappers import Response as WerkzeugResponse

from .wrappers import BaseRequestWebsocket
from .wrappers import Response

if TYPE_CHECKING:
    from .app import Quart  # noqa


class SessionInterface:
    """Base class for session interfaces.

    Attributes:
        null_session_class: Storage class for null (no storage)
            sessions.
        pickle_based: Indicates if pickling is used for the session.
    """

    null_session_class = NullSession
    pickle_based = False

    async def make_null_session(self, app: Quart) -> NullSession:
        """Create a Null session object.

        This is used in replacement of an actual session if sessions
        are not configured or active.
        """
        return self.null_session_class()

    def is_null_session(self, instance: object) -> bool:
        """Returns True is the instance is a null session."""
        return isinstance(instance, self.null_session_class)

    def get_cookie_name(self, app: Quart) -> str:
        """Helper method to return the Cookie Name for the App."""
        return app.config["SESSION_COOKIE_NAME"]

    def get_cookie_domain(self, app: Quart) -> str | None:
        """Helper method to return the Cookie Domain for the App."""
        rv = app.config["SESSION_COOKIE_DOMAIN"]
        return rv if rv else None

    def get_cookie_partitioned(self, app: Quart) -> bool:
        """Helper method to return the Cookie partitioned setting for the App."""
        return app.config["SESSION_COOKIE_PARTITIONED"]

    def get_cookie_path(self, app: Quart) -> str:
        """Helper method to return the Cookie path for the App."""
        return app.config["SESSION_COOKIE_PATH"] or app.config["APPLICATION_ROOT"]

    def get_cookie_httponly(self, app: Quart) -> bool:
        """Helper method to return if the Cookie should be HTTPOnly for the App."""
        return app.config["SESSION_COOKIE_HTTPONLY"]

    def get_cookie_secure(self, app: Quart) -> bool:
        """Helper method to return if the Cookie should be Secure for the App."""
        return app.config["SESSION_COOKIE_SECURE"]

    def get_cookie_samesite(self, app: Quart) -> str:
        """Helper method to return the Cookie Samesite configuration for the App."""
        return app.config["SESSION_COOKIE_SAMESITE"]

    def get_expiration_time(self, app: Quart, session: SessionMixin) -> datetime | None:
        """Helper method to return the Session expiration time.

        If the session is not 'permanent' it will expire as and when
        the browser stops accessing the app.
        """
        if session.permanent:
            return datetime.now(UTC) + app.permanent_session_lifetime
        else:
            return None

    def should_set_cookie(self, app: Quart, session: SessionMixin) -> bool:
        """Helper method to return if the Set Cookie header should be present.

        This triggers if the session is marked as modified or the app
        is configured to always refresh the cookie.
        """
        if session.modified:
            return True
        save_each = app.config["SESSION_REFRESH_EACH_REQUEST"]
        return save_each and session.permanent

    async def open_session(
        self, app: Quart, request: BaseRequestWebsocket
    ) -> SessionMixin | None:
        """Open an existing session from the request or create one.

        Returns:
            The Session object or None if no session can be created,
            in which case the :attr:`null_session_class` is expected
            to be used.
        """
        raise NotImplementedError()

    async def save_session(
        self,
        app: Quart,
        session: SessionMixin,
        response: Response | WerkzeugResponse | None,
    ) -> None:
        """Save the session argument to the response.

        Arguments:
            response: Can be None if the session is being saved after
                a websocket connection closes.

        Returns:
            The modified response, with the session stored.

        """
        raise NotImplementedError()


class SecureCookieSessionInterface(SessionInterface):
    """A Session interface that uses cookies as storage.

    This will store the data on the cookie in plain text, but with a
    signature to prevent modification.
    """

    digest_method = staticmethod(hashlib.sha1)
    key_derivation = "hmac"
    salt = "cookie-session"
    serializer = session_json_serializer
    session_class = SecureCookieSession

    def get_signing_serializer(self, app: Quart) -> URLSafeTimedSerializer | None:
        """Return a serializer for the session that also signs data.

        This will return None if the app is not configured for secrets.
        """
        if not app.secret_key:
            return None

        keys: list[str | bytes] = []

        if fallbacks := app.config["SECRET_KEY_FALLBACKS"]:
            keys.extend(fallbacks)

        keys.append(app.secret_key)  # itsdangerous expects current key at top
        options = {
            "key_derivation": self.key_derivation,
            "digest_method": self.digest_method,
        }
        return URLSafeTimedSerializer(
            keys,  # type: ignore[arg-type]
            salt=self.salt,
            serializer=self.serializer,
            signer_kwargs=options,
        )

    async def open_session(
        self, app: Quart, request: BaseRequestWebsocket
    ) -> SecureCookieSession | None:
        """Open a secure cookie based session.

        This will return None if a signing serializer is not available,
        usually if the config SECRET_KEY is not set.
        """
        signer = self.get_signing_serializer(app)
        if signer is None:
            return None

        cookie = request.cookies.get(self.get_cookie_name(app))
        if cookie is None:
            return self.session_class()
        max_age = int(app.permanent_session_lifetime.total_seconds())
        try:
            data = signer.loads(cookie, max_age=max_age)
            return self.session_class(data)
        except BadSignature:
            return self.session_class()

    async def save_session(
        self,
        app: Quart,
        session: SessionMixin,
        response: Response | WerkzeugResponse | None,
    ) -> None:
        """Saves the session to the response in a secure cookie."""
        if response is None:
            if session.modified:
                app.logger.exception(
                    "Secure Cookie Session modified during websocket handling. "
                    "These modifications will be lost as a cookie cannot be set."
                )
            return

        name = self.get_cookie_name(app)
        domain = self.get_cookie_domain(app)
        partitioned = self.get_cookie_partitioned(app)
        path = self.get_cookie_path(app)
        secure = self.get_cookie_secure(app)
        samesite = self.get_cookie_samesite(app)
        httponly = self.get_cookie_httponly(app)

        # Add a "Vary: Cookie" header if the session was accessed at all.
        if session.accessed:
            response.vary.add("Cookie")

        # If the session is modified to be empty, remove the cookie.
        # If the session is empty, return without setting the cookie.
        if not session:
            if session.modified:
                response.delete_cookie(
                    name,
                    domain=domain,
                    partitioned=partitioned,
                    path=path,
                    secure=secure,
                    samesite=samesite,
                    httponly=httponly,
                )
                response.vary.add("Cookie")

            return

        if not self.should_set_cookie(app, session):
            return

        expires = self.get_expiration_time(app, session)
        val = self.get_signing_serializer(app).dumps(dict(session))
        response.set_cookie(
            name,
            val,
            expires=expires,
            httponly=httponly,
            domain=domain,
            partitioned=partitioned,
            path=path,
            secure=secure,
            samesite=samesite,
        )
        response.vary.add("Cookie")


class SessionConflictError(Exception):
    """Raised when a session changeset cannot be committed cleanly.

    This indicates that another request modified the same top-level
    session keys (or deleted keys this request wrote) since the
    session was read. The conflicting keys are available on
    :attr:`conflicts`; no side of the conflict is silently discarded.

    Attributes:
        sid: The session id the conflict occurred on.
        conflicts: The top-level keys with conflicting changes.
    """

    def __init__(self, sid: str, conflicts: Collection[str]) -> None:
        self.sid = sid
        self.conflicts = frozenset(conflicts)
        super().__init__(
            f"Session {sid!r} has conflicting changes for keys: "
            f"{sorted(self.conflicts)}"
        )


@dataclass(frozen=True)
class SessionChangeset:
    """The changes made to a session relative to the version read.

    Both attributes are keyed by top-level session key; nested values
    are treated as part of their top-level key's value.

    Attributes:
        sets: Mapping of top-level keys to their new values.
        deletes: Top-level keys to remove.
    """

    sets: dict[str, Any] = field(default_factory=dict)
    deletes: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.sets or self.deletes)


class VersionedSession(CallbackDict, SessionMixin):
    """A session that computes a changeset against the version read.

    Attributes:
        sid: The session id used by the backing store, or None if the
            session has not been saved yet.
        version: The store version the session data was read at.
    """

    modified = False

    def __init__(
        self,
        initial: Mapping[str, Any] | None = None,
        *,
        sid: str | None = None,
        version: int = 0,
        new: bool = True,
    ) -> None:
        def on_update(self: VersionedSession) -> None:
            self.modified = True

        super().__init__(initial, on_update)
        self.sid = sid
        self.version = version
        self.new = new
        # A deep copy so that in-place mutations of nested values are
        # detected as changes to their top-level key.
        self._snapshot = deepcopy(dict(self))

    @property
    def changeset(self) -> SessionChangeset:
        """The changes made since the session was opened or last saved.

        Nested values are treated as part of their top-level key, so
        mutating a nested mapping marks the whole top-level key as
        set. Keys removed by :meth:`clear` are only those present when
        the session was opened, so keys concurrently added by other
        requests are never deleted.
        """
        sets = {
            key: value
            for key, value in self.items()
            if key not in self._snapshot or self._snapshot[key] != value
        }
        deletes = frozenset(key for key in self._snapshot if key not in self)
        return SessionChangeset(sets, deletes)

    def _rebase(self, version: int) -> None:
        """Mark the current state as the state saved in the store."""
        self._snapshot = deepcopy(dict(self))
        self.version = version
        self.new = False
        self.modified = False


class VersionedSessionInterface(SessionInterface):
    """Base class for server-side sessions with optimistic concurrency.

    The session data is stored server-side; the cookie only carries
    the session id. On save, the changes made relative to the version
    read are committed to the store. Changes to different top-level
    keys merge; concurrent changes to the same key (including delete
    versus write) raise :class:`SessionConflictError` rather than
    silently discarding one side.

    Subclasses must implement :meth:`load` and :meth:`commit`.
    """

    session_class = VersionedSession
    sid_bytes = 32

    def generate_sid(self) -> str:
        """Return a new unique session id."""
        return secrets.token_urlsafe(self.sid_bytes)

    async def load(self, sid: str) -> tuple[dict[str, Any], int] | None:
        """Return the stored ``(data, version)`` for the *sid*.

        Must return None if the sid is unknown. The returned data must
        be safe for the caller to mutate.
        """
        raise NotImplementedError()

    async def commit(
        self, sid: str, base_version: int, changeset: SessionChangeset
    ) -> int:
        """Atomically apply the *changeset* to the session *sid*.

        The changeset applies to the state at *base_version*. Must
        raise :class:`SessionConflictError` if any key in the
        changeset was concurrently changed after *base_version*.
        Returns the new version of the session.
        """
        raise NotImplementedError()

    async def open_session(
        self, app: Quart, request: BaseRequestWebsocket
    ) -> VersionedSession:
        """Open the session identified by the request's sid cookie."""
        sid = request.cookies.get(self.get_cookie_name(app))
        if sid is not None:
            stored = await self.load(sid)
            if stored is not None:
                data, version = stored
                return self.session_class(
                    data, sid=sid, version=version, new=False
                )
        return self.session_class()

    async def save_session(
        self,
        app: Quart,
        session: SessionMixin,
        response: Response | WerkzeugResponse | None,
    ) -> None:
        """Commit the session changeset and set the sid cookie.

        The commit is awaited before this returns, so for HTTP
        requests the data is stored before the response headers are
        sent. For websockets (*response* is None) the changes are
        still committed to the store once the connection closes; a
        conflicting commit raises :class:`SessionConflictError`.
        """
        session = cast(VersionedSession, session)
        changeset = session.changeset
        session.modified = bool(changeset)
        should_set_cookie = self.should_set_cookie(app, session)

        if changeset:
            if session.sid is None:
                session.sid = self.generate_sid()
            version = await self.commit(session.sid, session.version, changeset)
            session._rebase(version)

        if response is None:
            # A websocket connection: the changes (if any) have been
            # committed above; a cookie cannot be set.
            return

        name = self.get_cookie_name(app)
        domain = self.get_cookie_domain(app)
        partitioned = self.get_cookie_partitioned(app)
        path = self.get_cookie_path(app)
        secure = self.get_cookie_secure(app)
        samesite = self.get_cookie_samesite(app)
        httponly = self.get_cookie_httponly(app)

        if session.accessed:
            response.vary.add("Cookie")

        # If the session is modified to be empty, remove the cookie.
        # If the session is empty, return without setting the cookie.
        if not session:
            if changeset:
                response.delete_cookie(
                    name,
                    domain=domain,
                    partitioned=partitioned,
                    path=path,
                    secure=secure,
                    samesite=samesite,
                    httponly=httponly,
                )
                response.vary.add("Cookie")

            return

        if not should_set_cookie:
            return

        expires = self.get_expiration_time(app, session)
        response.set_cookie(
            name,
            session.sid,
            expires=expires,
            httponly=httponly,
            domain=domain,
            partitioned=partitioned,
            path=path,
            secure=secure,
            samesite=samesite,
        )
        response.vary.add("Cookie")


@dataclass
class _StoredSession:
    data: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    # The version each top-level key was last set or deleted at.
    # Deleted keys keep their entry (a tombstone) so that a delete
    # followed by a stale write is still detected as a conflict.
    key_versions: dict[str, int] = field(default_factory=dict)


class InMemorySessionStore:
    """A reference in-memory store for versioned sessions.

    Keeps a per-key version so that concurrent commits touching
    different top-level keys merge, whilst commits touching the same
    key raise :class:`SessionConflictError`. The data is local to this
    process and lost on restart.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _StoredSession] = {}
        self._lock = asyncio.Lock()

    async def load(self, sid: str) -> tuple[dict[str, Any], int] | None:
        """Return a ``(data, version)`` copy for *sid*, or None."""
        async with self._lock:
            stored = self._sessions.get(sid)
            if stored is None:
                return None
            return dict(stored.data), stored.version

    async def commit(
        self, sid: str, base_version: int, changeset: SessionChangeset
    ) -> int:
        """Apply the *changeset* if no touched key changed since *base_version*."""
        async with self._lock:
            stored = self._sessions.get(sid)
            if stored is None:
                if base_version != 0:
                    raise SessionConflictError(
                        sid, {*changeset.sets, *changeset.deletes}
                    )
                stored = _StoredSession()
                self._sessions[sid] = stored

            keys = {*changeset.sets, *changeset.deletes}
            conflicts = sorted(
                key
                for key in keys
                if stored.key_versions.get(key, 0) > base_version
            )
            if conflicts:
                raise SessionConflictError(sid, conflicts)

            for key in changeset.deletes:
                stored.data.pop(key, None)
            stored.data.update(changeset.sets)
            stored.version += 1
            for key in keys:
                stored.key_versions[key] = stored.version
            return stored.version


class InMemorySessionInterface(VersionedSessionInterface):
    """A versioned session interface using an in-memory store.

    This is a reference implementation suitable for development and
    testing; the data is not shared between processes and is lost on
    restart.
    """

    def __init__(self, store: InMemorySessionStore | None = None) -> None:
        self.store = store if store is not None else InMemorySessionStore()

    async def load(self, sid: str) -> tuple[dict[str, Any], int] | None:
        return await self.store.load(sid)

    async def commit(
        self, sid: str, base_version: int, changeset: SessionChangeset
    ) -> int:
        return await self.store.commit(sid, base_version, changeset)
