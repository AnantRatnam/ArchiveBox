# ruff: noqa
class UsernameAndPasswordAuth(HttpBasicAuth):
    """Allow authenticating by passing username & password via HTTP Basic Authentication (not recommended)"""

    def authenticate(self, request: HttpRequest, username: str, password: str) -> User | None:
        return _require_superuser(
            auth_using_password(username=username, password=password, request=request),
            request,
            self.__class__.__name__,
        )


class DjangoSessionAuth:
    """Allow authenticating with existing Django session cookies (same-origin only)."""

    def __call__(self, request: HttpRequest) -> User | None:
        return self.authenticate(request)

    def authenticate(self, request: HttpRequest, **kwargs) -> User | None:
        user = getattr(request, "user", None)
        if isinstance(user, User) and user.is_authenticated:
            setattr(request, "_api_auth_method", self.__class__.__name__)
            if not user.is_superuser:
                raise HttpError(403, "Valid session but User does not have permission (make sure user.is_superuser=True)")
            return user
        return None
