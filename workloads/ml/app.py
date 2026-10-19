# ML inference dependencies, imported at module level so they load during INIT.
import numpy  # noqa: F401
import sklearn.ensemble  # noqa: F401
import sklearn.linear_model  # noqa: F401


def handler(event, context):
    return {"ok": True}
