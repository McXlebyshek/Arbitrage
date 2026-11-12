"""Shared quote and pair thresholds for the collector and browser client."""

QUOTE_MAX_AGE_MS = 60_000
SOURCE_TIME_SKEW_MS = 1_500
RECEIPT_TIME_SKEW_MS = 3_000
BOOK_MAX_AGE_MS = 3_000
EARLY_QUOTE_MAX_AGE_MS = 3_000
FUTURES_QUOTE_CONFIRMATION_AGE_MS = 90_000
MAX_PRICE_RATIO = 2
FUTURE_CLOCK_TOLERANCE_MS = 1_000

PUBLIC_CONFIG = {
    'quoteMaxAgeMs': QUOTE_MAX_AGE_MS,
    'sourceTimeSkewMs': SOURCE_TIME_SKEW_MS,
    'receiptTimeSkewMs': RECEIPT_TIME_SKEW_MS,
    'bookMaxAgeMs': BOOK_MAX_AGE_MS,
    'earlyQuoteMaxAgeMs': EARLY_QUOTE_MAX_AGE_MS,
    'futuresQuoteConfirmationAgeMs': FUTURES_QUOTE_CONFIRMATION_AGE_MS,
    'maxPriceRatio': MAX_PRICE_RATIO,
    'futureClockToleranceMs': FUTURE_CLOCK_TOLERANCE_MS,
}
