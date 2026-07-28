# Сборка приложения и превращение ошибок в HTTP-коды
"""FastAPI application factory and error handling."""
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.api.wallets import router
from sqlalchemy.exc import OperationalError

from app.crud import (
    BalanceLimitError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InsufficientFundsError,
    PurchaseNotFoundError,
    RefundNotAllowedError,
    TotalMismatchError,
    WalletFrozenError,
    WalletNotFoundError,
)
from app.promos import (
    CouponAlreadyUsedError,
    CouponLimitReachedError,
    CouponWindowError,
    PromoNotApplicableError,
    UnknownPromoCodeError,
)

app = FastAPI(title="Wallet Service", version="1.0.0")
app.include_router(router, prefix="/api/v1")

WEB_DIR = Path(__file__).resolve().parent / "web"


# WalletNotFoundError -> 404
@app.exception_handler(WalletNotFoundError)
async def _wallet_not_found_handler(
    request: Request,
    exc: WalletNotFoundError,
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# InsufficientFundsError -> 400
@app.exception_handler(InsufficientFundsError)
async def _insufficient_funds_handler(
    request: Request,
    exc: InsufficientFundsError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# TotalMismatchError -> 409
@app.exception_handler(TotalMismatchError)
async def _total_mismatch_handler(
    request: Request,
    exc: TotalMismatchError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# UnknownPromoCodeError -> 400
@app.exception_handler(UnknownPromoCodeError)
async def _unknown_promo_handler(
    request: Request,
    exc: UnknownPromoCodeError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# PromoNotApplicableError -> 400
@app.exception_handler(PromoNotApplicableError)
async def _promo_not_applicable_handler(
    request: Request,
    exc: PromoNotApplicableError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# PurchaseNotFoundError -> 404
@app.exception_handler(PurchaseNotFoundError)
async def _purchase_not_found_handler(
    request: Request,
    exc: PurchaseNotFoundError,
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# RefundNotAllowedError -> 409
@app.exception_handler(RefundNotAllowedError)
async def _refund_not_allowed_handler(
    request: Request,
    exc: RefundNotAllowedError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# CouponWindowError -> 400 (вне срока)
@app.exception_handler(CouponWindowError)
async def _coupon_window_handler(
    request: Request,
    exc: CouponWindowError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# CouponLimitReachedError -> 409 (общий лимит)
@app.exception_handler(CouponLimitReachedError)
async def _coupon_limit_handler(
    request: Request,
    exc: CouponLimitReachedError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# CouponAlreadyUsedError -> 409 (лимит на кошелёк)
@app.exception_handler(CouponAlreadyUsedError)
async def _coupon_already_used_handler(
    request: Request,
    exc: CouponAlreadyUsedError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# WalletFrozenError -> 409
@app.exception_handler(WalletFrozenError)
async def _wallet_frozen_handler(
    request: Request,
    exc: WalletFrozenError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# BalanceLimitError -> 400 (потолок баланса)
@app.exception_handler(BalanceLimitError)
async def _balance_limit_handler(
    request: Request,
    exc: BalanceLimitError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# тот же ключ на другом теле -> 422
@app.exception_handler(IdempotencyConflictError)
async def _idempotency_conflict_handler(
    request: Request,
    exc: IdempotencyConflictError,
) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# запрос с этим ключом ещё идёт -> 409
@app.exception_handler(IdempotencyInProgressError)
async def _idempotency_in_progress_handler(
    request: Request,
    exc: IdempotencyInProgressError,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# потеря соединения с БД -> 503
@app.exception_handler(OperationalError)
async def _database_unavailable_handler(
    request: Request,
    exc: OperationalError,
) -> JSONResponse:
    """Report a lost database as unavailable rather than as a bug.

    Deliberately narrow: only connection-level failures are mapped here.
    Other database errors keep bubbling up as 500 so that real defects
    stay visible instead of being dressed up as an outage.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": "Database is unavailable, please retry"},
    )


# проверка живости
@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {"status": "ok"}


# страница-пульт на /
@app.get("/", include_in_schema=False)
async def console() -> FileResponse:
    """Serve the operator console.

    A browser can only issue GET from the address bar, so the API alone is
    awkward to try by hand. This page posts to the same endpoints.
    """
    return FileResponse(WEB_DIR / "index.html")


# тихий ответ на запрос иконки
@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Answer the browser's automatic icon request without a 404."""
    return Response(status_code=204)
