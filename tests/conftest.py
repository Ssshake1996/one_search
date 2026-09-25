"""Shared GUI lifetime: one Tcl/Tk interpreter, independent test windows."""
import gc
import os

import pytest


@pytest.fixture(scope="session")
def tk_root():
    import tkinter as tk
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("No X display is configured for Tk")
    # Tk initialization errors on a configured display must fail the tests.
    # Recreating interpreters between dialogs is not the application's lifetime.
    root = tk.Tk()
    root.attributes("-alpha", 0.0)
    root.withdraw()
    yield root
    root.destroy()
    del root
    gc.collect()
