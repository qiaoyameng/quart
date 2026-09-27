from __future__ import annotations

import asyncio
import copy
import hashlib
import secrets
from datetime import datetime
from datetime import UTC
from typing import Any
from typing import TYPE_CHECKING

from flask.sessions import NullSession as NullSession  # noqa: F401
from flask.sessions import SecureCookieSession as SecureCookieSession  # noqa: F401
from flask.sessions import (  # noqa: F401
    session_json_serializer as session_json_serializer,
)
from flask.sessions import SessionMixin as SessionMixin  # noqa: F401
from itsdangerous import BadSignature
from itsdangerous import URLSafeSerializer
from itsdangerous import URLSafeTimedSerializer
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


class SessionConflictError(RuntimeError):
    """Raised when a session changeset cannot be merged into the store.

    This indicates that another request modified the same session after
    this session was read, and at least one top-level key was changed by
    both. The conflicting keys are reported rather than silently
    resolved.

    Attributes:
        session_id: The id of the session that failed to save.
        conflicts: A mapping of conflicting top-level keys to the kind
            of local change, either ``"write"`` or ``"delete"``.
    """

    def __init__(self, session_id: str, conflicts: dict[str, str]) -> None:
        self.session_id = session_id
        self.conflicts = conflicts
        keys = ", ".join(sorted(conflicts))
        super().__init__(
            f"Session {session_id!r} has conflicting changes for keys: {keys}"
        )


class VersionedSession(dict, SessionMixin):  # type: ignore[type-arg]
    """A server-side session that tracks a changeset against a version.

    The session is loaded from a store at a specific ``version`` and
    remembers a snapshot of the data as read. On save a changeset of
    top-level key writes and deletes (relative to that snapshot) is
    computed and submitted to the store along with the read version.

    Nested values are treated as a whole by their top-level key;
    in-place mutations of nested structures are detected by comparing
    against the snapshot, but two concurrent changes to the same
    top-level key conflict even if they touch different nested keys.

    Attributes:
        version: The store version this session was read at.
        session_id: The id of this session in the store.
        new: True if this session did not exist in the store.
    """

    modified = False

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        *,
        version: int = 0,
        session_id: str | None = None,
        new: bool = False,
    ) -> None:
        data = dict(initial or {})
        super().__init__(data)
        self.version = version
        self.session_id = session_id
        self.new = new
        self._snapshot = copy.deepcopy(data)

    def __setitem__(self, key: str, value: Any) -> None:
        self.modified = True
        self.accessed = True
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        self.modified = True
        self.accessed = True
        super().__delitem__(key)

    def clear(self) -> None:
        # Only keys present in the snapshot are deleted, keys added
        # concurrently by other requests are unknown here and survive.
        for key in list(self.keys()):
            del self[key]

    def pop(self, key: str, *args: Any) -> Any:
        if key in self:
            self.modified = True
            self.accessed = True
        return super().pop(key, *args)

    def popitem(self) -> Any:
        self.modified = True
        self.accessed = True
        return super().popitem()

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self.modified = True
            self.accessed = True
        return super().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        if dict(*args, **kwargs):
            self.modified = True
            self.accessed = True
        super().update(*args, **kwargs)

    def changeset(self) -> tuple[dict[str, Any], set[str]]:
        """Return the (writes, deletes) relative to the read snapshot."""
        writes = {}
        for key, value in self.items():
            if key not in self._snapshot or self._snapshot[key] != value:
                writes[key] = value
        deletes = set(self._snapshot) - set(self)
        return writes, deletes

    def _commit(self, version: int) -> None:
        """Mark the current state as saved at the given version."""
        self.version = version
        self.new = False
        self.modified = False
        self._snapshot = copy.deepcopy(dict(self))


class VersionedSessionStore:
    """Base class for stores used by the versioned session interface.

    Implementations must apply changesets atomically: either the whole
    changeset is merged and a new version returned, or a
    :class:`SessionConflictError` is raised and nothing is changed.
    """

    async def load(self, session_id: str) -> tuple[int, dict[str, Any]]:
        """Return the (version, data) for the session id.

        Returns ``(0, {})`` if the session id is unknown.
        """
        raise NotImplementedError()

    async def apply(
        self,
        session_id: str,
        base_version: int,
        writes: dict[str, Any],
        deletes: set[str],
    ) -> int:
        """Merge the changeset read at ``base_version`` into the store.

        Changes to keys not touched since ``base_version`` must merge;
        keys written or deleted by both must raise
        :class:`SessionConflictError`. Returns the new version.
        """
        raise NotImplementedError()


