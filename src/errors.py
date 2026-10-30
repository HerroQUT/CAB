from __future__ import annotations


class AppError(Exception):
    """Represent an application error with HTTP status metadata."""
    def __init__(self, status_code: int, error: str, message: str):
        """Initialize the object with validated constructor inputs."""
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message


def validation_error(message: str) -> AppError:
    """Create a 400 validation error object."""
    return AppError(400, "ValidationError", message)


def not_found_error(message: str) -> AppError:
    """Create a 404 not-found error object."""
    return AppError(404, "NotFound", message)


def unauthorised_error(message: str) -> AppError:
    """Create a 401 unauthorized error object."""
    return AppError(401, "Unauthorised", message)


def forbidden_error(message: str) -> AppError:
    """Create a 403 forbidden error object."""
    return AppError(403, "Forbidden", message)


def conflict_error(message: str) -> AppError:
    """Create a 409 conflict error object."""
    return AppError(409, "Conflict", message)

