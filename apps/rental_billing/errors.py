"""What rental billing refuses with. The messages are shown to the office, or the tenant, as they are."""
from __future__ import annotations

DISABLED_MESSAGE = 'חיוב השכירויות כבוי כרגע. הוא יופעל על ידי בעל המערכת.'


class BillingDisabled(Exception):
    """RENTAL_BILLING_ENABLED is off: nothing may reach Tranzila."""

    def __init__(self, message: str = DISABLED_MESSAGE):
        super().__init__(message)
        self.message = message


class BillingError(ValueError):
    """A charge that cannot be made, or a change that cannot be made. 409 when something is in flight."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
