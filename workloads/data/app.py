# Data-processing dependencies, imported at module level so they load during INIT.
import numpy  # noqa: F401
import pandas  # noqa: F401


def handler(event, context):
    return {"ok": True}
