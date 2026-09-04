import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

# closed so a sixth code cannot enter the vocabulary by accident at a later feature
ERROR_CODES = frozenset(
    {"invalid_cursor", "unknown_symbol", "invalid_range", "invalid_params", "internal"}
)

INTERNAL_MESSAGE = "the request could not be completed"
UNKNOWN_ROUTE_MESSAGE = "no route matches this path and method"
INVALID_PARAMS_MESSAGE = "one or more parameters are not valid"
INVALID_CURSOR_MESSAGE = "the cursor is not one this endpoint issued"


class ApiError(RuntimeError):
    """The one error shape's exception: refuses a code outside ERROR_CODES at construction."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None):
        if code not in ERROR_CODES:
            raise ValueError(f"{code!r} is not a member of ERROR_CODES")
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def error_body(code: str, message: str, detail: dict | None) -> dict:
    # checked here as well as in ApiError: this is the single constructor of the wire shape, so it
    # is the door a later feature's handler reaches for without raising anything
    if code not in ERROR_CODES:
        raise ValueError(f"{code!r} is not a member of ERROR_CODES")
    return {"error": {"code": code, "message": message, "detail": detail}}


def _internal_response(exc: Exception) -> JSONResponse:
    # shared by the Exception handler and the non-routing branch below, so the two 500 bodies cannot drift apart
    log.exception("unhandled exception", exc_info=exc)
    return JSONResponse(status_code=500, content=error_body("internal", INTERNAL_MESSAGE, None))


def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content=error_body(exc.code, exc.message, exc.detail)
    )


def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    loc = exc.errors()[0]["loc"]
    detail = {"reason": "invalid_parameter", "parameter": loc[-1], "location": loc[0]}
    return JSONResponse(
        status_code=400, content=error_body("invalid_params", INVALID_PARAMS_MESSAGE, detail)
    )


def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # this is what puts the unrouted 404 and the wrong-method 405 into the one error shape
    if type(exc) is StarletteHTTPException:
        # exact type only -- a subclass reaching here is an endpoint's own HTTPException, not a routing failure
        detail = {"reason": "unknown_route", "path": request.url.path}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("invalid_params", UNKNOWN_ROUTE_MESSAGE, detail),
            headers=exc.headers,
        )
    return _internal_response(exc)


def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _internal_response(exc)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
