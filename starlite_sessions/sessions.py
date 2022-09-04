from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union, cast

from pydantic import validator
from starlite.exceptions import NotAuthorizedException
from starlite.middleware import ExceptionHandlerMiddleware
from starlite.middleware.authentication import (
    AbstractAuthenticationMiddleware,
    AuthenticationResult,
)
from starlite.middleware.base import DefineMiddleware, MiddlewareProtocol
from starlite.middleware.session import SessionCookieConfig, SessionMiddleware
from starlite.types import Empty, SyncOrAsyncUnion
from starlite.utils import AsyncCallable

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection
    from starlite.app import Starlite
    from starlite.types import ASGIApp, Receive, Scope, Send


RetrieveUserHandler = Callable[[Dict[str, Any]], SyncOrAsyncUnion[Any]]


class SessionAuthConfig(SessionCookieConfig):
    retrieve_user_handler: RetrieveUserHandler
    """
    Callable that receives the session dictionary after it has been decoded and returns a 'user' value.

    Notes:
    - User can be any arbitrary value,
    - The callable can be sync or async.
    """
    exclude: Optional[Union[str, List[str]]] = None
    """
    A pattern or list of patterns to skip in the authentication middleware.
    """

    @validator("retrieve_user_handler")
    def validate_retrieve_user_handler(  # pylint: disable=no-self-argument
        cls, value: RetrieveUserHandler
    ) -> AsyncCallable[[Dict[str, Any]], Any]:
        """This validator ensures that the passed in value does not get bound.

        Args:
            value: A callable fulfilling the RetrieveUserHandler type.

        Returns:
            An instance of AsyncCallable wrapping the callable.
        """
        return AsyncCallable(value)

    @property
    def middleware(self) -> DefineMiddleware:
        """Use this property to insert the config into a middleware list on one
        of the application layers.

        Examples:

            ```python
            from typing import Any
            from os import urandom

            from starlite import Starlite, Request, get
            from starlite_session import SessionAuthConfig


            async def retrieve_user_from_session(session: dict[str, Any]) -> Any:
                # implement logic here to retrieve a 'user' datum given the session dictionary
                ...


            session_auth_config = SessionAuthConfig(
                secret=urandom(16), retrieve_user_handler=retrieve_user_from_session
            )


            @get("/")
            def my_handler(request: Request) -> None:
                ...


            app = Starlite(route_handlers=[my_handler], middleware=[session_auth_config.middleware])
            ```

        Returns:
            An instance of DefineMiddleware including 'self' as the config kwarg value.
        """
        return DefineMiddleware(MiddlewareWrapper, config=self)


class MiddlewareWrapper(MiddlewareProtocol):
    def __init__(self, app: "ASGIApp", config: SessionAuthConfig):
        """This class creates a small stack of middlewares: It wraps the
        SessionAuthMiddleware inside ExceptionHandlerMiddleware, and it wrap
        this inside SessionMiddleware. This allows the auth middleware to raise
        exceptions and still have the response handled, while having the
        session cleared.

        Args:
            app: An ASGIApp, this value is the next ASGI handler to call in the middleware stack.
            config: An instance of SessionAuthConfig
        """
        super().__init__(app)
        self.app = app
        self.has_wrapped_middleware = False
        self.config = config

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        """This is the entry point to the middleware. If
        'self.had_wrapped_middleware' is False, the wrapper will update the
        value of 'self.app' to be the middleware stack described in the
        __init__ method. Otherwise it will call the next ASGI handler.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive function.
            send: The ASGI send function.

        Returns:
            None
        """
        if not self.has_wrapped_middleware:
            starlite_app = cast("Starlite", scope["app"])
            auth_middleware = SessionAuthMiddleware(
                app=self.app,
                exclude=self.config.exclude,
                retrieve_user_handler=cast("AsyncCallable[[Dict[str, Any]], Any]", self.config.retrieve_user_handler),  # type: ignore
            )
            exception_middleware = ExceptionHandlerMiddleware(
                app=auth_middleware,
                exception_handlers=starlite_app.exception_handlers or {},
                debug=starlite_app.debug,
            )
            self.app = SessionMiddleware(app=exception_middleware, config=self.config)
        await self.app(scope, receive, send)


class SessionAuthMiddleware(AbstractAuthenticationMiddleware):
    def __init__(
        self,
        app: "ASGIApp",
        exclude: Optional[Union[str, List[str]]],
        retrieve_user_handler: AsyncCallable[[Dict[str, Any]], Any],
    ):
        """This is an abstract AuthenticationMiddleware that allows users to
        create their own AuthenticationMiddleware by extending it and
        overriding the 'authenticate_request' method.

        Args:
            app: An ASGIApp, this value is the next ASGI handler to call in the middleware stack.
            exclude: A pattern or list of patterns to skip in the authentication middleware.
            retrieve_user_handler: A callable that receives the session dictionary after it has been decoded and returns
                a 'user' value.
        """
        super().__init__(app=app, exclude=exclude)
        self.retrieve_user_handler = retrieve_user_handler

    async def authenticate_request(self, connection: "HTTPConnection") -> AuthenticationResult:
        """Implementation of the authentication method specified by Starlite's.

        [AbstractAuthenticationMiddleware][starlite.middleware.authentication.AbstractAuthenticationMiddleware].

        Args:
            connection: A Starlette 'HTTPConnection' instance.

        Raises:
            [NotAuthorizedException][starlite.exceptions.NotAuthorizedException]: if session data is empty or user
                is not found.

        Returns:
            [AuthenticationResult][starlite.middleware.authentication.AuthenticationResult]
        """
        if not connection.session or connection.session is Empty:
            # the assignment of 'Empty' forces the session middleware to clear session data.
            connection.scope["session"] = Empty
            raise NotAuthorizedException("no session data found")

        user = await self.retrieve_user_handler(connection.session)

        if not user:
            connection.scope["session"] = Empty
            raise NotAuthorizedException("no user correlating to session found")

        return AuthenticationResult(user=user, auth=connection.session)
