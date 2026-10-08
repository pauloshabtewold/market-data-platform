import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

# closed: no sixth code can enter the vocabulary by accident
ERROR_CODES = frozenset(
    {"invalid_cursor", "unknown_symbol", "invalid_range", "invalid_params", "internal"}
)

INTERNAL_MESSAGE = "the request could not be completed"
UNKNOWN_ROUTE_MESSAGE = "no route matches this path and method"
INVALID_PARAMS_MESSAGE = "one or more parameters are not valid"
INVALID_CURSOR_MESSAGE = "the cursor is not one this endpoint issued"
UNKNOWN_SYMBOL_MESSAGE = "no symbol by that name has been ingested"
INVALID_RANGE_MESSAGE = "the requested date range is not one this endpoint serves"


class ErrorInfo(BaseModel):
    # a Literal, not str, so the document names the same closed vocabulary the frozenset enforces
    code: Literal[tuple(sorted(ERROR_CODES))]
    message: str
    # no default: documented required as well as nullable -- every body carries the key
    detail: dict | None


class ErrorResponse(BaseModel):
    """The one error shape, published so the generated document matches what the service answers."""

    error: ErrorInfo


# shared across the routes' `responses=`, so one status has one schema and description, not four
# independently-typed copies
RESPONSE_400 = {"model": ErrorResponse, "description": "A parameter or cursor was not valid."}
RESPONSE_404 = {"model": ErrorResponse, "description": "No symbol by that name has been ingested."}
RESPONSE_422 = {"model": ErrorResponse, "description": "The requested range was not valid."}
RESPONSE_500 = {"model": ErrorResponse, "description": "The request could not be completed."}
RESPONSE_DEFAULT = {"model": ErrorResponse, "description": "Any other error, in the same shape."}


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
    # checked here, not only in ApiError: the sole wire-shape constructor, reached without raising
    if code not in ERROR_CODES:
        raise ValueError(f"{code!r} is not a member of ERROR_CODES")
    return {"error": {"code": code, "message": message, "detail": detail}}


def _internal_response(exc: Exception) -> JSONResponse:
    # shared by the Exception handler and the branch below, so the 500 bodies cannot drift
    log.exception("unhandled exception", exc_info=exc)
    return JSONResponse(status_code=500, content=error_body("internal", INTERNAL_MESSAGE, None))


# all four async: a def handler runs on the sync routes' thread pool, behind their slow requests
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content=error_body(exc.code, exc.message, exc.detail)
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    loc = exc.errors()[0]["loc"]
    detail = {"reason": "invalid_parameter", "parameter": loc[-1], "location": loc[0]}
    # additive, keeping the three keys above as the contract; pydantic's type slug separates five
    # wrong requests with identical bodies -- missing start and end reports only start
    detail["errors"] = [
        {"parameter": e["loc"][-1], "location": e["loc"][0], "type": e["type"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=400, content=error_body("invalid_params", INVALID_PARAMS_MESSAGE, detail)
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # puts the unrouted 404 and the wrong-method 405 into the one error shape
    if type(exc) is StarletteHTTPException:
        # exact type only: a subclass is an endpoint's own HTTPException, not a routing failure
        detail = {"reason": "unknown_route", "path": request.url.path}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("invalid_params", UNKNOWN_ROUTE_MESSAGE, detail),
            headers=exc.headers,
        )
    return _internal_response(exc)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _internal_response(exc)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