class InMemorySessionStore(VersionedSessionStore):
    """A reference in-memory :class:`VersionedSessionStore`.

    This keeps the data, a monotonic version and a per-key version
    (the session version at which the key was last written) in
    memory. It is suitable for tests, development and single-process
    deployments only.
    """

    def __init__(self) -> None:
        # session_id -> (version, data, key -> version last written)
        self._entries: dict[str, tuple[int, dict[str, Any], dict[str, int]]] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> tuple[int, dict[str, Any]]:
        async with self._lock:
            version, data, _ = self._entries.get(session_id, (0, {}, {}))
            return version, dict(data)

    async def apply(
        self,
        session_id: str,
        base_version: int,
        writes: dict[str, Any],
        deletes: set[str],
    ) -> int:
        async with self._lock:
            version, data, key_versions = self._entries.get(session_id, (0, {}, {}))
            if version != base_version:
                conflicts: dict[str, str] = {}
                for key in writes:
                    if key_versions.get(key, 0) > base_version:
                        conflicts[key] = "write"
                for key in deletes:
                    if key_versions.get(key, 0) > base_version:
                        conflicts[key] = "delete"
                if conflicts:
                    raise SessionConflictError(session_id, conflicts)

            new_version = version + 1
            new_data = dict(data)
            new_key_versions = dict(key_versions)
            for key, value in writes.items():
                new_data[key] = value
                new_key_versions[key] = new_version
            for key in deletes:
                new_data.pop(key, None)
                new_key_versions.pop(key, None)

            if new_data:
                self._entries[session_id] = (new_version, new_data, new_key_versions)
            else:
                # An emptied session is removed, keys added concurrently
                # by other requests keep the session alive.
                self._entries.pop(session_id, None)
            return new_version


class VersionedSessionInterface(SessionInterface):
    """A server-side session interface with optimistic concurrency.

    Sessions are stored server-side in a :class:`VersionedSessionStore`
    (see :meth:`get_store`); only a signed, otherwise meaningless,
    session id is kept in the client cookie. On save the changeset
    relative to the version read is submitted to the store, allowing
    concurrent requests to merge changes to different keys whilst
    conflicting changes to the same key raise
    :class:`SessionConflictError`.

    The session is saved to the store during response finalization,
    before the response headers are sent. For websockets the session is
    saved to the store after the handler finishes (response is None);
    modifications are therefore persisted, or a
    :class:`SessionConflictError` is raised, but never silently lost.

    Attributes:
        session_class: The session class to use.
    """

    salt = "session-id"
    session_class = VersionedSession

    def get_store(self, app: Quart) -> VersionedSessionStore:
        """Return the store to save the sessions to."""
        raise NotImplementedError()

    def generate_session_id(self) -> str:
        """Return a new unique session id."""
        return secrets.token_urlsafe(32)

    def get_signing_serializer(self, app: Quart) -> URLSafeSerializer | None:
        """Return a serializer that signs the session id cookie."""
        if not app.secret_key:
            return None

        keys: list[str | bytes] = []
        if fallbacks := app.config["SECRET_KEY_FALLBACKS"]:
            keys.extend(fallbacks)
        keys.append(app.secret_key)
        return URLSafeSerializer(keys, salt=self.salt)  # type: ignore[arg-type]

    async def open_session(
        self, app: Quart, request: BaseRequestWebsocket
    ) -> VersionedSession | None:
        """Open the session identified by the request cookie.

        This will return None if a signing serializer is not available,
        usually if the config SECRET_KEY is not set.
        """
        signer = self.get_signing_serializer(app)
        if signer is None:
            return None

        session_id = None
        cookie = request.cookies.get(self.get_cookie_name(app))
        if cookie is not None:
            try:
                session_id = signer.loads(cookie)
            except BadSignature:
                session_id = None

        if session_id is None:
            return self.session_class(
                session_id=self.generate_session_id(), new=True
            )

        version, data = await self.get_store(app).load(session_id)
        return self.session_class(
            data, version=version, session_id=session_id, new=False
        )

    async def save_session(
        self,
        app: Quart,
        session: SessionMixin,
        response: Response | WerkzeugResponse | None,
    ) -> None:
        """Commit the session changeset to the store, then the cookie.

        The changeset is committed to the store first, so the save
        completes before the response headers are sent. If the response
        is None (a handled websocket) the changes are still committed
        to the store, only the cookie cannot be set.
        """
        if not isinstance(session, VersionedSession):
            raise TypeError(
                f"Expected a VersionedSession instance, got {type(session)!r}"
            )

        if session.session_id is None:
            session.session_id = self.generate_session_id()

        # The changeset diff against the snapshot is authoritative, as
        # in-place mutations of nested values do not set modified.
        writes, deletes = session.changeset()
        modified = session.modified or bool(writes or deletes)
        if writes or deletes:
            version = await self.get_store(app).apply(
                session.session_id,
                session.version,
                writes,
                deletes,
            )
            session._commit(version)

        if response is None:
            # A websocket that has been accepted cannot receive a
            # cookie, the changes above are still saved to the store.
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
            if modified:
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

        save_each = app.config["SESSION_REFRESH_EACH_REQUEST"]
        if not (modified or (save_each and session.permanent)):
            return

        expires = self.get_expiration_time(app, session)
        val = self.get_signing_serializer(app).dumps(session.session_id)  # type: ignore[union-attr]
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


class InMemorySessionInterface(VersionedSessionInterface):
    """A reference versioned session interface using in-memory storage.

    This is suitable for tests, development and single-process
    deployments only. Use a custom :class:`VersionedSessionInterface`
    subclass backed by a shared store for multi-process deployments.
    """

    def __init__(self, store: InMemorySessionStore | None = None) -> None:
        self.store = store or InMemorySessionStore()

    def get_store(self, app: Quart) -> InMemorySessionStore:
        return self.store
