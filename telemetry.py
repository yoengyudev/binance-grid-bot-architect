"""Persistent exception telemetry shared by engine and services."""

import traceback
from typing import Any
from database import GridDatabase
from settings import (
    ENGINE_STATUS_KEY,
)


def _exception_details(error: Any) -> str:
    if isinstance(error, BaseException):
        return "".join(traceback.format_exception(error))
    return str(error)


def _journal_fault(database: GridDatabase, error: Any,
                   trigger_event: str = "ENGINE_FAULT") -> None:
    state = database.get_state(ENGINE_STATUS_KEY)
    database.record_transition(state, state, trigger_event, _exception_details(error))
