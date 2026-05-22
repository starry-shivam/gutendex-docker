#!/usr/bin/env python
import importlib.util
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gutendex.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError:
        # Only show the custom message when Django itself is missing.
        if importlib.util.find_spec("django") is None:
            raise ImportError(
                "Couldn't import Django. Are you sure it's installed and "
                "available on your PYTHONPATH environment variable? Did you "
                "forget to activate a virtual environment?"
            )
        raise
    execute_from_command_line(sys.argv)
